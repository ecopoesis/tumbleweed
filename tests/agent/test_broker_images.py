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
