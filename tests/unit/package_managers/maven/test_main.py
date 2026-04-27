# SPDX-License-Identifier: GPL-3.0-only
import xml.etree.ElementTree as ET  # noqa: S405
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from hermeto.core.checksum import ChecksumInfo
from hermeto.core.errors import (
    ChecksumVerificationFailed,
    InvalidChecksum,
    LockfileNotFound,
    PackageRejected,
)
from hermeto.core.models.input import Mode, Request
from hermeto.core.models.output import RequestOutput
from hermeto.core.models.property_semantics import PropertyEnum
from hermeto.core.package_managers.maven.main import (
    MAX_ARTIFACT_COUNT,
    SETTINGS_XML_TEMPLATE,
    _create_checksums_files,
    _create_remote_repositories_files,
    _deduplicate_artifacts,
    _download_maven_artifacts,
    _download_optional_file,
    _generate_main_component,
    _generate_sbom_components,
    _prepare_pom_and_checksum_downloads,
    _rewrite_url_for_proxy,
    _validate_artifacts,
    _verify_artifact_sizes,
    _verify_checksums,
    fetch_maven_source,
)
from hermeto.core.package_managers.maven.models import MavenArtifact, MavenLockfile
from tests.unit.package_managers.maven.conftest import (
    make_artifact,
    make_artifact_data,
    minimal_lockfile_data,
    write_lockfile,
)


