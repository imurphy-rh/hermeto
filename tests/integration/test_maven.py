# SPDX-License-Identifier: GPL-3.0-only
import logging
import os
from pathlib import Path

import pytest

from . import utils

log = logging.getLogger(__name__)

_LOCAL_TEST_REPO = os.getenv(
    "HERMETO_MAVEN_TEST_REPO",
    "file:///Users/imurphy/projects/konflux-maven/test-repo",
)


@pytest.mark.parametrize(
    "test_params",
    [
        pytest.param(
            utils.TestParameters(
                branch="x-maven/e2e",
                packages=({"path": ".", "type": "x-maven"},),
                repo_url=_LOCAL_TEST_REPO,
                check_output=False,
                check_deps_checksums=False,
                expected_exit_code=0,
                expected_output="All dependencies fetched successfully",
            ),
            id="x_maven_e2e",
        ),
        pytest.param(
            utils.TestParameters(
                branch="x-maven/missing-lockfile",
                packages=({"path": ".", "type": "x-maven"},),
                repo_url=_LOCAL_TEST_REPO,
                check_output=False,
                check_deps_checksums=False,
                expected_exit_code=2,
                expected_output="lockfile.json",
            ),
            id="x_maven_missing_lockfile",
        ),
        pytest.param(
            utils.TestParameters(
                branch="x-maven/missing-lockfile",
                packages=({"path": ".", "type": "x-maven"},),
                repo_url=_LOCAL_TEST_REPO,
                global_flags=["--mode", "permissive"],
                check_output=False,
                check_deps_checksums=False,
                expected_exit_code=0,
                expected_output="All dependencies fetched successfully",
            ),
            id="x_maven_missing_lockfile_permissive",
        ),
    ],
)
def test_maven_packages(
    test_params: utils.TestParameters,
    hermeto_image: utils.HermetoImage,
    tmp_path: Path,
    test_repo_dir: Path,
    test_data_dir: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Integration tests for Maven package manager."""
    test_case = request.node.callspec.id

    utils.fetch_deps_and_check_output(
        tmp_path, test_case, test_params, test_repo_dir, test_data_dir, hermeto_image
    )
