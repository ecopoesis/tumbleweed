"""Fixtures of the voice tests."""

from collections.abc import Iterator

import pytest

from tests.voice.stub_server import StubServer


@pytest.fixture
def stub() -> Iterator[StubServer]:
    """A whisper-server stand-in on the loopback, closed when the test ends."""
    server = StubServer()
    server.start()
    yield server
    server.close()
