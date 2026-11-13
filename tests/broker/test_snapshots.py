"""The snapshot backends and the manager (S21, D28): copy, a fake btrfs, noop, and the choice."""

import os
import shutil
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from tests.broker.rig import START
from tumbleweed.broker.config import GIB, SnapshotMode, SnapshotSettings
from tumbleweed.broker.errors import SnapshotError, SnapshotFault
from tumbleweed.broker.fdio import OpenedPath
from tumbleweed.broker.identity import FsIdentity
from tumbleweed.broker.paths import ResolvedPath, StoredPath
from tumbleweed.broker.snapshots import (
    BackendKind,
    SnapshotKind,
    SnapshotManager,
    SnapshotRecord,
    SnapshotTarget,
)
from tumbleweed.broker.treeops import TreeLimits
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import RunId, SnapshotId, TaskId


def stored(path: Path) -> StoredPath:
    return StoredPath.record(ResolvedPath.resolve(str(path)))


def opened(path: Path) -> OpenedPath:
    return stored(path).open()


def make_tree(base: Path) -> Path:
    root = base / "proj"
    (root / "d").mkdir(parents=True)
    (root / "a.txt").write_text("alpha")
    (root / "d" / "b.txt").write_text("beta")
    return root


def record(snapshot: SnapshotId, targets: tuple[SnapshotTarget, ...]) -> SnapshotRecord:
    return SnapshotRecord(
        id=snapshot,
        kind=SnapshotKind.RUN,
        task=TaskId.new(),
        run=RunId.new(),
        taken_at=START,
        targets=targets,
    )


class FakeBtrfs:
    """A btrfs that is a directory: a 'subvolume' is the directory `root`, a snapshot a copy of it
    (made read-only by dropping write bits would stop our own delete: the real one has a verb)."""

    def __init__(self, root: Path, *, nested: bool = False) -> None:
        self.root = root
        self.nested = nested
        self.snapshots: list[str] = []
        self.deleted: list[str] = []
        self.same = True
        self.directory_inode: int | None = None  # the snapshot directory, which may not be btrfs
        self.directory_on_btrfs = True
        self.inodes: dict[int, int] = {}  # a snapshot's inode numbers are its source's, as btrfs's

    def is_btrfs(self, fd: int) -> bool:
        if self.directory_inode is not None and os.fstat(fd).st_ino == self.directory_inode:
            return self.directory_on_btrfs
        return self.same

    def has_nested_subvolume(self, fd: int, max_files: int, deadline: float) -> bool:
        return self.nested

    def subvolume_root(self, opened: OpenedPath, components: int) -> tuple[int, int] | None:
        current = os.dup(opened.fd)
        depth = 0
        while True:
            if os.fstat(current).st_ino == self.root.stat().st_ino:
                return current, depth
            if depth >= components:
                os.close(current)
                return None
            parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=current)
            os.close(current)
            current = parent
            depth += 1

    def snapshot(self, source_fd: int, directory: int, name: str) -> None:
        target = Path(f"/proc/self/fd/{directory}/{name}")
        source = Path(f"/proc/self/fd/{source_fd}")
        shutil.copytree(source, target, symlinks=True)
        for origin, copy in zip(
            sorted(p for p in source.rglob("*") if not p.is_symlink()),
            sorted(p for p in target.rglob("*") if not p.is_symlink()),
            strict=True,
        ):
            self.inodes[copy.stat().st_ino] = origin.stat().st_ino
        self.inodes[target.stat().st_ino] = source.stat().st_ino
        self.snapshots.append(name)

    def inode(self, fd: int) -> int:
        number = os.fstat(fd).st_ino
        return self.inodes.get(number, number)

    def delete(self, directory: int, name: str) -> None:
        shutil.rmtree(f"/proc/self/fd/{directory}/{name}")  # a missing one raises, as btrfs does
        self.deleted.append(name)


def manager(tmp_path: Path, **changes: object) -> SnapshotManager:
    settings = SnapshotSettings(**changes)  # type: ignore[arg-type]  # a test passes what it means
    return SnapshotManager(settings, tmp_path / "store", None)