class TestFetchMavenSource:
    def test_minimal(self, maven_fetch_result: RequestOutput) -> None:
        assert len(maven_fetch_result.components) > 0
        assert len(maven_fetch_result.build_config.environment_variables) == 2
        assert len(maven_fetch_result.build_config.project_files) == 1

        env_names = {ev.name for ev in maven_fetch_result.build_config.environment_variables}
        assert "MAVEN_OPTS" in env_names
        assert "MAVEN_ARGS" in env_names

    def test_environment_variables(self, maven_fetch_result: RequestOutput) -> None:
        env_map = {
            ev.name: ev.value for ev in maven_fetch_result.build_config.environment_variables
        }
        assert "maven.repo.local" in env_map["MAVEN_OPTS"]
        assert "settings.xml" in env_map["MAVEN_ARGS"]

    def test_settings_xml(self, maven_fetch_result: RequestOutput, tmp_path: Path) -> None:
        assert len(maven_fetch_result.build_config.project_files) == 1
        pf = maven_fetch_result.build_config.project_files[0]
        assert "localRepository" in pf.template
        assert "mirrorOf" in pf.template

    @patch("hermeto.core.package_managers.maven.main._download_maven_artifacts")
    def test_strict_mode_missing_lockfile(self, mock_download: MagicMock, tmp_path: Path) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        request = Request(
            source_dir=source_dir,
            output_dir=output_dir,
            packages=[{"type": "x-maven", "path": "."}],
            mode=Mode.STRICT,
        )

        with pytest.raises(LockfileNotFound, match="lockfile"):
            fetch_maven_source(request)

    @patch("hermeto.core.package_managers.maven.main._download_maven_artifacts")
    def test_permissive_mode_missing_lockfile(
        self, mock_download: MagicMock, tmp_path: Path
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        request = Request(
            source_dir=source_dir,
            output_dir=output_dir,
            packages=[{"type": "x-maven", "path": "."}],
            mode=Mode.PERMISSIVE,
        )

        result = fetch_maven_source(request)
        assert len(result.components) == 0

    @patch("hermeto.core.package_managers.maven.main._download_maven_artifacts")
    def test_empty_deps_produces_main_component(
        self, mock_download: MagicMock, tmp_path: Path
    ) -> None:
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        write_lockfile(source_dir, minimal_lockfile_data(dependencies=[]))

        request = Request(
            source_dir=source_dir,
            output_dir=output_dir,
            packages=[{"type": "x-maven", "path": "."}],
        )

        result = fetch_maven_source(request)
        assert len(result.components) == 1
        assert "org.example" in result.components[0].name

    @patch("hermeto.core.package_managers.maven.main._download_maven_artifacts")
    def test_multimodule_deduplicates_across_modules(
        self, mock_download: MagicMock, tmp_path: Path
    ) -> None:
        source_dir = tmp_path / "source"
        output_dir = tmp_path / "output"
        source_dir.mkdir()
        output_dir.mkdir()

        shared_dep = make_artifact_data()
        module_a = source_dir / "module-a"
        module_a.mkdir()
        write_lockfile(
            module_a,
            {
                "groupId": "org.example",
                "artifactId": "module-a",
                "version": "1.0.0",
                "dependencies": [shared_dep],
            },
        )

        module_b = source_dir / "module-b"
        module_b.mkdir()
        write_lockfile(
            module_b,
            {
                "groupId": "org.example",
                "artifactId": "module-b",
                "version": "1.0.0",
                "dependencies": [shared_dep],
            },
        )

        request = Request(
            source_dir=source_dir,
            output_dir=output_dir,
            packages=[
                {"type": "x-maven", "path": "module-a"},
                {"type": "x-maven", "path": "module-b"},
            ],
        )

        result = fetch_maven_source(request)

        dep_components = [c for c in result.components if c.name == "org.example.foo"]
        assert len(dep_components) == 1

        main_components = [c for c in result.components if "module-" in c.name]
        assert len(main_components) == 2

        mock_download.assert_called_once()


class TestDeduplicateArtifacts:
    def test_no_duplicates(self) -> None:
        a1 = make_artifact(artifactId="foo")
        a2 = make_artifact(
            artifactId="bar",
            resolved="https://repo.maven.apache.org/maven2/org/example/bar/1.0.0/bar-1.0.0.jar",
        )
        assert len(_deduplicate_artifacts([a1, a2])) == 2

    def test_removes_duplicates(self) -> None:
        assert len(_deduplicate_artifacts([make_artifact(), make_artifact()])) == 1

    def test_empty_list(self) -> None:
        assert _deduplicate_artifacts([]) == []


class TestValidateArtifacts:
    def test_valid_artifacts(self) -> None:
        _validate_artifacts([make_artifact()])

    def test_count_cap_exceeded(self) -> None:
        artifacts = [
            make_artifact(
                artifactId=f"dep-{i}",
                resolved=f"https://repo.maven.apache.org/maven2/org/example/dep-{i}/1.0/dep-{i}-1.0.jar",
            )
            for i in range(MAX_ARTIFACT_COUNT + 1)
        ]
        with pytest.raises(PackageRejected, match=str(MAX_ARTIFACT_COUNT)):
            _validate_artifacts(artifacts)

    def test_count_at_limit_is_accepted(self) -> None:
        artifacts = [
            make_artifact(
                artifactId=f"dep-{i}",
                resolved=f"https://repo.maven.apache.org/maven2/org/example/dep-{i}/1.0/dep-{i}-1.0.jar",
            )
            for i in range(MAX_ARTIFACT_COUNT)
        ]
        _validate_artifacts(artifacts)

    def test_invalid_url_rejected(self) -> None:
        artifacts = [
            make_artifact(
                resolved="http://repo.maven.apache.org/maven2/org/example/foo/1.0.0/foo-1.0.0.jar"
            )
        ]
        with pytest.raises(PackageRejected, match="https://"):
            _validate_artifacts(artifacts)

    def test_invalid_checksum_rejected(self) -> None:
        artifacts = [make_artifact(checksum="not-hex!!!")]
        with pytest.raises(InvalidChecksum):
            _validate_artifacts(artifacts)


class TestGenerateSbomComponents:
    def test_single_component(self) -> None:
        components = _generate_sbom_components([make_artifact()])
        assert len(components) == 1
        c = components[0]
        assert c.name == "org.example.foo"
        assert c.version == "1.0.0"
        assert "pkg:maven/org.example/foo@1.0.0" in c.purl

    def test_scope_property(self) -> None:
        components = _generate_sbom_components([make_artifact(scope="test")])
        props = {p.name: p.value for p in components[0].properties}
        assert props.get(PropertyEnum.PROP_MAVEN_SCOPE) == "test"

    def test_default_scope_is_compile(self) -> None:
        data = make_artifact_data()
        del data["scope"]
        components = _generate_sbom_components([MavenArtifact(data)])
        props = {p.name: p.value for p in components[0].properties}
        assert props.get(PropertyEnum.PROP_MAVEN_SCOPE) == "compile"

    def test_multiple_components(self) -> None:
        a1 = make_artifact()
        a2 = make_artifact(
            groupId="com.google.guava",
            artifactId="guava",
            version="33.1.0-jre",
            resolved="https://repo.maven.apache.org/maven2/com/google/guava/guava/33.1.0-jre/guava-33.1.0-jre.jar",
        )
        components = _generate_sbom_components([a1, a2])
        assert len(components) == 2
        names = {c.name for c in components}
        assert "org.example.foo" in names
        assert "com.google.guava.guava" in names


class TestGenerateSbomComponentsClassifierAndType:
    def test_classifier_in_purl(self) -> None:
        artifact = make_artifact(
            classifier="sources",
            resolved="https://repo.maven.apache.org/maven2/org/example/foo/1.0.0/foo-1.0.0-sources.jar",
        )
        components = _generate_sbom_components([artifact])
        assert "classifier=sources" in components[0].purl

    def test_no_classifier_no_qualifier(self) -> None:
        components = _generate_sbom_components([make_artifact()])
        assert "classifier=" not in components[0].purl

    def test_type_war_in_purl(self) -> None:
        artifact = make_artifact(type="war")
        components = _generate_sbom_components([artifact])
        assert "type=war" in components[0].purl

    def test_type_jar_omitted_from_purl(self) -> None:
        artifact = make_artifact(type="jar")
        components = _generate_sbom_components([artifact])
        assert "type=" not in components[0].purl

    def test_no_type_no_qualifier(self) -> None:
        components = _generate_sbom_components([make_artifact()])
        assert "type=" not in components[0].purl

    def test_classifier_and_type_combined(self) -> None:
        artifact = make_artifact(classifier="sources", type="war")
        components = _generate_sbom_components([artifact])
        assert "classifier=sources" in components[0].purl
        assert "type=war" in components[0].purl

    def test_empty_string_classifier_ignored(self) -> None:
        artifact = make_artifact(classifier="")
        components = _generate_sbom_components([artifact])
        assert "classifier=" not in components[0].purl

    def test_empty_string_type_ignored(self) -> None:
        artifact = make_artifact(type="")
        components = _generate_sbom_components([artifact])
        assert "type=" not in components[0].purl


class TestGenerateMainComponent:
    def test_basic(self) -> None:
        lockfile = MavenLockfile(
            Path("/fake"),
            {"groupId": "org.example", "artifactId": "my-app", "version": "1.0.0"},
        )
        component = _generate_main_component(lockfile)
        assert component.name == "org.example.my-app"
        assert component.version == "1.0.0"
        assert "pkg:maven/org.example/my-app@1.0.0" in component.purl


class TestVerifyChecksums:
    def test_matching_checksum(self, tmp_path: Path) -> None:
        artifact = make_artifact()
        file_path = tmp_path / "foo-1.0.0.jar"
        file_path.write_bytes(b"test content")

        with patch(
            "hermeto.core.package_managers.maven.main.must_match_any_checksum"
        ) as mock_check:
            _verify_checksums([artifact], {artifact.url: file_path})
            mock_check.assert_called_once_with(
                file_path,
                [ChecksumInfo("sha256", "a" * 64)],
            )

    def test_mismatched_checksum(self, tmp_path: Path) -> None:
        artifact = make_artifact()
        file_path = tmp_path / "foo-1.0.0.jar"
        file_path.write_bytes(b"test content")

        with patch(
            "hermeto.core.package_managers.maven.main.must_match_any_checksum",
            side_effect=ChecksumVerificationFailed("foo-1.0.0.jar"),
        ):
            with pytest.raises(ChecksumVerificationFailed):
                _verify_checksums([artifact], {artifact.url: file_path})


class TestVerifyArtifactSizes:
    def test_under_limit(self, tmp_path: Path) -> None:
        file_path = tmp_path / "small.jar"
        file_path.write_bytes(b"x" * 100)
        _verify_artifact_sizes({"url": file_path})

    def test_over_limit(self, tmp_path: Path) -> None:
        file_path = tmp_path / "big.jar"
        file_path.write_bytes(b"x" * 10)

        with patch("hermeto.core.package_managers.maven.main.MAX_ARTIFACT_SIZE_BYTES", 5):
            with pytest.raises(PackageRejected, match="exceeding the limit"):
                _verify_artifact_sizes({"url": file_path})


class TestCreateArtifactDir:
    def test_creates_directory(self, tmp_path: Path) -> None:
        from hermeto.core.package_managers.maven.main import _create_artifact_dir

        artifact = make_artifact()
        path = _create_artifact_dir(tmp_path, artifact)
        assert path.parent.exists()
        assert path.name == "foo-1.0.0.jar"
        assert "org/example/foo/1.0.0" in str(path.parent)


class TestPreparePomAndChecksumDownloads:
    def test_jar_artifact(self, tmp_path: Path) -> None:
        poms, checksums = _prepare_pom_and_checksum_downloads(tmp_path, [make_artifact()])

        assert len(poms) == 1
        pom_url = list(poms.keys())[0]
        assert pom_url.endswith("foo-1.0.0.pom")
        assert "foo-1.0.0.jar" not in pom_url

        assert len(checksums) == 1
        assert list(checksums.keys())[0].endswith(".sha256")

    def test_pom_artifact_skipped(self, tmp_path: Path) -> None:
        artifact = make_artifact(
            resolved="https://repo.maven.apache.org/maven2/org/example/foo/1.0.0/foo-1.0.0.pom",
            type="pom",
        )
        poms, checksums = _prepare_pom_and_checksum_downloads(tmp_path, [artifact])
        assert len(poms) == 0
        assert len(checksums) == 0

    def test_classified_artifact_skipped(self, tmp_path: Path) -> None:
        artifact = make_artifact(
            classifier="sources",
            resolved="https://repo.maven.apache.org/maven2/org/example/foo/1.0.0/foo-1.0.0-sources.jar",
        )
        poms, checksums = _prepare_pom_and_checksum_downloads(tmp_path, [artifact])
        assert len(poms) == 0
        assert len(checksums) == 0

    def test_empty_classifier_gets_pom(self, tmp_path: Path) -> None:
        artifact = make_artifact(classifier="")
        poms, checksums = _prepare_pom_and_checksum_downloads(tmp_path, [artifact])
        assert len(poms) == 1

    def test_type_war_gets_pom(self, tmp_path: Path) -> None:
        artifact = make_artifact(
            type="war",
            resolved="https://repo.maven.apache.org/maven2/org/example/foo/1.0.0/foo-1.0.0.war",
        )
        poms, checksums = _prepare_pom_and_checksum_downloads(tmp_path, [artifact])
        assert len(poms) == 1
        pom_url = list(poms.keys())[0]
        assert pom_url.endswith("foo-1.0.0.pom")

    def test_multiple_artifacts(self, tmp_path: Path) -> None:
        a1 = make_artifact()
        a2 = make_artifact(
            groupId="com.example",
            artifactId="bar",
            version="2.0",
            resolved="https://repo.maven.apache.org/maven2/com/example/bar/2.0/bar-2.0.jar",
        )
        poms, checksums = _prepare_pom_and_checksum_downloads(tmp_path, [a1, a2])
        assert len(poms) == 2
        assert len(checksums) == 2


class TestDownloadOptionalFile:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [404, 403, 500])
    async def test_non_success_skipped(self, status: int, tmp_path: Path) -> None:
        mock_response = AsyncMock()
        mock_response.status = status
        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.get.return_value.__aenter__.return_value = mock_response

        path = tmp_path / "file.sha256"
        await _download_optional_file(mock_session, "https://example.com/file.sha256", path)
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_success_checksum_file(self, tmp_path: Path) -> None:
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.read.return_value = b"  abc123def456  \n"
        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.get.return_value.__aenter__.return_value = mock_response

        path = tmp_path / "file.sha256"
        await _download_optional_file(mock_session, "https://example.com/file.sha256", path)
        assert path.read_text() == "abc123def456"

    @pytest.mark.asyncio
    async def test_success_pom_file(self, tmp_path: Path) -> None:
        pom_content = b"<project>...</project>"
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.read.return_value = pom_content
        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.get.return_value.__aenter__.return_value = mock_response

        path = tmp_path / "foo-1.0.0.pom"
        await _download_optional_file(mock_session, "https://example.com/foo-1.0.0.pom", path)
        assert path.read_bytes() == pom_content


