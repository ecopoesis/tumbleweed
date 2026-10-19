"""Fixtures of the voice tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.voice.stub_server import StubServer


@pytest.fixture
def stub() -> Iterator[StubServer]:
    """A whisper-server stand-in on the loopback, closed when the test ends."""
    server = StubServer()
    server.start()
    yield server
    server.close()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home with the configuration directory made, as `environ_for` and `write_config` use it."""
    (tmp_path / "config" / "tumbleweed").mkdir(parents=True, mode=0o700)
    return tmp_path
