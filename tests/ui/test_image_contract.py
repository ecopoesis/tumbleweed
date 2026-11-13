"""The resolver contract, run on every implementation there is in this step: the renderer's fake
and a real directory. S19's resolver adds its own subclass of `ResolverContract` here or next to
the broker's tests."""

from pathlib import Path

import pytest

from tests.ui.image_contract import DirfdWorkspace, ResolverContract, Workspace
from tests.ui.image_support import FakeResolver


class TestTheFakeResolver(ResolverContract):
    def make_workspace(self, tmp_path: Path) -> Workspace:
        return FakeResolver()


class TestADirectoryResolver(ResolverContract):
    def make_workspace(self, tmp_path: Path) -> Workspace:
        return DirfdWorkspace(tmp_path)


def test_the_directory_opener_closes_each_descriptor_once_when_a_directory_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reference S19 copies: a failure in the middle of the walk used to close the parent
    # twice (once on the way down, once by the caller), which can close another thread's file.
    import os
    import stat

    from tests.ui.image_contract import ref
    from tumbleweed.core.types import ByteCount
    from tumbleweed.ui.images import ImageNotAllowedError

    workspace = DirfdWorkspace(tmp_path)
    workspace.add_file("sub/inside.png", b"x")
    closed: list[int] = []
    real_close, real_fstat = os.close, os.fstat

    def recording_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def swapped(fd: int) -> os.stat_result:
        seen = real_fstat(fd)
        if stat.S_ISDIR(seen.st_mode):  # the directory is not the one that was looked at
            fields = list(seen)
            fields[1] += 1  # st_ino
            return os.stat_result(fields)
        return seen

    monkeypatch.setattr(os, "close", recording_close)
    monkeypatch.setattr(os, "fstat", swapped)

    with pytest.raises(ImageNotAllowedError, match="swapped"):
        workspace.resolver().read(ref("sub/inside.png"), ByteCount(100))

    assert len(closed) == len(set(closed))  # no descriptor was closed twice
    assert len(closed) == 2  # the one it was reading from and the one it opened


def test_the_directory_opener_does_not_leak_a_descriptor_when_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #137: the walk the broker-backed resolver was modelled on used to leak the child directory
    # when looking at it raised.
    import os
    import stat

    from tests.ui.image_contract import ref
    from tumbleweed.core.types import ByteCount

    workspace = DirfdWorkspace(tmp_path)
    workspace.add_file("sub/inside.png", b"x")
    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close, real_fstat = os.open, os.close, os.fstat

    def recording_open(path: str, flags: int, *args: int, **kwargs: int) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        opened.append(fd)
        return fd

    def recording_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def failing(fd: int) -> os.stat_result:
        if stat.S_ISDIR(real_fstat(fd).st_mode) and len(opened) > 1:
            raise OSError(5, "boom")
        return real_fstat(fd)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "close", recording_close)
    monkeypatch.setattr(os, "fstat", failing)

    with pytest.raises(OSError, match="boom"):
        workspace.resolver().read(ref("sub/inside.png"), ByteCount(100))

    assert sorted(opened) == sorted(closed)  # every descriptor that was opened was closed
