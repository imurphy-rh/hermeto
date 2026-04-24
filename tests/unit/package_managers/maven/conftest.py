# SPDX-License-Identifier: GPL-3.0-only
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from hermeto.core.models.input import Request
from hermeto.core.models.output import RequestOutput
from hermeto.core.package_managers.maven.main import DEFAULT_LOCKFILE, fetch_maven_source
from hermeto.core.package_managers.maven.models import MavenArtifact

TEST_DATA_DIR = Path(__file__).parent / "test_data"


def make_artifact_data(**overrides: Any) -> dict[str, Any]:
    """Build a valid Maven artifact dict with sensible defaults."""
    base: dict[str, Any] = {
        "groupId": "org.example",
        "artifactId": "foo",
        "version": "1.0.0",
        "checksumAlgorithm": "SHA-256",
        "checksum": "a" * 64,
        "resolved": "https://repo.maven.apache.org/maven2/org/example/foo/1.0.0/foo-1.0.0.jar",
        "repositoryId": "central",
        "scope": "compile",
    }
    base.update(overrides)
    return base


def make_artifact(**overrides: Any) -> MavenArtifact:
    """Build a MavenArtifact with sensible defaults."""
    return MavenArtifact(make_artifact_data(**overrides))


def minimal_lockfile_data(**overrides: Any) -> dict[str, Any]:
    """Build a minimal lockfile dict with one dependency."""
    base: dict[str, Any] = {
        "groupId": "org.example",
        "artifactId": "my-app",
        "version": "1.0.0",
        "dependencies": [make_artifact_data()],
    }
    base.update(overrides)
    return base


def write_lockfile(project_dir: Path, data: dict[str, Any]) -> Path:
    """Write a lockfile.json to the given directory."""
    lockfile_path = project_dir / DEFAULT_LOCKFILE
    lockfile_path.write_text(json.dumps(data))
    return lockfile_path


@pytest.fixture
def maven_fetch_result(tmp_path: Path) -> RequestOutput:
    """Set up a minimal Maven project and return the fetch result."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    write_lockfile(source_dir, minimal_lockfile_data())

    request = Request(
        source_dir=source_dir,
        output_dir=output_dir,
        packages=[{"type": "x-maven", "path": "."}],
    )

    with patch("hermeto.core.package_managers.maven.main._download_maven_artifacts"):
        return fetch_maven_source(request)
