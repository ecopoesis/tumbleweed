"""A fake peer-credentials provider for the IPC tests."""

from tumbleweed.ipc.peercred import SupportsGetsockopt, Uid, current_uid


class FixedUid:
    """A peer-credentials provider that says every peer runs as `uid`."""

    def __init__(self, uid: int) -> None:
        self.uid = Uid(uid)
        self.asked = 0

    def peer_uid(self, sock: SupportsGetsockopt, /) -> Uid:
        self.asked += 1
        return self.uid


def other_uid() -> int:
    return int(current_uid()) + 1