class TestAsyncDownloadOptionalFiles:
    @pytest.mark.asyncio
    async def test_empty_dict(self) -> None:
        from hermeto.core.package_managers.maven.main import _async_download_optional_files

        await _async_download_optional_files({})

    @pytest.mark.asyncio
    async def test_exception_is_logged_not_raised(self, tmp_path: Path) -> None:
        from hermeto.core.package_managers.maven.main import _async_download_optional_files

        files = {"https://example.com/file.sha256": tmp_path / "file.sha256"}
        with patch(
            "hermeto.core.package_managers.maven.main._download_optional_file",
            side_effect=aiohttp.ClientError("connection failed"),
        ):
            await _async_download_optional_files(files)


class TestCreateChecksumsFiles:
    def test_creates_checksum_file(self, tmp_path: Path) -> None:
        artifact = make_artifact()
        jar_path = tmp_path / "foo-1.0.0.jar"
        jar_path.write_bytes(b"fake jar content")

        _create_checksums_files([artifact], {artifact.url: jar_path})

        checksum_file = jar_path.with_suffix(".jar.sha256")
        assert checksum_file.exists()
        assert checksum_file.read_text() == "a" * 64

    def test_sha1_suffix(self, tmp_path: Path) -> None:
        artifact = make_artifact(checksumAlgorithm="SHA-1", checksum="b" * 40)
        jar_path = tmp_path / "foo-1.0.0.jar"
        jar_path.write_bytes(b"content")

        _create_checksums_files([artifact], {artifact.url: jar_path})

        checksum_file = jar_path.with_suffix(".jar.sha1")
        assert checksum_file.exists()
        assert checksum_file.read_text() == "b" * 40