class TestCopy:
    def test_take_and_restore_a_tree(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path)
        snapshots = manager(tmp_path)
        snapshot = SnapshotId.new()
        with opened(root) as target:
            taken = snapshots.take(snapshot, [(stored(root), target)]).targets
            (root / "a.txt").write_text("CHANGED")
            (root / "new").write_text("n")
            (root / "d" / "b.txt").unlink()
            snapshots.restore(record(snapshot, taken), [target])

        assert taken[0].backend is BackendKind.COPY
        assert taken[0].files == 4
        assert (root / "a.txt").read_text() == "alpha"
        assert (root / "d" / "b.txt").read_text() == "beta"
        assert not (root / "new").exists()
        snapshots.close()

    @pytest.mark.linux
    def test_a_single_file_is_saved_and_restored_in_place(self, tmp_path: Path) -> None:
        file = tmp_path / "f.txt"
        file.write_text("before")
        snapshots = manager(tmp_path)
        snapshot = SnapshotId.new()
        with opened(file) as target:
            taken = snapshots.take(snapshot, [(stored(file), target)]).targets
            file.write_text("after")
            snapshots.restore(record(snapshot, taken), [target])

        assert file.read_text() == "before"
        snapshots.close()

    def test_a_cap_fails_closed_and_leaves_nothing_behind(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path)
        snapshots = manager(tmp_path, max_bytes=3, store_max_bytes=GIB)
        with opened(root) as target, pytest.raises(SnapshotError) as caught:
            snapshots.take(SnapshotId.new(), [(stored(root), target)])

        assert caught.value.fault is SnapshotFault.TOO_MANY_BYTES
        assert list((tmp_path / "store").iterdir()) == []
        snapshots.close()

    def test_all_or_nothing_across_paths(self, tmp_path: Path) -> None:
        small = tmp_path / "small"
        small.mkdir()
        (small / "x").write_text("x")
        big = tmp_path / "big"
        big.mkdir()
        (big / "y").write_text("y" * 100)
        snapshots = manager(tmp_path, max_bytes=50, store_max_bytes=GIB)
        with opened(small) as first, opened(big) as second, pytest.raises(SnapshotError):
            snapshots.take(SnapshotId.new(), [(stored(small), first), (stored(big), second)])

        assert list((tmp_path / "store").iterdir()) == []
        snapshots.close()

    def test_delete_removes_the_copy(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path)
        snapshots = manager(tmp_path)
        snapshot = SnapshotId.new()
        with opened(root) as target:
            taken = snapshots.take(snapshot, [(stored(root), target)]).targets
        made = record(snapshot, taken)

        snapshots.delete(made)

        assert list((tmp_path / "store").iterdir()) == []
        snapshots.delete(made)  # deleting what is gone is fine
        snapshots.close()

    def test_a_restore_with_a_missing_copy_is_refused(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path)
        snapshots = manager(tmp_path)
        snapshot = SnapshotId.new()
        with opened(root) as target:
            taken = snapshots.take(snapshot, [(stored(root), target)]).targets
            shutil.rmtree(tmp_path / "store" / str(snapshot))
            with pytest.raises(SnapshotError) as caught:
                snapshots.restore(record(snapshot, taken), [target])

        assert caught.value.fault is SnapshotFault.STORED_COPY_BAD
        snapshots.close()


class TestNoop:
    def test_it_takes_nothing_and_says_so(self, tmp_path: Path) -> None:
        root = make_tree(tmp_path)
        snapshots = manager(tmp_path, mode=SnapshotMode.NOOP)
        with opened(root) as target, pytest.raises(SnapshotError) as caught:
            snapshots.take(SnapshotId.new(), [(stored(root), target)])

        assert caught.value.fault is SnapshotFault.UNSUPPORTED
        assert snapshots.copy_store is None
        snapshots.close()


