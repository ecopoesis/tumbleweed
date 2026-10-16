"""Fixtures for the sandbox tests: a workspace directory and its opened descriptor."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.sandbox.support import Opened


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "workspace"
    directory.mkdir()
    return directory


@pytest.fixture
def workspace(workspace_dir: Path) -> Iterator[Opened]:
    handle = Opened(workspace_dir)
    try:
        yield handle
    finally:
        handle.close()
