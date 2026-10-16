"""Fixtures for the broker tests."""

from pathlib import Path

import pytest

from tests.broker.support import World, make_world


@pytest.fixture
def world(tmp_path: Path) -> World:
    # pytest's tmp_path is already canonical: on macOS it is under /private/var, not /var.
    return make_world(tmp_path)


@pytest.fixture(scope="module")
def shared_world(tmp_path_factory: pytest.TempPathFactory) -> World:
    """One world for a whole module, for tests that only read it (many parametrized checks)."""
    return make_world(tmp_path_factory.mktemp("shared", numbered=True).resolve())
