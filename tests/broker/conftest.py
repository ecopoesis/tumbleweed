"""Fixtures for the broker tests."""

from pathlib import Path

import pytest

from tests.broker.support import World, make_world

# Autouse, and shared with the IPC tests: an error that asyncio had to report because nobody
# handled it fails the test that caused it (the daemon is asyncio all the way down).
from tests.ipc.conftest import no_error_is_lost_to_the_event_loop  # noqa: F401


@pytest.fixture
def world(tmp_path: Path) -> World:
    # pytest's tmp_path is already canonical: on macOS it is under /private/var, not /var.
    return make_world(tmp_path)


@pytest.fixture(scope="module")
def shared_world(tmp_path_factory: pytest.TempPathFactory) -> World:
    """One world for a whole module, for tests that only read it (many parametrized checks)."""
    return make_world(tmp_path_factory.mktemp("shared", numbered=True).resolve())
