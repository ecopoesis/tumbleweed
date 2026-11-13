"""The `ImageResolver` of a task's workspace, through the broker (S19, D26 (7), #109).

The renderer's decode worker calls `read` on its own thread; the resolver asks the broker for the
file (`read_workspace_file`), which opens it component by component without following a symlink
(I11), and waits at most `READ_DEADLINE` for the answer, so a broker that hangs holds a worker for
that long and no longer. The ref is passed as the validated workspace-relative name it is: nothing
here resolves, expands or opens it. A resolver belongs to the task that made the instance: once the
task has ended the broker refuses (`unknown_task`), which is how an ended task's images stop.

The wire carries at most `MAX_FILE_BYTES` (512 KiB) of a file, so a larger image is refused as too
large whatever the renderer's own limit is.
"""

import base64
import binascii
from typing import Final, final

from tumbleweed.agent.broker_link import BrokerLink, BrokerUnavailableError
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.core.ids import TaskId
from tumbleweed.core.types import ByteCount, WorkspaceRelPath
from tumbleweed.ipc.broker_messages import MAX_FILE_BYTES
from tumbleweed.ui.images import (
    ImageError,
    ImageNotAllowedError,
    ImageNotFoundError,
    ImageTooLargeError,
)
from tumbleweed.ui.spec import ImageRef

READ_DEADLINE: Final = 10.0


async def _told_apart(
    client: BrokerClient, task: TaskId, path: str, refusal: BrokerRefused
) -> BrokerRefused:
    parent, _, name = path.rpartition("/")
    try:
        listing = await client.list_workspace(task, WorkspaceRelPath(parent) if parent else None)
    except BrokerRefused as listing_refusal:
        if listing_refusal.reason == "unsafe":
            return BrokerRefused("unsafe", listing_refusal.message)
        return BrokerRefused("not_found", listing_refusal.message)
    if any(entry.name == name for entry in listing.entries):
        return refusal
    return BrokerRefused("not_found", refusal.message)


@final
class BrokerImageResolver:
    def __init__(self, link: BrokerLink, task: TaskId, *, deadline: float = READ_DEADLINE) -> None:
        self._link = link
        self._task = task
        self._deadline = deadline

    def read(self, ref: ImageRef, limit: ByteCount) -> bytes:
        task, path = self._task, ref.path
        wanted = max(1, min(int(limit), MAX_FILE_BYTES))

        async def work(client: BrokerClient) -> str:
            try:
                return (await client.read_workspace_file(task, path, max_bytes=wanted)).data
            except BrokerRefused as refusal:
                if refusal.reason != "invalid_request":
                    raise
                # "Wrong kind" covers a directory where the file should be (not allowed) and a
                # file where a directory should be (not there): look at the parent to tell.
                raise await _told_apart(client, task, str(path), refusal) from None

        try:
            encoded = self._link.call_blocking(work, deadline=self._deadline)
        except BrokerRefused as refusal:
            match refusal.reason:
                case "not_found":
                    raise ImageNotFoundError("the broker found no such file") from None
                case "too_large":
                    raise ImageTooLargeError("the file is over the limit") from None
                case "unsafe" | "denied" | "invalid_request" | "unknown_task" | "not_granted":
                    raise ImageNotAllowedError("the broker refused the file") from None
                case _:
                    raise ImageError("the broker could not read the file") from None
        except BrokerUnavailableError:
            raise ImageError("the broker could not be reached") from None
        try:
            data = base64.b64decode(encoded, validate=True)
        except binascii.Error:
            raise ImageError("the broker's answer could not be read") from None
        if len(data) > limit:  # the broker holds to the limit it was given; this is the contract
            raise ImageTooLargeError("over the limit")
        return data
