# SPDX-License-Identifier: GPL-3.0-only
import json
import logging
from collections import UserDict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class MavenLockfile:
    """Class representing JSON lockfile for Maven."""

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        """Initialize a MavenLockfile object."""
        self.path = path
        self.data = data

    @classmethod
    def from_file(cls, path: Path) -> "MavenLockfile":
        """Create a MavenLockfile object from the provided path."""
        with path.open() as f:
            data = json.load(f)

        return cls(path, data)


class MavenArtifact(UserDict):
    """Class representing a Maven artifact to download."""

    url: str
    group_id: str
    artifact_id: str
    version: str
    checksum_algorithm: str
    checksum: str

    def __init__(self, data: dict[str, Any]) -> None:
        """Initialize a MavenArtifact object."""
        self.url = data["resolved"]
        self.group_id = data["groupId"]
        self.artifact_id = data["artifactId"]
        self.version = data["version"]
        self.checksum_algorithm = data["checksumAlgorithm"]
        self.checksum = data["checksum"]
        super().__init__(data)

    @property
    def filename(self) -> str:
        """Get the filename of the artifact."""
        parsed_url = urlparse(self.url)
        return Path(parsed_url.path).name

    @property
    def artifact_relative_dir(self) -> Path:
        """Get the relative artifact directory."""
        group_dir = self.group_id.replace(".", "/")
        return Path(group_dir) / self.artifact_id / self.version


def _parse_dependency_tree(
    dependencies: list[MavenArtifact], children: list[dict[str, Any]]
) -> None:
    """Recursively parse dependency tree."""
    for child in children:
        dependencies.append(MavenArtifact(child))
        _parse_dependency_tree(dependencies, child.get("children", []))


def _parse_bom_tree(boms: list[MavenArtifact], children: list[dict[str, Any]]) -> None:
    """Recursively parse BOM tree."""
    for child in children:
        boms.append(MavenArtifact(child))
        _parse_bom_tree(boms, child.get("boms", []))


def parse_maven_dependencies(lockfile: MavenLockfile) -> list[MavenArtifact]:
    """Parse dependencies from the lockfile to a flat list."""
    dependencies: list[MavenArtifact] = []
    _parse_dependency_tree(dependencies, lockfile.data.get("dependencies", []))
    return dependencies


def parse_maven_plugins(lockfile: MavenLockfile) -> list[MavenArtifact]:
    """Parse plugins from the lockfile to a flat list."""
    plugins: list[MavenArtifact] = []
    dependencies: list[MavenArtifact] = []
    for plugin in lockfile.data.get("mavenPlugins", []):
        plugins.append(MavenArtifact(plugin))
        _parse_dependency_tree(dependencies, plugin.get("dependencies", []))

    return plugins + dependencies


def parse_maven_boms(dependencies: list[MavenArtifact]) -> list[MavenArtifact]:
    """Parse BOMs from the lockfile to a flat list."""
    boms: list[MavenArtifact] = []
    for dependency in dependencies:
        _parse_bom_tree(boms, dependency.get("boms", []))

    return boms