@pytest.mark.linux  # the fake tool copies through /proc/self/fd
class TestBtrfs:
    def build(
        self, tmp_path: Path, *, nested: bool = False
    ) -> tuple[SnapshotManager, FakeBtrfs, Path]:
        volume = tmp_path / "volume"
        root = make_tree(volume)
        volume.mkdir(exist_ok=True)
        directory = tmp_path / "snapshots-subvolume"
        tool = FakeBtrfs(volume, nested=nested)
        settings = SnapshotSettings(btrfs_directory=directory)
        return SnapshotManager(settings, tmp_path / "store", tool), tool, root

    def test_a_folder_on_the_file_system_is_saved_as_a_subvolume_snapshot_and_put_back(
        self, tmp_path: Path
    ) -> None:
        snapshots, tool, root = self.build(tmp_path)
        snapshot = SnapshotId.new()
        with opened(root) as target:
            taken = snapshots.take(snapshot, [(stored(root), target)]).targets
            (root / "a.txt").write_text("CHANGED")
            (root / "d" / "b.txt").unlink()
            (root / "created").write_text("c")
            snapshots.restore(record(snapshot, taken), [target])

        assert taken[0].backend is BackendKind.BTRFS
        assert taken[0].inside == ("proj",)
        assert tool.snapshots == [f"{snapshot}-0"]
        assert (root / "a.txt").read_text() == "alpha"
        assert (root / "d" / "b.txt").read_text() == "beta"
        assert not (root / "created").exists()
        snapshots.delete(record(snapshot, taken))
        assert tool.deleted == [f"{snapshot}-0"]
        assert list((tmp_path / "store").iterdir()) == []  # the copy store was not used
        snapshots.close()

    def test_a_folder_that_holds_a_subvolume_is_copied_not_snapshotted(
        self, tmp_path: Path
    ) -> None:
        snapshots, tool, root = self.build(tmp_path, nested=True)
        with opened(root) as target:
            taken = snapshots.take(SnapshotId.new(), [(stored(root), target)])

        assert taken.targets[0].backend is BackendKind.COPY  # a snapshot would leave it out
        assert tool.snapshots == []
        assert taken.fallbacks == ((0, BackendKind.BTRFS, SnapshotFault.UNSUPPORTED),)
        snapshots.close()

    def test_a_snapshot_the_tool_cannot_make_falls_back_and_says_so(self, tmp_path: Path) -> None:
        snapshots, tool, root = self.build(tmp_path)

        def refused(source_fd: int, directory: int, name: str) -> None:
            raise SnapshotError(SnapshotFault.IO)  # another btrfs, no permission

        tool.snapshot = refused  # type: ignore[method-assign]  # a test double
        with opened(root) as target:
            taken = snapshots.take(SnapshotId.new(), [(stored(root), target)])

        assert taken.targets[0].backend is BackendKind.COPY
        assert taken.fallbacks == ((0, BackendKind.BTRFS, SnapshotFault.IO),)
        snapshots.close()

    def test_a_timeout_in_btrfs_is_not_retried_by_the_copy(self, tmp_path: Path) -> None:
        snapshots, tool, root = self.build(tmp_path)

        def slow(source_fd: int, directory: int, name: str) -> None:
            raise SnapshotError(SnapshotFault.TIMEOUT)

        tool.snapshot = slow  # type: ignore[method-assign]  # a test double
        with opened(root) as target, pytest.raises(SnapshotError) as caught:
            snapshots.take(SnapshotId.new(), [(stored(root), target)])

        assert caught.value.fault is SnapshotFault.TIMEOUT
        assert list((tmp_path / "store").iterdir()) == []  # the copy was not tried
        snapshots.close()

    def test_deleting_a_subvolume_that_is_gone_is_fine(self, tmp_path: Path) -> None:
        snapshots, tool, root = self.build(tmp_path)
        snapshot = SnapshotId.new()
        with opened(root) as target:
            taken = snapshots.take(snapshot, [(stored(root), target)]).targets
        shutil.rmtree(tmp_path / "snapshots-subvolume" / f"{snapshot}-0")

        snapshots.delete(record(snapshot, taken))  # no error: the Protocol says so

        assert tool.deleted == []
        snapshots.close()

    def test_a_subvolume_nobody_owns_is_found_and_deleted_like_a_copy(self, tmp_path: Path) -> None:
        snapshots, _tool, root = self.build(tmp_path)
        snapshot = SnapshotId.new()
        with opened(root) as target:
            snapshots.take(snapshot, [(stored(root), target)])

        assert snapshots.stored_names() == [snapshot]
        assert snapshots.delete_all(snapshot) is True
        assert snapshots.stored_names() == []
        assert snapshots.delete_all(snapshot) is False
        snapshots.close()

    def test_a_snapshot_directory_that_is_not_on_btrfs_means_the_copy(self, tmp_path: Path) -> None:
        snapshots, tool, root = self.build(tmp_path)
        tool.directory_inode = (tmp_path / "snapshots-subvolume").stat().st_ino
        tool.directory_on_btrfs = False
        with opened(root) as target:
            taken = snapshots.take(SnapshotId.new(), [(stored(root), target)])

        assert taken.targets[0].backend is BackendKind.COPY
        assert tool.snapshots == []
        snapshots.close()

    def test_a_file_or_another_file_system_uses_the_copy(self, tmp_path: Path) -> None:
        snapshots, tool, root = self.build(tmp_path)
        tool.same = False
        with opened(root) as target:
            taken = snapshots.take(SnapshotId.new(), [(stored(root), target)]).targets
        assert taken[0].backend is BackendKind.COPY
        assert tool.snapshots == []
        snapshots.close()

    def test_a_failing_btrfs_falls_back_to_the_copy(self, tmp_path: Path) -> None:
        snapshots, tool, root = self.build(tmp_path)

        def broken(source_fd: int, directory: int, name: str) -> None:
            raise SnapshotError(SnapshotFault.IO)

        tool.snapshot = broken  # type: ignore[method-assign]  # a test double
        with opened(root) as target:
            taken = snapshots.take(SnapshotId.new(), [(stored(root), target)]).targets
        assert taken[0].backend is BackendKind.COPY
        snapshots.close()

    def test_a_snapshot_whose_folder_is_not_the_one_saved_is_refused(self, tmp_path: Path) -> None:
        snapshots, _tool, root = self.build(tmp_path)
        snapshot = SnapshotId.new()
        with opened(root) as target:
            taken = snapshots.take(snapshot, [(stored(root), target)]).targets
            # The saved folder is swapped for another directory (another inode number).
            saved = tmp_path / "snapshots-subvolume" / f"{snapshot}-0" / "proj"
            saved.rename(saved.with_name("moved"))  # the old inode stays alive: no reuse
            saved.mkdir()
            with pytest.raises(SnapshotError) as caught:
                snapshots.restore(record(snapshot, taken), [target])

        assert caught.value.fault is SnapshotFault.STORED_COPY_BAD
        snapshots.close()

    def test_a_saved_folder_that_is_a_symlink_is_not_followed(self, tmp_path: Path) -> None:
        snapshots, _tool, root = self.build(tmp_path)
        snapshot = SnapshotId.new()
        elsewhere = tmp_path / "elsewhere"
        shutil.copytree(root, elsewhere)
        with opened(root) as target:
            taken = snapshots.take(snapshot, [(stored(root), target)]).targets
            saved = tmp_path / "snapshots-subvolume" / f"{snapshot}-0" / "proj"
            shutil.rmtree(saved)
            saved.symlink_to(elsewhere)
            with pytest.raises(SnapshotError):
                snapshots.restore(record(snapshot, taken), [target])
        snapshots.close()


