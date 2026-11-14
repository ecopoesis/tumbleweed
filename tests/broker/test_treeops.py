"""Copying, restoring, removing and measuring trees through descriptors (S21, I11)."""

import os
import stat
import time
from pathlib import Path

import pytest

from tests.broker.fsutil import make_fifo, make_socket, remove_chain
from tumbleweed.broker.errors import SnapshotError, SnapshotFault
from tumbleweed.broker.treeops import (
    TreeBudget,
    TreeLimits,
    TreeMeta,
    contains_subvolume,
    copy_object,
    load_meta,
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
        copy_object(sfd, dfd, "copy", TreeBudget(LIMITS), soon())
    finally:
        os.close(sfd)
        os.close(dfd)
    return store / "copy"


def restore(saved: Path, root: Path) -> None:
    sfd, tfd = open_dir(saved), open_dir(root)
    try:
        restore_object(sfd, tfd, load_meta(os.open(saved.parent, os.O_RDONLY), saved.name), soon())
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


@pytest.mark.linux
def test_a_granted_file_is_restored_in_place(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    target.write_text("before")
    store = tmp_path / "store"
    store.mkdir()
    sfd = os.open(target, os.O_RDONLY)
    dfd = open_dir(store)
    copy_object(sfd, dfd, "copy", TreeBudget(LIMITS), soon())
    target.write_text("after, longer")
    inode = target.stat().st_ino
    stored = os.open(store / "copy", os.O_RDONLY)
    try:
        restore_object(stored, sfd, TreeMeta(), soon())
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
            copy_object(sfd, dfd, "c", TreeBudget(limits), soon())
    finally:
        os.close(sfd)
        os.close(dfd)
    assert caught.value.fault is fault


def test_the_deadline_stops_a_walk(tmp_path: Path) -> None:
    root = tree(tmp_path)
    (tmp_path / "s").mkdir()
    sfd, dfd = open_dir(root), open_dir(tmp_path / "s")
    try:
        with pytest.raises(SnapshotError) as caught:
            copy_object(sfd, dfd, "c", TreeBudget(LIMITS), time.monotonic() - 1)
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


# --- special files, sparse files, unreadable things, depth (D28, H1) ----------------------------


def stat_size(path: Path) -> int:
    return path.stat().st_blocks * 512


def test_a_fifo_is_recorded_not_opened_and_a_new_one_is_removed_by_a_restore(
    tmp_path: Path,
) -> None:
    root = tree(tmp_path)
    saved = snap(root, tmp_path / "store")  # no FIFO then
    make_fifo(root / "pipe")  # a run leaves one

    restore(saved, root)

    assert not (root / "pipe").exists()
    assert (root / "a.txt").read_text() == "alpha"


@pytest.mark.linux  # mkfifo relative to a descriptor
def test_a_fifo_the_saved_state_had_is_recreated_and_a_socket_is_reported_skipped(
    tmp_path: Path,
) -> None:
    root = tree(tmp_path)
    make_fifo(root / "pipe")
    make_socket(root / "sock")
    saved = snap(root, tmp_path / "store")  # never opened: this would hang on a FIFO
    (root / "pipe").unlink()
    (root / "sock").unlink()
    sfd, tfd = open_dir(saved), open_dir(root)
    meta_parent = os.open(saved.parent, os.O_RDONLY)
    try:
        stats = restore_object(sfd, tfd, load_meta(meta_parent, saved.name), soon())
    finally:
        for fd in (sfd, tfd, meta_parent):
            os.close(fd)

    assert stat.S_ISFIFO((root / "pipe").lstat().st_mode)
    assert not (root / "sock").exists()  # not ours to make
    assert stats.skipped == 1
    assert stats.written >= 1


@pytest.mark.linux  # mkfifo relative to a descriptor
def test_a_special_file_of_the_saved_kind_is_kept_and_one_of_another_kind_replaced(
    tmp_path: Path,
) -> None:
    root = tree(tmp_path)
    make_fifo(root / "pipe")
    saved = snap(root, tmp_path / "store")
    (root / "pipe").unlink()
    (root / "pipe").write_text("now a file")

    restore(saved, root)

    assert stat.S_ISFIFO((root / "pipe").lstat().st_mode)


def test_a_sparse_file_costs_what_it_holds_and_stays_sparse(tmp_path: Path) -> None:
    root = tree(tmp_path)
    with (root / "huge").open("wb") as handle:
        handle.write(b"head")
        handle.truncate(2 * 1024**3)  # 2 GiB apparent, a block on disk
    (tmp_path / "s").mkdir()
    sfd, dfd = open_dir(root), open_dir(tmp_path / "s")
    try:
        stats = copy_object(
            sfd, dfd, "c", TreeBudget(TreeLimits(max_bytes=1 << 20, max_files=100)), soon()
        )
    finally:
        os.close(sfd)
        os.close(dfd)

    copy = tmp_path / "s" / "c" / "huge"
    assert copy.stat().st_size == 2 * 1024**3
    assert stat_size(copy) < 1 << 20
    assert stats.bytes < 1 << 20
    assert copy.read_bytes()[:4] == b"head"


def test_the_workspace_measure_counts_allocated_blocks_not_apparent_size(tmp_path: Path) -> None:
    root = tree(tmp_path)
    with (root / "huge").open("wb") as handle:
        handle.truncate(2 * 1024**3)
    fd = open_dir(root)
    try:
        usage = measure(fd, max_bytes=1 << 20, max_files=100, deadline=soon())
    finally:
        os.close(fd)

    assert not usage.over


@pytest.mark.linux  # unlocking goes through an O_PATH descriptor
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_an_unreadable_directory_and_file_of_ours_are_copied_and_their_modes_come_back(
    tmp_path: Path,
) -> None:
    root = tree(tmp_path)
    (root / "locked").mkdir()
    (root / "locked" / "inner.txt").write_text("inner")
    (root / "locked" / "inner.txt").chmod(0o000)
    (root / "locked").chmod(0o000)
    (root / "secret.txt").write_text("s")
    (root / "secret.txt").chmod(0o000)
    saved = snap(root, tmp_path / "store")
    try:
        # The copy itself stays readable to us, and the originals are as they were.
        assert (root / "locked").stat().st_mode & 0o777 == 0
        assert (root / "secret.txt").stat().st_mode & 0o777 == 0
        (root / "locked").chmod(0o700)
        (root / "locked" / "inner.txt").chmod(0o600)
        (root / "locked" / "inner.txt").write_text("CHANGED")
        (root / "locked").chmod(0)
        (root / "secret.txt").chmod(0o600)
        (root / "secret.txt").write_text("CHANGED")

        restore(saved, root)

        assert (root / "locked").stat().st_mode & 0o777 == 0
        assert (root / "secret.txt").stat().st_mode & 0o777 == 0
        (root / "locked").chmod(0o700)
        (root / "locked" / "inner.txt").chmod(0o600)
        assert (root / "locked" / "inner.txt").read_text() == "inner"
        (root / "secret.txt").chmod(0o600)
        assert (root / "secret.txt").read_text() == "s"
    finally:
        (root / "locked").chmod(0o700)


def test_a_tree_a_program_nested_deeper_than_a_copy_goes_is_removed_by_a_restore(
    tmp_path: Path,
) -> None:
    root = tree(tmp_path)
    saved = snap(root, tmp_path / "store")
    here = root / "deep"
    here.mkdir()
    for _ in range(120):
        here = here / "x"
        here.mkdir()

    restore(saved, root)

    assert not (root / "deep").exists()


def test_a_tree_deeper_than_a_copy_goes_is_refused_by_the_copy(tmp_path: Path) -> None:
    root = tree(tmp_path)
    here = root
    for _ in range(80):
        here = here / "x"
        here.mkdir()
    (tmp_path / "s").mkdir()
    sfd, dfd = open_dir(root), open_dir(tmp_path / "s")
    try:
        with pytest.raises(SnapshotError) as caught:
            copy_object(sfd, dfd, "c", TreeBudget(LIMITS), soon())
    finally:
        os.close(sfd)
        os.close(dfd)

    assert caught.value.fault is SnapshotFault.TOO_DEEP


# --- the cap is one budget, and its edges ------------------------------------------------------


def test_two_copies_share_one_budget(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    for each in (one, two):
        each.mkdir()
        (each / "f").write_bytes(b"x" * 10)
    store = tmp_path / "s"
    store.mkdir()
    cap = stat_size(one / "f") + 100  # room for the first file, not for both
    budget = TreeBudget(TreeLimits(max_bytes=cap, max_files=100))
    dfd = open_dir(store)
    first, second = open_dir(one), open_dir(two)
    try:
        copy_object(first, dfd, "a", budget, soon())
        with pytest.raises(SnapshotError) as caught:
            copy_object(second, dfd, "b", budget, soon())
    finally:
        for fd in (dfd, first, second):
            os.close(fd)

    assert caught.value.fault is SnapshotFault.TOO_MANY_BYTES


def exact_copy(tmp_path: Path, root: Path, **limits: int) -> None:
    (tmp_path / "s").mkdir(exist_ok=True)
    sfd, dfd = open_dir(root), open_dir(tmp_path / "s")
    name = f"c{len(list((tmp_path / 's').iterdir()))}"
    try:
        copy_object(sfd, dfd, name, TreeBudget(TreeLimits(**limits)), soon())
    finally:
        os.close(sfd)
        os.close(dfd)


def test_a_cap_that_is_exactly_met_is_met_and_one_less_is_not(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    (root / "f").write_bytes(b"x" * 5)
    (root / "link").symlink_to("f" * 30)
    need = stat_size(root / "f") + len("f" * 30)
    exact_copy(tmp_path, root, max_bytes=need, max_files=3)  # the directory, a file, a link
    with pytest.raises(SnapshotError) as bytes_:
        exact_copy(tmp_path, root, max_bytes=need - 1, max_files=3)
    with pytest.raises(SnapshotError) as files:
        exact_copy(tmp_path, root, max_bytes=need, max_files=2)

    assert bytes_.value.fault is SnapshotFault.TOO_MANY_BYTES  # the link's target counts too
    assert files.value.fault is SnapshotFault.TOO_MANY_FILES


# --- restoring links, kinds and modes ----------------------------------------------------------


def test_a_link_that_changed_its_target_or_became_a_file_or_a_directory_is_put_back(
    tmp_path: Path,
) -> None:
    root = tree(tmp_path)
    saved = snap(root, tmp_path / "store")
    (root / "link").unlink()
    (root / "link").symlink_to("elsewhere")
    restore(saved, root)
    assert (root / "link").readlink().as_posix() == "a.txt"
    (root / "link").unlink()
    (root / "link").write_text("a file now")
    restore(saved, root)
    assert (root / "link").readlink().as_posix() == "a.txt"
    (root / "link").unlink()
    (root / "link").mkdir()
    (root / "link" / "x").write_text("x")
    restore(saved, root)
    assert (root / "link").readlink().as_posix() == "a.txt"


def test_a_directory_where_a_file_was_is_replaced_by_the_file(tmp_path: Path) -> None:
    root = tree(tmp_path)
    saved = snap(root, tmp_path / "store")
    (root / "a.txt").unlink()
    (root / "a.txt").mkdir()
    (root / "a.txt" / "inner").write_text("x")

    restore(saved, root)

    assert (root / "a.txt").read_text() == "alpha"


def test_modes_of_files_and_directories_are_put_back(tmp_path: Path) -> None:
    root = tree(tmp_path)
    (root / "d").chmod(0o750)
    saved = snap(root, tmp_path / "store")
    (root / "d").chmod(0o700)
    (root / "a.txt").chmod(0o600)
    (root / "ro.txt").chmod(0o666)

    restore(saved, root)

    assert (root / "d").stat().st_mode & 0o777 == 0o750
    assert (root / "a.txt").stat().st_mode & 0o777 == (
        tmp_path / "t" / "a.txt"
    ).stat().st_mode & 0o777
    assert (root / "ro.txt").stat().st_mode & 0o777 == 0o444


# --- the swap guards: an entry is replaced between lstat and open (I11) -------------------------


class Swap:
    """Wraps `os.open` so that, on a chosen call, a directory is swapped for another (the old one
    renamed away) just before the real open: what a program racing the broker would do."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, name: str, nth: int, victim: Path
    ) -> None:
        self.real = os.open
        self.name, self.nth, self.victim = name, nth, victim
        self.seen = 0
        monkeypatch.setattr(os, "open", self)

    def __call__(self, path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == self.name and kwargs.get("dir_fd") is not None and flags & os.O_DIRECTORY:
            self.seen += 1
            if self.seen == self.nth:
                self.victim.rename(self.victim.with_name(self.victim.name + "-old"))
                self.victim.mkdir()
        return self.real(path, flags, *args, **kwargs)  # type: ignore[arg-type]  # delegating


def test_a_directory_swapped_while_it_is_copied_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tree(tmp_path)
    Swap(monkeypatch, name="d", nth=1, victim=root / "d")
    (tmp_path / "s").mkdir()
    sfd, dfd = open_dir(root), open_dir(tmp_path / "s")
    try:
        with pytest.raises(SnapshotError):
            copy_object(sfd, dfd, "c", TreeBudget(LIMITS), soon())
    finally:
        os.close(sfd)
        os.close(dfd)


def test_a_target_directory_swapped_while_it_is_restored_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tree(tmp_path)
    saved = snap(root, tmp_path / "store")
    (root / "d" / "b.txt").write_text("CHANGED")
    Swap(monkeypatch, name="d", nth=2, victim=root / "d")  # the second open of `d` is the target's

    with pytest.raises(SnapshotError) as caught:
        restore(saved, root)

    assert caught.value.fault is SnapshotFault.TARGET_CHANGED
    assert (tmp_path / "t" / "d-old" / "b.txt").read_text() == "CHANGED"  # the old one: untouched


def test_a_directory_swapped_while_it_is_removed_is_refused_and_not_emptied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "p"
    (parent / "victim").mkdir(parents=True)
    (parent / "victim" / "keep").write_text("keep")
    Swap(monkeypatch, name="victim", nth=1, victim=parent / "victim")
    fd = open_dir(parent)
    try:
        with pytest.raises(SnapshotError) as caught:
            remove_entry(fd, "victim", soon())
    finally:
        os.close(fd)

    assert caught.value.fault is SnapshotFault.TARGET_CHANGED
    assert (parent / "victim-old" / "keep").read_text() == "keep"


@pytest.mark.linux
def test_a_granted_file_reopened_as_another_object_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.txt"
    target.write_text("before")
    other = tmp_path / "other.txt"
    other.write_text("other")
    store = tmp_path / "store"
    store.mkdir()
    tfd = os.open(target, os.O_RDONLY)
    dfd = open_dir(store)
    copy_object(tfd, dfd, "copy", TreeBudget(LIMITS), soon())
    target.write_text("after")
    real = os.open

    def reopen(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if isinstance(path, str) and path.startswith("/proc/self/fd/"):
            return real(other, os.O_WRONLY)  # a different object behind the same name
        return real(path, flags, *args, **kwargs)  # type: ignore[arg-type]  # delegating

    monkeypatch.setattr(os, "open", reopen)
    stored = real(store / "copy", os.O_RDONLY)
    try:
        with pytest.raises(SnapshotError) as caught:
            restore_object(stored, tfd, TreeMeta(), soon())
    finally:
        for fd in (tfd, dfd, stored):
            os.close(fd)

    assert caught.value.fault is SnapshotFault.TARGET_CHANGED
    assert other.read_text() == "other"
    assert target.stat().st_mode & 0o777 == 0o644 or target.stat().st_mode & 0o200  # mode back


def test_a_subvolume_search_that_cannot_finish_says_there_is_one(tmp_path: Path) -> None:
    root = tree(tmp_path)
    fd = open_dir(root)
    try:
        assert contains_subvolume(fd, max_files=1000, deadline=soon()) is False
        assert contains_subvolume(fd, max_files=1000, deadline=time.monotonic() - 1) is True
        assert contains_subvolume(fd, max_files=2, deadline=soon()) is True  # a limit
    finally:
        os.close(fd)


# --- round 2: guards a mutant showed untested --------------------------------------------------


def chain(parent: Path, name: str, levels: int) -> None:
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    os.mkdir(name, dir_fd=fd)
    here = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
    os.close(fd)
    for _ in range(levels):
        os.mkdir("x", dir_fd=here)
        nxt = os.open("x", os.O_RDONLY | os.O_DIRECTORY, dir_fd=here)
        os.close(here)
        here = nxt
    os.close(here)


@pytest.mark.linux  # macOS resolves a chain that deep in quadratic time
def test_a_chain_of_thousands_of_directories_is_removed(tmp_path: Path) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    chain(parent, "deep", 5000)
    (parent / "keep").write_text("keep")
    fd = open_dir(parent)
    try:
        remove_entry(fd, "deep", soon())
    finally:
        os.close(fd)
        remove_chain(parent, "deep")  # only if the code under test left it

    assert [p.name for p in parent.iterdir()] == ["keep"]


def test_a_directory_that_is_not_the_one_climbed_from_is_refused_on_the_way_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "p"
    (parent / "victim" / "inner").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "keep").mkdir(parents=True)
    real = os.open

    def lying(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == ".." and kwargs.get("dir_fd") is not None:
            return real(elsewhere, os.O_RDONLY | os.O_DIRECTORY)  # `..` leads somewhere else
        return real(path, flags, *args, **kwargs)  # type: ignore[arg-type]  # delegating

    monkeypatch.setattr(os, "open", lying)
    fd = open_dir(parent)
    try:
        with pytest.raises(SnapshotError) as caught:
            remove_entry(fd, "victim", soon())
    finally:
        os.close(fd)

    assert caught.value.fault is SnapshotFault.TARGET_CHANGED
    assert (elsewhere / "keep").is_dir()  # nothing outside was touched


@pytest.mark.linux
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_a_file_swapped_while_it_is_unlocked_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tree(tmp_path)
    (root / "secret.txt").write_text("s")
    (root / "secret.txt").chmod(0)
    real = os.open
    swapped: list[bool] = []

    def swap(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == "secret.txt" and flags & getattr(os, "O_PATH", 0) and not swapped:
            swapped.append(True)
            (root / "secret.txt").rename(root / "moved.txt")
            (root / "secret.txt").write_text("another")
        return real(path, flags, *args, **kwargs)  # type: ignore[arg-type]  # delegating

    monkeypatch.setattr(os, "open", swap)
    (tmp_path / "s").mkdir()
    sfd, dfd = open_dir(root), open_dir(tmp_path / "s")
    try:
        with pytest.raises(SnapshotError):
            copy_object(sfd, dfd, "c", TreeBudget(LIMITS), soon())
    finally:
        os.close(sfd)
        os.close(dfd)
        (root / "moved.txt").chmod(0o600)

    assert (root / "secret.txt").stat().st_mode & 0o777 == 0o644  # the swapped one: not unlocked


@pytest.mark.linux
def test_a_failed_in_place_restore_puts_the_mode_back_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.txt"
    target.write_text("before")
    target.chmod(0o444)
    store = tmp_path / "store"
    store.mkdir()
    tfd = os.open(target, os.O_RDONLY)
    dfd = open_dir(store)
    copy_object(tfd, dfd, "copy", TreeBudget(LIMITS), soon())
    target.chmod(0o600)
    target.write_text("after")
    target.chmod(0o444)
    real = os.open

    def fail(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if isinstance(path, str) and path.startswith("/proc/self/fd/"):
            raise PermissionError
        return real(path, flags, *args, **kwargs)  # type: ignore[arg-type]  # delegating

    monkeypatch.setattr(os, "open", fail)
    stored = real(store / "copy", os.O_RDONLY)
    try:
        with pytest.raises(SnapshotError):
            restore_object(stored, tfd, TreeMeta(), soon())
    finally:
        for fd in (tfd, dfd, stored):
            os.close(fd)

    assert target.stat().st_mode & 0o777 == 0o444  # not left writable


def test_the_mode_of_the_restored_folder_itself_is_put_back(tmp_path: Path) -> None:
    root = tree(tmp_path)
    root.chmod(0o750)
    saved = snap(root, tmp_path / "store")
    root.chmod(0o700)

    restore(saved, root)

    assert root.stat().st_mode & 0o777 == 0o750


def test_a_special_file_that_is_still_there_is_the_same_one_after_a_restore(tmp_path: Path) -> None:
    root = tree(tmp_path)
    make_fifo(root / "pipe")
    saved = snap(root, tmp_path / "store")
    (tmp_path / "held").hardlink_to(root / "pipe")  # keeps the inode, so it cannot be reused

    restore(saved, root)

    assert (root / "pipe").stat().st_ino == (tmp_path / "held").stat().st_ino  # kept, not remade


def test_the_subvolume_root_inode_is_what_the_search_looks_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tree(tmp_path)
    fd = open_dir(root)
    try:
        assert contains_subvolume(fd, max_files=1000, deadline=soon()) is False
        monkeypatch.setattr(
            "tumbleweed.broker.treeops._SUBVOLUME_ROOT_INODE", (root / "d").stat().st_ino
        )
        assert contains_subvolume(fd, max_files=1000, deadline=soon()) is True
    finally:
        os.close(fd)


def test_a_file_that_grows_while_it_is_copied_is_stopped_at_the_cap_chunk_by_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    from tumbleweed.broker import treeops

    big = tmp_path / "big"
    big.write_bytes(b"x" * (12 * treeops.CHUNK))
    monkeypatch.setattr(fcntl, "ioctl", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    reads: list[int] = []
    real = os.read

    def counting(fd: int, count: int) -> bytes:
        data = real(fd, count)
        reads.append(len(data))
        return data

    monkeypatch.setattr(os, "read", counting)
    source = os.open(big, os.O_RDONLY)
    (tmp_path / "out").write_bytes(b"")
    out = os.open(tmp_path / "out", os.O_WRONLY)
    work = treeops._Work(  # noqa: SLF001
        TreeBudget(TreeLimits(max_bytes=2 * treeops.CHUNK, max_files=10)), soon()
    )
    try:
        with pytest.raises(SnapshotError) as caught:
            treeops._copy_content(source, out, work, allowed=0)  # noqa: SLF001
    finally:
        os.close(source)
        os.close(out)

    assert caught.value.fault is SnapshotFault.TOO_MANY_BYTES
    assert len([n for n in reads if n]) <= 4  # stopped near the cap, not at the end of the file
