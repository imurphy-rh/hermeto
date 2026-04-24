# SPDX-License-Identifier: GPL-3.0-only
import re
from urllib.parse import urlparse

from hermeto.core.errors import PackageRejected

JAVA_TO_PYTHON_CHECKSUM_ALGORITHMS = {
    "SHA-256": "sha256",
    "SHA-1": "sha1",
    "SHA-512": "sha512",
    "SHA-224": "sha224",
    "SHA-384": "sha384",
    "MD5": "md5",
}

_EXPECTED_HEX_LENGTHS: dict[str, int] = {
    "sha256": 64,
    "sha1": 40,
    "sha512": 128,
    "sha224": 56,
    "sha384": 96,
    "md5": 32,
}

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def get_checksum_algorithm(java_algorithm: str) -> str:
    """Convert Java checksum algorithm name to Python hashlib algorithm name."""
    algorithm = JAVA_TO_PYTHON_CHECKSUM_ALGORITHMS.get(java_algorithm)
    if not algorithm:
        raise PackageRejected(
            f"Unsupported checksum algorithm: {java_algorithm}",
            solution=f"Supported checksum algorithms: {','.join(JAVA_TO_PYTHON_CHECKSUM_ALGORITHMS.keys())}",
        )

    return algorithm


def validate_checksum_format(algorithm: str, checksum: str) -> None:
    """Validate that a checksum is hex-encoded with the correct length."""
    if not _HEX_RE.match(checksum):
        raise PackageRejected(
            f"Checksum is not valid hexadecimal: {checksum!r}",
            solution="The lockfile checksum must be a hex-encoded digest. "
            "Regenerate your lockfile with: mvn io.github.chains-project:maven-lockfile:generate",
        )

    expected_len = _EXPECTED_HEX_LENGTHS.get(algorithm)
    if expected_len and len(checksum) != expected_len:
        raise PackageRejected(
            f"Checksum length {len(checksum)} does not match expected {expected_len} for {algorithm}",
            solution="The lockfile checksum appears corrupted. "
            "Regenerate your lockfile with: mvn io.github.chains-project:maven-lockfile:generate",
        )


def validate_artifact_url(url: str) -> None:
    """Validate that an artifact URL uses HTTPS and contains no embedded credentials."""
    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise PackageRejected(
            f"Artifact URL must use https:// scheme, got {parsed.scheme!r}: {url}",
            solution="Update the lockfile to use HTTPS URLs for all artifact repositories.",
        )

    if parsed.username or parsed.password:
        raise PackageRejected(
            "Artifact URL must not contain embedded credentials",
            solution="Use .netrc or environment variables for repository authentication "
            "instead of embedding credentials in URLs.",
        )


def derive_repository_id(url: str) -> str:
    """Derive a repository ID from the given URL."""
    hostname_to_id = {
        "repo1.maven.org": "central",
        "repo.maven.apache.org": "central",
        "central.maven.org": "central",
        "oss.sonatype.org": "sonatype",
        "s01.oss.sonatype.org": "sonatype",
        "repository.jboss.org": "jboss",
        "repo.spring.io": "spring",
    }

    parsed_url = urlparse(url)
    hostname = parsed_url.hostname
    if not hostname:
        return "unknown"

    return hostname_to_id.get(hostname, "unknown")


def derive_pom_filename(artifact_id: str, version: str) -> str:
    """Derive a POM filename from the given artifact ID and version."""
    return f"{artifact_id}-{version}.pom"