class TestCreateRemoteRepositoriesFiles:
    def test_creates_file(self, tmp_path: Path) -> None:
        artifact = make_artifact()
        artifact_dir = tmp_path / "org" / "example" / "foo" / "1.0.0"
        artifact_dir.mkdir(parents=True)

        _create_remote_repositories_files(tmp_path, [artifact])

        remote_repos_file = artifact_dir / "_remote.repositories"
        assert remote_repos_file.exists()
        content = remote_repos_file.read_text()
        assert "foo-1.0.0.jar>central=" in content
        assert "#NOTE:" in content


class TestDownloadMavenArtifacts:
    @patch("hermeto.core.package_managers.maven.main._async_download_optional_files")
    @patch("hermeto.core.package_managers.maven.main.async_download_files")
    @patch("hermeto.core.package_managers.maven.main.get_config")
    def test_calls_download_and_verify(
        self,
        mock_config: MagicMock,
        mock_download: MagicMock,
        mock_optional: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_config.return_value.runtime.concurrency_limit = 5
        artifact = make_artifact()

        artifact_dir = tmp_path / "org" / "example" / "foo" / "1.0.0"
        artifact_dir.mkdir(parents=True)
        jar_path = artifact_dir / "foo-1.0.0.jar"
        jar_path.write_bytes(b"fake jar")

        with patch("hermeto.core.package_managers.maven.main.must_match_any_checksum"):
            _download_maven_artifacts(tmp_path, [artifact])

        assert mock_download.call_count >= 1


class TestRewriteUrlForProxy:
    def test_basic_rewrite(self) -> None:
        result = _rewrite_url_for_proxy(
            "https://repo.maven.apache.org/maven2/org/example/foo/1.0/foo-1.0.jar",
            "https://proxy.corp.example.com/maven",
        )
        assert (
            result == "https://proxy.corp.example.com/maven/maven2/org/example/foo/1.0/foo-1.0.jar"
        )

    def test_proxy_with_trailing_slash(self) -> None:
        result = _rewrite_url_for_proxy(
            "https://repo.maven.apache.org/maven2/foo.jar",
            "https://proxy.example.com/",
        )
        assert result == "https://proxy.example.com/maven2/foo.jar"

    def test_proxy_without_trailing_slash(self) -> None:
        result = _rewrite_url_for_proxy(
            "https://repo.maven.apache.org/maven2/foo.jar",
            "https://proxy.example.com",
        )
        assert result == "https://proxy.example.com/maven2/foo.jar"


class TestSbomComponentsWithProxy:
    def test_no_proxy_no_external_refs(self) -> None:
        components = _generate_sbom_components([make_artifact()])
        assert components[0].external_references is None

    def test_proxy_adds_external_ref(self) -> None:
        components = _generate_sbom_components(
            [make_artifact()], proxy_url="https://proxy.example.com"
        )
        refs = components[0].external_references
        assert refs is not None
        assert len(refs) == 1
        assert refs[0].url == "https://proxy.example.com"
        assert refs[0].type == "distribution"


class TestProxyAuth:
    @patch("hermeto.core.package_managers.maven.main._async_download_optional_files")
    @patch("hermeto.core.package_managers.maven.main.async_download_files")
    @patch("hermeto.core.package_managers.maven.main.get_config")
    def test_proxy_auth_forwarded_to_downloads(
        self,
        mock_config: MagicMock,
        mock_download: MagicMock,
        mock_optional: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_config.return_value.runtime.concurrency_limit = 5
        artifact = make_artifact()

        artifact_dir = tmp_path / "org" / "example" / "foo" / "1.0.0"
        artifact_dir.mkdir(parents=True)
        jar_path = artifact_dir / "foo-1.0.0.jar"
        jar_path.write_bytes(b"fake jar")

        proxy_auth = aiohttp.BasicAuth("user", "pass")

        with patch("hermeto.core.package_managers.maven.main.must_match_any_checksum"):
            _download_maven_artifacts(tmp_path, [artifact], "https://proxy.example.com", proxy_auth)

        for call in mock_download.call_args_list:
            assert call.kwargs.get("auth") is proxy_auth or call[1].get("auth") is proxy_auth


class TestSettingsXmlTemplate:
    def test_valid_xml_with_required_elements(self) -> None:
        ET.fromstring(SETTINGS_XML_TEMPLATE)  # noqa: S314
        assert "localRepository" in SETTINGS_XML_TEMPLATE
        assert "<mirrorOf>*</mirrorOf>" in SETTINGS_XML_TEMPLATE
        assert "${output_dir}" in SETTINGS_XML_TEMPLATE
