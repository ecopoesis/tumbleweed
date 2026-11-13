"""The btrfs backend against a real loopback btrfs (S21). Needs root: the `btrfs` gate skips it
elsewhere, and S27's VM test covers the backend end to end."""

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.broker.rig import START
from tumbleweed.broker.config import SnapshotSettings
from tumbleweed.broker.fdio import FileKind
from tumbleweed.broker.paths import ResolvedPath, StoredPath
from tumbleweed.broker.snapshots import BackendKind, SnapshotKind, SnapshotManager, SnapshotRecord
from tumbleweed.core.ids import RunId, SnapshotId, TaskId

pytestmark = [pytest.mark.btrfs, pytest.mark.linux]


def run(*arguments: str) -> None:
    subprocess.run(arguments, check=True, capture_output=True)  # noqa: S603  (a test's own argv)


@pytest.fixture
def volume(tmp_path: Path) -> Iterator[Path]:
    image, mount = tmp_path / "image", tmp_path / "mnt"
    mount.mkdir()
    with image.open("wb") as file:
        file.truncate(256 * 1024 * 1024)
    run("mkfs.btrfs", "-q", str(image))
    run("mount", "-o", "loop", str(image), str(mount))
    try:
        run("btrfs", "subvolume", "create", str(mount / "data"))
        run("btrfs", "subvolume", "create", str(mount / "snapshots"))
        yield mount
    finally:
        run("umount", "-l", str(mount))


def stored(path: Path) -> StoredPath:
    return StoredPath.record(ResolvedPath.resolve(str(path)))


def test_a_folder_on_btrfs_is_snapshotted_read_only_and_put_back(volume: Path) -> None:
    folder = volume / "data" / "proj"
    (folder / "d").mkdir(parents=True)
    (folder / "a.txt").write_text("alpha")
    (folder / "d" / "b.txt").write_text("beta")
    manager = SnapshotManager(
        SnapshotSettings(btrfs_directory=volume / "snapshots"), volume / "s", None
    )
    snapshot = SnapshotId.new()
    opened = stored(folder).open()
    try:
        assert opened.kind is FileKind.DIRECTORY
        taken = manager.take(snapshot, [(stored(folder), opened)])
        assert taken[0].backend is BackendKind.BTRFS
        saved = volume / "snapshots" / f"{snapshot}-0"
        assert saved.is_dir()
        with pytest.raises(OSError):  # noqa: PT011  (the snapshot is read only)
            (saved / "proj" / "new").write_text("x")
        (folder / "a.txt").write_text("CHANGED")
        (folder / "d" / "b.txt").unlink()
        (folder / "created").write_text("c")
        made = SnapshotRecord(
            id=snapshot,
            kind=SnapshotKind.RUN,
            task=TaskId.new(),
            run=RunId.new(),
            taken_at=START,
            targets=taken,
        )

        manager.restore(made, [opened])

        assert (folder / "a.txt").read_text() == "alpha"
        assert (folder / "d" / "b.txt").read_text() == "beta"
        assert not (folder / "created").exists()
        manager.delete(made)
        assert not saved.exists()
    finally:
        opened.close()
        manager.close()


def test_a_folder_that_holds_a_subvolume_falls_back_to_the_copy(volume: Path) -> None:
    folder = volume / "data" / "proj"
    folder.mkdir()
    run("btrfs", "subvolume", "create", str(folder / "nested"))
    (folder / "nested" / "keep").write_text("keep")
    manager = SnapshotManager(
        SnapshotSettings(btrfs_directory=volume / "snapshots"), volume / "s", None
    )
    opened = stored(folder).open()
    try:
        taken = manager.take(SnapshotId.new(), [(stored(folder), opened)])
        assert taken[0].backend is BackendKind.COPY  # a snapshot would have left `nested` out
    finally:
        opened.close()
        manager.close()