def test_the_btrfs_backend_is_only_in_auto_mode_with_a_directory(tmp_path: Path) -> None:
    only_copy = SnapshotManager(
        SnapshotSettings(mode=SnapshotMode.COPY, btrfs_directory=tmp_path / "b"),
        tmp_path / "s1",
        None,
    )
    assert only_copy.copy_store is not None
    assert not (tmp_path / "b").exists()
    only_copy.close()


def test_settings_are_validated() -> None:
    for bad in (
        {"max_bytes": 0},
        {"max_files": -1},
        {"store_max_bytes": 1, "max_bytes": 2},
        {"retention": timedelta(0)},
        {"max_snapshots": 0},
        {"timeout": timedelta(hours=2)},
        {"btrfs_program": ""},
    ):
        with pytest.raises(InvalidValueError):
            SnapshotSettings(**bad)
    assert SnapshotSettings().max_bytes == GIB
    assert SnapshotSettings().retention == timedelta(days=7)
    assert SnapshotSettings().max_snapshots == 100
    assert TreeLimits(max_bytes=1, max_files=1).max_depth == 64


def test_a_record_is_a_no_undo_record_exactly_when_nothing_was_saved() -> None:
    from pydantic import ValidationError

    path = StoredPath(path="/x", identity=FsIdentity(fsid=1, ino=2))
    none = SnapshotTarget(path=path, backend=BackendKind.NOOP)
    copy = SnapshotTarget(path=path, backend=BackendKind.COPY)
    with pytest.raises(ValidationError):
        record(SnapshotId.new(), (none,))  # nothing saved, yet not said to be a no-undo record
    with pytest.raises(ValidationError):
        SnapshotRecord(
            id=SnapshotId.new(),
            kind=SnapshotKind.RUN,
            task=TaskId.new(),
            run=RunId.new(),
            taken_at=START,
            targets=(copy,),
            no_undo=True,
        )


def test_one_cap_for_all_the_paths_of_one_run(tmp_path: Path) -> None:
    folders = []
    for name in ("one", "two"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "f").write_bytes(b"x" * 40)
        folders.append(folder)
    one_file = (folders[0] / "f").stat().st_blocks * 512
    snapshots = manager(tmp_path, max_bytes=one_file + 100, store_max_bytes=GIB)
    with (
        opened(folders[0]) as first,
        opened(folders[1]) as second,
        pytest.raises(SnapshotError) as caught,
    ):
        snapshots.take(
            SnapshotId.new(), [(stored(folders[0]), first), (stored(folders[1]), second)]
        )

    assert caught.value.fault is SnapshotFault.TOO_MANY_BYTES  # each fits, together they do not
    assert list((tmp_path / "store").iterdir()) == []
    snapshots.close()


def test_a_fifo_in_a_tree_does_not_stop_the_snapshot(tmp_path: Path) -> None:
    root = make_tree(tmp_path)
    os.mkfifo(root / "pipe")
    snapshots = manager(tmp_path)
    snapshot = SnapshotId.new()
    with opened(root) as target:
        taken = snapshots.take(snapshot, [(stored(root), target)]).targets
        (root / "pipe").unlink()
        snapshots.restore(record(snapshot, taken), [target])

    assert stat.S_ISFIFO((root / "pipe").lstat().st_mode)
    snapshots.close()
