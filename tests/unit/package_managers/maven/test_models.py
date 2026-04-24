# SPDX-License-Identifier: GPL-3.0-only
import json
from pathlib import Path

import pytest

from hermeto.core.errors import InvalidLockfileFormat, PackageRejected, UnexpectedFormat
from hermeto.core.package_managers.maven.models import (
    MavenArtifact,
    MavenLockfile,
    parse_maven_boms,
    parse_maven_dependencies,
    parse_maven_plugins,
)
from tests.unit.package_managers.maven.conftest import TEST_DATA_DIR, make_artifact_data


class TestMavenLockfile:
    def test_from_file_minimal(self) -> None:
        lockfile = MavenLockfile.from_file(TEST_DATA_DIR / "lockfile_minimal.json")
        assert lockfile.data["groupId"] == "org.example"
        assert lockfile.data["artifactId"] == "my-app"
        assert lockfile.data["version"] == "1.0.0"
        assert len(lockfile.data["dependencies"]) == 1

    def test_from_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            MavenLockfile.from_file(tmp_path / "nonexistent.json")

    def test_from_file_malformed_json(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "lockfile.json"
        bad_file.write_text("{invalid json!!!")
        with pytest.raises(InvalidLockfileFormat, match="invalid JSON"):
            MavenLockfile.from_file(bad_file)

    def test_from_file_missing_groupId(self, tmp_path: Path) -> None:
        lockfile = tmp_path / "lockfile.json"
        lockfile.write_text(json.dumps({"artifactId": "foo", "version": "1.0"}))
        with pytest.raises(InvalidLockfileFormat, match="groupId"):
            MavenLockfile.from_file(lockfile)

    def test_from_file_missing_multiple_fields(self, tmp_path: Path) -> None:
        lockfile = tmp_path / "lockfile.json"
        lockfile.write_text(json.dumps({"version": "1.0"}))
        with pytest.raises(InvalidLockfileFormat, match="groupId.*artifactId"):
            MavenLockfile.from_file(lockfile)

    def test_from_file_empty_deps(self) -> None:
        lockfile = MavenLockfile.from_file(TEST_DATA_DIR / "lockfile_empty_deps.json")
        assert lockfile.data["dependencies"] == []

    def test_path_is_stored(self) -> None:
        path = TEST_DATA_DIR / "lockfile_minimal.json"
        lockfile = MavenLockfile.from_file(path)
        assert lockfile.path == path

    def test_lockfile_version_1_accepted(self, tmp_path: Path) -> None:
        lockfile = tmp_path / "lockfile.json"
        data = {"groupId": "g", "artifactId": "a", "version": "1", "lockFileVersion": 1}
        lockfile.write_text(json.dumps(data))
        result = MavenLockfile.from_file(lockfile)
        assert result.data["lockFileVersion"] == 1

    def test_lockfile_version_absent_accepted(self, tmp_path: Path) -> None:
        lockfile = tmp_path / "lockfile.json"
        data = {"groupId": "g", "artifactId": "a", "version": "1"}
        lockfile.write_text(json.dumps(data))
        MavenLockfile.from_file(lockfile)

    def test_lockfile_version_2_rejected(self, tmp_path: Path) -> None:
        lockfile = tmp_path / "lockfile.json"
        data = {"groupId": "g", "artifactId": "a", "version": "1", "lockFileVersion": 2}
        lockfile.write_text(json.dumps(data))
        with pytest.raises(InvalidLockfileFormat, match="lockFileVersion"):
            MavenLockfile.from_file(lockfile)


class TestMavenArtifact:
    def test_basic_construction(self) -> None:
        data = make_artifact_data()
        artifact = MavenArtifact(data)
        assert artifact.url == data["resolved"]
        assert artifact.group_id == "org.example"
        assert artifact.artifact_id == "foo"
        assert artifact.version == "1.0.0"
        assert artifact.checksum_algorithm == "SHA-256"
        assert artifact.checksum == "a" * 64
        assert artifact.algorithm == "sha256"

    def test_missing_resolved_field(self) -> None:
        data = make_artifact_data()
        del data["resolved"]
        with pytest.raises(UnexpectedFormat, match="resolved"):
            MavenArtifact(data)

    def test_missing_checksum_field(self) -> None:
        data = make_artifact_data()
        del data["checksum"]
        del data["checksumAlgorithm"]
        with pytest.raises(UnexpectedFormat, match="checksumAlgorithm.*checksum"):
            MavenArtifact(data)

    def test_missing_all_required_fields(self) -> None:
        with pytest.raises(UnexpectedFormat, match="missing required fields"):
            MavenArtifact({})

    def test_unsupported_algorithm_in_artifact(self) -> None:
        data = make_artifact_data(checksumAlgorithm="SHA-3")
        with pytest.raises(PackageRejected, match="Unsupported checksum algorithm"):
            MavenArtifact(data)

    def test_artifact_hint_uses_artifactId(self) -> None:
        data = {"artifactId": "my-lib"}
        with pytest.raises(UnexpectedFormat, match="my-lib"):
            MavenArtifact(data)

    def test_artifact_hint_falls_back_to_groupId(self) -> None:
        data = {"groupId": "org.example"}
        with pytest.raises(UnexpectedFormat, match="org.example"):
            MavenArtifact(data)

    def test_filename(self) -> None:
        data = make_artifact_data()
        artifact = MavenArtifact(data)
        assert artifact.filename == "foo-1.0.0.jar"

    def test_filename_pom(self) -> None:
        data = make_artifact_data(
            resolved="https://repo.maven.apache.org/maven2/org/example/foo/1.0.0/foo-1.0.0.pom"
        )
        artifact = MavenArtifact(data)
        assert artifact.filename == "foo-1.0.0.pom"

    def test_artifact_relative_dir(self) -> None:
        data = make_artifact_data()
        artifact = MavenArtifact(data)
        assert artifact.artifact_relative_dir == Path("org/example/foo/1.0.0")

    def test_artifact_relative_dir_nested_group(self) -> None:
        data = make_artifact_data(groupId="org.apache.maven.plugins")
        artifact = MavenArtifact(data)
        assert artifact.artifact_relative_dir == Path("org/apache/maven/plugins/foo/1.0.0")

    def test_userdict_access(self) -> None:
        data = make_artifact_data(scope="test")
        artifact = MavenArtifact(data)
        assert artifact["scope"] == "test"
        assert artifact.get("scope") == "test"
        assert artifact.get("nonexistent", "default") == "default"


class TestParseMavenDependencies:
    def test_minimal_lockfile(self) -> None:
        lockfile = MavenLockfile.from_file(TEST_DATA_DIR / "lockfile_minimal.json")
        deps = parse_maven_dependencies(lockfile)
        assert len(deps) == 1
        assert deps[0].artifact_id == "commons-io"

    def test_nested_deps(self) -> None:
        lockfile = MavenLockfile.from_file(TEST_DATA_DIR / "lockfile_nested_deps.json")
        deps = parse_maven_dependencies(lockfile)
        assert len(deps) == 3
        artifact_ids = [d.artifact_id for d in deps]
        assert "spring-core" in artifact_ids
        assert "spring-jcl" in artifact_ids
        assert "commons-logging" in artifact_ids

    def test_empty_deps(self) -> None:
        lockfile = MavenLockfile.from_file(TEST_DATA_DIR / "lockfile_empty_deps.json")
        deps = parse_maven_dependencies(lockfile)
        assert deps == []

    def test_missing_deps_key(self) -> None:
        lockfile = MavenLockfile(Path("/fake"), {"groupId": "g", "artifactId": "a", "version": "1"})
        deps = parse_maven_dependencies(lockfile)
        assert deps == []


class TestParseMavenPlugins:
    def test_with_plugins(self) -> None:
        lockfile = MavenLockfile.from_file(TEST_DATA_DIR / "lockfile_with_plugins.json")
        plugins = parse_maven_plugins(lockfile)
        assert len(plugins) == 2
        artifact_ids = [p.artifact_id for p in plugins]
        assert "maven-compiler-plugin" in artifact_ids
        assert "maven-plugin-api" in artifact_ids

    def test_no_plugins(self) -> None:
        lockfile = MavenLockfile.from_file(TEST_DATA_DIR / "lockfile_minimal.json")
        plugins = parse_maven_plugins(lockfile)
        assert plugins == []


class TestParseMavenBoms:
    def test_with_boms(self) -> None:
        lockfile = MavenLockfile.from_file(TEST_DATA_DIR / "lockfile_with_boms.json")
        deps = parse_maven_dependencies(lockfile)
        boms = parse_maven_boms(deps)
        assert len(boms) == 1
        assert boms[0].artifact_id == "spring-boot-dependencies"

    def test_no_boms(self) -> None:
        lockfile = MavenLockfile.from_file(TEST_DATA_DIR / "lockfile_minimal.json")
        deps = parse_maven_dependencies(lockfile)
        boms = parse_maven_boms(deps)
        assert boms == []

    def test_missing_fields_in_artifact(self) -> None:
        lockfile = MavenLockfile.from_file(TEST_DATA_DIR / "lockfile_missing_fields.json")
        with pytest.raises(UnexpectedFormat, match="missing required fields"):
            parse_maven_dependencies(lockfile)
