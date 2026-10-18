"""Every store a history test opens is closed when the test ends."""

from collections.abc import Iterator

import pytest

from tests.history.support import closing_every_store


@pytest.fixture(autouse=True)
def _close_every_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with closing_every_store(monkeypatch):
        yield
