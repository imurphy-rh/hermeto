# SPDX-License-Identifier: GPL-3.0-only
import pytest

from hermeto.core.errors import InvalidChecksum, PackageRejected
from hermeto.core.package_managers.maven.utils import (
    derive_pom_filename,
    derive_repository_id,
    get_checksum_algorithm,
    validate_artifact_url,
    validate_checksum_format,
)


class TestGetChecksumAlgorithm:
    @pytest.mark.parametrize(
        "java_name, expected",
        [
            ("SHA-256", "sha256"),
            ("SHA-1", "sha1"),
            ("SHA-512", "sha512"),
            ("SHA-224", "sha224"),
            ("SHA-384", "sha384"),
            ("MD5", "md5"),
        ],
    )
    def test_supported_algorithms(self, java_name: str, expected: str) -> None:
        assert get_checksum_algorithm(java_name) == expected

    def test_unsupported_algorithm(self) -> None:
        with pytest.raises(PackageRejected, match="Unsupported checksum algorithm"):
            get_checksum_algorithm("SHA-3")

    def test_empty_string(self) -> None:
        with pytest.raises(PackageRejected, match="Unsupported checksum algorithm"):
            get_checksum_algorithm("")


class TestValidateChecksumFormat:
    def test_valid_sha256(self) -> None:
        validate_checksum_format("sha256", "a" * 64)

    def test_valid_sha1(self) -> None:
        validate_checksum_format("sha1", "0123456789abcdef" * 2 + "01234567")

    def test_valid_sha512(self) -> None:
        validate_checksum_format("sha512", "ab" * 64)

    def test_valid_md5(self) -> None:
        validate_checksum_format("md5", "abcdef0123456789" * 2)

    def test_valid_mixed_case_hex(self) -> None:
        validate_checksum_format("sha256", "aAbBcCdD" * 8)

    def test_non_hex_characters(self) -> None:
        with pytest.raises(InvalidChecksum):
            validate_checksum_format("sha256", "g" * 64)

    def test_base64_rejected(self) -> None:
        with pytest.raises(InvalidChecksum):
            validate_checksum_format("sha256", "YWJj+/==" + "a" * 56)

    def test_wrong_length_sha256(self) -> None:
        with pytest.raises(InvalidChecksum, match="length"):
            validate_checksum_format("sha256", "a" * 32)

    def test_wrong_length_sha1(self) -> None:
        with pytest.raises(InvalidChecksum, match="length"):
            validate_checksum_format("sha1", "a" * 64)

    def test_empty_string(self) -> None:
        with pytest.raises(InvalidChecksum):
            validate_checksum_format("sha256", "")


class TestValidateArtifactUrl:
    def test_https_accepted(self) -> None:
        validate_artifact_url("https://repo.maven.apache.org/maven2/foo/bar/1.0/bar-1.0.jar")

    def test_http_rejected(self) -> None:
        with pytest.raises(PackageRejected, match="https://"):
            validate_artifact_url("http://repo.maven.apache.org/maven2/foo/bar.jar")

    def test_file_rejected(self) -> None:
        with pytest.raises(PackageRejected, match="https://"):
            validate_artifact_url("file:///tmp/foo.jar")

    def test_ftp_rejected(self) -> None:
        with pytest.raises(PackageRejected, match="https://"):
            validate_artifact_url("ftp://repo.example.com/foo.jar")

    def test_no_scheme_rejected(self) -> None:
        with pytest.raises(PackageRejected):
            validate_artifact_url("repo.maven.apache.org/maven2/foo.jar")

    def test_credentials_rejected(self) -> None:
        with pytest.raises(PackageRejected, match="credentials"):
            validate_artifact_url("https://user:pass@repo.maven.apache.org/maven2/foo.jar")

    def test_username_only_rejected(self) -> None:
        with pytest.raises(PackageRejected, match="credentials"):
            validate_artifact_url("https://user@repo.maven.apache.org/maven2/foo.jar")


class TestDeriveRepositoryId:
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://repo.maven.apache.org/maven2/foo/bar.jar", "central"),
            ("https://repo1.maven.org/maven2/foo/bar.jar", "central"),
            ("https://central.maven.org/maven2/foo/bar.jar", "central"),
            ("https://oss.sonatype.org/content/groups/public/foo.jar", "sonatype"),
            ("https://s01.oss.sonatype.org/content/groups/public/foo.jar", "sonatype"),
            ("https://repository.jboss.org/nexus/foo.jar", "jboss"),
            ("https://repo.spring.io/release/foo.jar", "spring"),
            ("https://my-company.example.com/repo/foo.jar", "unknown"),
        ],
    )
    def test_known_and_unknown_hosts(self, url: str, expected: str) -> None:
        assert derive_repository_id(url) == expected

    def test_no_hostname(self) -> None:
        assert derive_repository_id("urn:example:foo") == "unknown"


class TestDerivePomFilename:
    def test_simple(self) -> None:
        assert derive_pom_filename("commons-io", "2.16.1") == "commons-io-2.16.1.pom"

    def test_with_dashes(self) -> None:
        assert (
            derive_pom_filename("maven-compiler-plugin", "3.13.0")
            == "maven-compiler-plugin-3.13.0.pom"
        )
