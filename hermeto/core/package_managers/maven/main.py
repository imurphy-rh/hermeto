# SPDX-License-Identifier: GPL-3.0-only
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from packageurl import PackageURL

from hermeto.core.checksum import ChecksumInfo, must_match_any_checksum
from hermeto.core.config import get_config
from hermeto.core.errors import LockfileNotFound, PackageRejected
from hermeto.core.models.input import Mode, Request
from hermeto.core.models.output import Component, EnvironmentVariable, ProjectFile, RequestOutput
from hermeto.core.models.property_semantics import Property, PropertyEnum
from hermeto.core.models.sbom import Annotation, create_backend_annotation
from hermeto.core.package_managers.general import async_download_files
from hermeto.core.package_managers.maven.models import (
    MavenArtifact,
    MavenLockfile,
    parse_maven_boms,
    parse_maven_dependencies,
    parse_maven_plugins,
)
from hermeto.core.package_managers.maven.utils import (
    derive_pom_filename,
    derive_repository_id,
    validate_artifact_url,
    validate_checksum_format,
)

log = logging.getLogger(__name__)

DEFAULT_LOCKFILE = "lockfile.json"
MAX_ARTIFACT_COUNT = 10_000
MAX_ARTIFACT_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB

SETTINGS_XML_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0"
          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
          xsi:schemaLocation="http://maven.apache.org/SETTINGS/1.2.0 https://maven.apache.org/xsd/settings-1.2.0.xsd">
  <localRepository>${output_dir}/deps/maven</localRepository>
  <mirrors>
    <mirror>
      <id>hermeto-local</id>
      <mirrorOf>*</mirrorOf>
      <url>file://${output_dir}/deps/maven</url>
    </mirror>
  </mirrors>
