"""The broker-backed resolver passes the resolver contract unchanged (S19, D26 (7))."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import final

import pytest

from tests.agent.broker_thread import BrokerThread
from tests.ui.image_contract import ResolverContract, Workspace
from tumbleweed.agent.broker_images import BrokerImageResolver
from tumbleweed.broker.client import BrokerClient
from tumbleweed.core.ids import TaskId
from tumbleweed.ui.images import ImageResolver
from tumbleweed.ui.spec import ImageRef  # noqa: F401  (documentation of what is resolved)


@final
class BrokerWorkspace:
    """A task's workspace in a real broker: files are placed in its directory on disk."""

    def __init__(self, broker: BrokerThread) -> None:
        self._broker = broker
        self._link = broker.link()

        async def start(client: BrokerClient) -> TaskId:
            return (await client.start_task()).task

        self.task = self._link.call_blocking(start, deadline=10.0)
        self.root = broker.rig.workspaces / str(self.task)

    def _where(self, rel: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def add_file(self, rel: str, data: bytes) -> None:
        self._where(rel).write_bytes(data)

    def add_symlink(self, rel: str, target: str) -> None:
        self._where(rel).symlink_to(target)

    def add_dir(self, rel: str) -> None:
        self._where(rel).mkdir()

    def add_fifo(self, rel: str) -> None:
        os.mkfifo(self._where(rel))

    def add_outside_file(self, name: str, data: bytes) -> None:
        (self.root.parent / name).write_bytes(data)

    def resolver(self) -> ImageResolver:
        return BrokerImageResolver(self._link, self.task)


class TestTheBrokerResolver(ResolverContract):
    @pytest.fixture(autouse=True)
    def broker(self) -> Iterator[BrokerThread]:
        thread = BrokerThread()
        self.thread = thread
        yield thread
        thread.stop()

    def make_workspace(self, tmp_path: Path) -> Workspace:
        return BrokerWorkspace(self.thread)


def test_a_broker_that_never_answers_holds_the_reader_for_the_deadline_and_no_longer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #109: the resolver waits at most its deadline.
    import asyncio
    import time

    from tests.ui.image_contract import ref
    from tumbleweed.core.types import ByteCount
    from tumbleweed.ui.images import ImageError

    async def never(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(BrokerClient, "read_workspace_file", never)
    broker = BrokerThread()
    try:
        workspace = BrokerWorkspace(broker)
        resolver = BrokerImageResolver(workspace._link, workspace.task, deadline=0.3)  # noqa: SLF001
        began = time.monotonic()

        with pytest.raises(ImageError):
            resolver.read(ref("a.png"), ByteCount(100))

        assert time.monotonic() - began < 5.0
    finally:
        broker.stop()


def test_bytes_over_the_limit_are_refused_even_if_the_broker_sent_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from tests.ui.image_contract import ref
    from tumbleweed.core.types import ByteCount
    from tumbleweed.ipc.broker_messages import FileContent
    from tumbleweed.ui.images import ImageTooLargeError

    async def too_much(*args: object, **kwargs: object) -> FileContent:
        return FileContent(kind="file_content", size=50, data=base64.b64encode(b"x" * 50).decode())

    monkeypatch.setattr(BrokerClient, "read_workspace_file", too_much)
    broker = BrokerThread()
    try:
        workspace = BrokerWorkspace(broker)

        with pytest.raises(ImageTooLargeError):
            workspace.resolver().read(ref("a.png"), ByteCount(10))
    finally:
        broker.stop()
