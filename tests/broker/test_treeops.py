"""Copying, restoring, removing and measuring trees through descriptors (S21, I11)."""

import os
import time
from pathlib import Path

import pytest

from tests.broker.fsutil import make_fifo
from tumbleweed.broker.errors import SnapshotError, SnapshotFault
from tumbleweed.broker.treeops import (
    TreeLimits,
    copy_object,
    measure,
    remove_entry,
    restore_object,
)

LIMITS = TreeLimits(max_bytes=1 << 20, max_files=100)


def soon() -> float:
    return time.monotonic() + 30


def open_dir(path: Path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY)


def tree(base: Path) -> Path:
    root = base / "t"
    (root / "d" / "e").mkdir(parents=True)
    (root / "a.txt").write_text("alpha")
    (root / "d" / "b.txt").write_text("beta")
    (root / "d" / "e" / "c.txt").write_text("gamma")
    (root / "ro.txt").write_text("ro")
    (root / "ro.txt").chmod(0o444)
    (root / "link").symlink_to("a.txt")
    return root


def snap(root: Path, store: Path) -> Path:
    store.mkdir()
    sfd, dfd = open_dir(root), open_dir(store)
    try:
        copy_object(sfd, dfd, "copy", LIMITS, soon())
    finally:
        os.close(sfd)
        os.close(dfd)
    return store / "copy"


def restore(saved: Path, root: Path) -> None:
    sfd, tfd = open_dir(saved), open_dir(root)
    try:
        restore_object(sfd, tfd, LIMITS, soon())
    finally:
        os.close(sfd)
        os.close(tfd)


def test_restore_undoes_changes_creations_and_deletions(tmp_path: Path) -> None:
    root = tree(tmp_path)
    saved = snap(root, tmp_path / "store")
    (root / "a.txt").write_text("CHANGED")
    (root / "d" / "b.txt").unlink()
    (root / "new.txt").write_text("new")
    (root / "newdir" / "x").mkdir(parents=True)
    (root / "d" / "e").rename(root / "moved")

    restore(saved, root)

    assert (root / "a.txt").read_text() == "alpha"
    assert (root / "d" / "b.txt").read_text() == "beta"
    assert (root / "d" / "e" / "c.txt").read_text() == "gamma"
    assert not (root / "new.txt").exists()
    assert not (root / "newdir").exists()
    assert not (root / "moved").exists()
    assert (root / "ro.txt").stat().st_mode & 0o777 == 0o444
    assert (root / "link").readlink().as_posix() == "a.txt"


def test_a_symlink_planted_where_a_directory_was_is_replaced_not_followed(tmp_path: Path) -> None:
    root = tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    saved = snap(root, tmp_path / "store")
    import shutil

    shutil.rmtree(root / "d")
    (root / "d").symlink_to(outside)

    restore(saved, root)

    assert not (root / "d").is_symlink()
    assert (root / "d" / "b.txt").read_text() == "beta"
    assert sorted(p.name for p in outside.iterdir()) == ["keep"]
    assert (outside / "keep").read_text() == "keep"


def test_a_symlink_planted_where_a_file_was_is_not_written_through(tmp_path: Path) -> None:
    root = tree(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("victim")
    saved = snap(root, tmp_path / "store")
    (root / "a.txt").unlink()
    (root / "a.txt").symlink_to(victim)

    restore(saved, root)

    assert victim.read_text() == "victim"
    assert not (root / "a.txt").is_symlink()
    assert (root / "a.txt").read_text() == "alpha"


def test_a_granted_file_is_restored_in_place(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("before")
    store = tmp_path / "store"
    store.mkdir()
    sfd = os.open(target, os.O_RDONLY)
    dfd = open_dir(store)
    copy_object(sfd, dfd, "copy", LIMITS, soon())
    target.write_text("after, longer")
    inode = target.stat().st_ino
    stored = os.open(store / "copy", os.O_RDONLY)
    try:
        restore_object(stored, sfd, LIMITS, soon())
    finally:
        for fd in (sfd, dfd, stored):
            os.close(fd)
    assert target.read_text() == "before"
    assert target.stat().st_ino == inode


@pytest.mark.parametrize(
    ("limits", "fault"),
    [
        (TreeLimits(max_bytes=5, max_files=100), SnapshotFault.TOO_MANY_BYTES),
        (TreeLimits(max_bytes=1 << 20, max_files=3), SnapshotFault.TOO_MANY_FILES),
        (TreeLimits(max_bytes=1 << 20, max_files=100, max_depth=1), SnapshotFault.TOO_DEEP),
    ],
)
def test_caps_fail_closed(tmp_path: Path, limits: TreeLimits, fault: SnapshotFault) -> None:
    root = tree(tmp_path)
    (tmp_path / "s").mkdir()
    sfd, dfd = open_dir(root), open_dir(tmp_path / "s")
    try:
        with pytest.raises(SnapshotError) as caught:
            copy_object(sfd, dfd, "c", limits, soon())
    finally:
        os.close(sfd)
        os.close(dfd)
    assert caught.value.fault is fault


def test_a_fifo_refuses_the_copy(tmp_path: Path) -> None:
    root = tree(tmp_path)
    make_fifo(root / "pipe")
    (tmp_path / "s").mkdir()
    sfd, dfd = open_dir(root), open_dir(tmp_path / "s")
    try:
        with pytest.raises(SnapshotError) as caught:
            copy_object(sfd, dfd, "c", LIMITS, soon())
    finally:
        os.close(sfd)
        os.close(dfd)
    assert caught.value.fault is SnapshotFault.SPECIAL_FILE


def test_the_deadline_stops_a_walk(tmp_path: Path) -> None:
    root = tree(tmp_path)
    (tmp_path / "s").mkdir()
    sfd, dfd = open_dir(root), open_dir(tmp_path / "s")
    try:
        with pytest.raises(SnapshotError) as caught:
            copy_object(sfd, dfd, "c", LIMITS, time.monotonic() - 1)
    finally:
        os.close(sfd)
        os.close(dfd)
    assert caught.value.fault is SnapshotFault.TIMEOUT


def test_remove_does_not_follow_links_and_opens_locked_directories(tmp_path: Path) -> None:
    root = tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k")
    (root / "d" / "out").symlink_to(outside)
    (root / "d" / "e").chmod(0)
    fd = open_dir(tmp_path)
    try:
        remove_entry(fd, "t", soon())
    finally:
        os.close(fd)
    assert not root.exists()
    assert (outside / "keep").read_text() == "k"


def test_measure_counts_and_stops_at_the_limit(tmp_path: Path) -> None:
    root = tree(tmp_path)
    (root / "big").write_bytes(b"x" * 100_000)
    fd = open_dir(root)
    try:
        small = measure(fd, max_bytes=1 << 30, max_files=1000, deadline=soon())
        over = measure(fd, max_bytes=50_000, max_files=1000, deadline=soon())
    finally:
        os.close(fd)
    assert not small.over
    assert small.bytes >= 100_000
    assert small.files == 8
    assert over.over