</settings>
"""


def fetch_maven_source(request: Request) -> RequestOutput:
    """Resolve and fetch Maven dependencies for the given request."""
    annotations: list[Annotation] = []
    components: list[Component] = []

    deps_dir = request.output_dir.join_within_root("deps", "maven")
    deps_dir.path.mkdir(parents=True, exist_ok=True)

    all_artifacts: list[MavenArtifact] = []
    lockfiles: list[MavenLockfile] = []

    for package in request.maven_packages:
        project_dir = request.source_dir.join_within_root(package.path)
        result = _resolve_maven_project(project_dir.path, request.mode)
        if result is not None:
            artifacts, lockfile = result
            all_artifacts.extend(artifacts)
            lockfiles.append(lockfile)

    all_artifacts = _deduplicate_artifacts(all_artifacts)

    if all_artifacts:
        _validate_artifacts(all_artifacts)
        _download_maven_artifacts(deps_dir.path, all_artifacts)

    components.extend(_generate_sbom_components(all_artifacts))
    for lockfile in lockfiles:
        components.append(_generate_main_component(lockfile))

    backend_annotation = create_backend_annotation(components, "x-maven")
    if backend_annotation is not None:
        annotations.append(backend_annotation)

    return RequestOutput.from_obj_list(
        annotations=annotations,
        components=components,
        environment_variables=[
            EnvironmentVariable(
                name="MAVEN_OPTS",
                value="-Dmaven.repo.local=${output_dir}/deps/maven",
            ),
            EnvironmentVariable(
                name="MAVEN_ARGS",
                value="-s ${output_dir}/settings.xml",
            ),
        ],
        project_files=[
            ProjectFile(
                abspath=request.output_dir.path / "settings.xml", template=SETTINGS_XML_TEMPLATE
            ),
        ],
    )


def _resolve_maven_project(
    project_dir: Path, mode: Mode = Mode.STRICT
) -> tuple[list[MavenArtifact], MavenLockfile] | None:
    """Parse a Maven lockfile and return its artifacts and lockfile metadata.

    Returns None in permissive mode when the lockfile is missing.
    """
    lockfile_path = project_dir / DEFAULT_LOCKFILE
    try:
        lockfile = MavenLockfile.from_file(lockfile_path)
    except FileNotFoundError:
        if mode == Mode.PERMISSIVE:
            log.warning(
                "lockfile.json not found in %s, skipping due to permissive mode", project_dir
            )
            return None
        raise LockfileNotFound(
            lockfile_path,
            solution="Generate a lockfile with: "
            "mvn io.github.chains-project:maven-lockfile:generate",
        )
    deps = parse_maven_dependencies(lockfile)
    plugins = parse_maven_plugins(lockfile)
    boms = parse_maven_boms(deps)

    return deps + plugins + boms, lockfile


def _deduplicate_artifacts(artifacts: list[MavenArtifact]) -> list[MavenArtifact]:
    """Deduplicate artifacts by URL to avoid redundant downloads."""
    seen: dict[str, MavenArtifact] = {}
    for artifact in artifacts:
        if artifact.url not in seen:
            seen[artifact.url] = artifact
    deduplicated = list(seen.values())
    if len(deduplicated) < len(artifacts):
        log.info("Deduplicated %d artifacts to %d unique URLs", len(artifacts), len(deduplicated))
    return deduplicated


def _validate_artifacts(artifacts: list[MavenArtifact]) -> None:
    """Validate all artifact URLs and checksum formats before downloading."""
    if len(artifacts) > MAX_ARTIFACT_COUNT:
        raise PackageRejected(
            f"Lockfile declares {len(artifacts)} artifacts, exceeding the limit of {MAX_ARTIFACT_COUNT}",
            solution="This may indicate a corrupted or malicious lockfile. "
            "Verify the lockfile and regenerate if necessary.",
        )

    for artifact in artifacts:
        validate_artifact_url(artifact.url)
        validate_checksum_format(artifact.algorithm, artifact.checksum)


def _generate_sbom_components(
    artifacts: list[MavenArtifact],
) -> list[Component]:
    """Generate SBOM components from Maven dependencies and plugins."""
    result: list[Component] = []
    for artifact in artifacts:
        purl = PackageURL(
            type="maven",
            namespace=artifact.group_id,
            name=artifact.artifact_id,
            version=artifact.version,
        )

        name = f"{artifact.group_id}.{artifact.artifact_id}"
        scope = artifact.get("scope", "compile")  # fallback to 'compile' scope
        component = Component(
            name=name,
            purl=purl.to_string(),
            version=artifact.version,
            properties=[Property(name=PropertyEnum.PROP_MAVEN_SCOPE, value=scope)],
        )
        result.append(component)

    return result


def _generate_main_component(lockfile: MavenLockfile) -> Component:
    """Get the main component from Maven lockfile."""
    group_id = lockfile.data["groupId"]
    artifact_id = lockfile.data["artifactId"]
    version = lockfile.data["version"]

    purl = PackageURL(type="maven", namespace=group_id, name=artifact_id, version=version)

    return Component(
        name=f"{group_id}.{artifact_id}",
        purl=purl.to_string(),
        version=version,
        properties=[Property(name=PropertyEnum.PROP_MAVEN_SCOPE, value="compile")],
    )


def _create_artifact_dir(deps_dir: Path, artifact: MavenArtifact) -> Path:
    """Prepare a directory for the Maven artifact to download."""
    artifact_dir_abs_path = deps_dir / artifact.artifact_relative_dir
    artifact_dir_abs_path.mkdir(parents=True, exist_ok=True)
    return artifact_dir_abs_path / artifact.filename


def _download_maven_artifacts(deps_dir: Path, artifacts: list[MavenArtifact]) -> None:
    """Download Maven dependencies."""
    config = get_config()

    download_paths = {a.url: _create_artifact_dir(deps_dir, a) for a in artifacts}
    asyncio.run(async_download_files(download_paths, config.runtime.concurrency_limit))

    _verify_checksums(artifacts, download_paths)
    _verify_artifact_sizes(download_paths)

    pom_files, pom_checksums = _prepare_pom_and_checksum_downloads(deps_dir, artifacts)
    asyncio.run(async_download_files(pom_files, config.runtime.concurrency_limit))
    asyncio.run(_async_download_optional_files(pom_checksums))

    _create_checksums_files(artifacts, download_paths)
    _create_remote_repositories_files(deps_dir, artifacts)


def _verify_checksums(artifacts: list[MavenArtifact], download_paths: dict[str, Path]) -> None:
    """Verify checksums of all downloaded Maven artifacts."""
    for artifact in artifacts:
        download_path = download_paths[artifact.url]
        expected = ChecksumInfo(artifact.algorithm, artifact.checksum)
        must_match_any_checksum(download_path, [expected])


def _verify_artifact_sizes(download_paths: dict[str, Path]) -> None:
    """Reject artifacts that exceed the size limit."""
    for path in download_paths.values():
        size = path.stat().st_size
        if size > MAX_ARTIFACT_SIZE_BYTES:
            raise PackageRejected(
                f"Artifact {path.name} is {size} bytes, exceeding the limit of {MAX_ARTIFACT_SIZE_BYTES}",
                solution="This may indicate a corrupted download or an unexpectedly large artifact. "
                "Verify the artifact in your lockfile.",
            )


def _prepare_pom_and_checksum_downloads(
    deps_dir: Path, artifacts: list[MavenArtifact]
) -> tuple[dict[str, Path], dict[str, Path]]:
    """Prepare POM files and checksum files to download."""
    pom_files: dict[str, Path] = {}
    pom_checksums: dict[str, Path] = {}

    for artifact in artifacts:
        parsed_url = urlparse(artifact.url)
        url_path = Path(parsed_url.path)

        if url_path.suffix != ".pom":
            pom_filename = derive_pom_filename(artifact.artifact_id, artifact.version)
            pom_file_url = artifact.url.replace(url_path.name, pom_filename)

            artifact_dir = deps_dir / artifact.artifact_relative_dir
            pom_files[pom_file_url] = artifact_dir / pom_filename

            pom_checksum_url = f"{pom_file_url}.{artifact.algorithm}"
            pom_checksum_path = artifact_dir / f"{pom_filename}.{artifact.algorithm}"
            pom_checksums[pom_checksum_url] = pom_checksum_path

    return pom_files, pom_checksums


async def _download_optional_file(session: aiohttp.ClientSession, url: str, path: Path) -> None:
    """Download a file that may not exist (404 is acceptable)."""
    suffixes = (".sha1", ".md5", ".sha256", ".sha512", ".sha224", ".sha384")
    async with session.get(url, raise_for_status=False) as response:
        if response.status == 404:
            log.debug("Skipping %s (not found)", url)
            return

        if response.status >= 400:
            log.warning("Failed to download optional file %s: HTTP %d", url, response.status)
            return

        content = await response.read()
        if path.suffix in suffixes:
            path.write_text(content.decode().strip())
        else:
            path.write_bytes(content)


async def _async_download_optional_files(files: dict[str, Path]) -> None:
    """Download optional files, logging any errors without failing the build."""
    async with aiohttp.ClientSession(trust_env=True) as session:
        urls = list(files.keys())
        tasks = [_download_optional_file(session, url, files[url]) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for url, result in zip(urls, results):
            if isinstance(result, Exception):
                log.warning("Error downloading optional file %s: %s", url, result)


def _create_checksums_files(
    artifacts: list[MavenArtifact], download_paths: dict[str, Path]
) -> None:
    """Create checksum files for the Maven artifacts."""
    for artifact in artifacts:
        download_path = download_paths[artifact.url]
        checksum_file = download_path.with_suffix(f"{download_path.suffix}.{artifact.algorithm}")
        checksum_file.write_text(artifact.checksum)


def _create_remote_repositories_files(deps_dir: Path, artifacts: list[MavenArtifact]) -> None:
    """Create a _remote.repositories file for each artifact."""
    now = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S %Z %Y")
    for artifact in artifacts:
        artifact_dir_abs_path = deps_dir / artifact.artifact_relative_dir
        remote_repos_file = artifact_dir_abs_path.joinpath("_remote.repositories")
        lines = [
            "#NOTE: This is a Maven Resolver internal implementation file, its format can be changed without prior notice.\n",
            f"#{now}\n",
        ]
        lines.append(f"{artifact.filename}>{derive_repository_id(artifact.url)}=\n")
        remote_repos_file.write_text("".join(lines))
