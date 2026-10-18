"""Workspaces on disk: opened by identity, listed and deleted without following what they hold."""

import os
import stat
from pathlib import Path

import pytest

from tests.broker.fsutil import make_fifo, make_socket
from tumbleweed.broker.errors import PathError, PathFault, WorkspaceError
from tumbleweed.broker.fdio import FileKind
from tumbleweed.broker.workspace import open_in_workspace
from tumbleweed.broker.workspaces import (
    MAX_DEPTH,
    MAX_LISTED,
    MAX_SCANNED,
    Workspaces,
    list_directory,
)
from tumbleweed.core.ids import TaskId
from tumbleweed.core.types import WorkspaceRelPath


@pytest.fixture
def workspaces(tmp_path: Path) -> Workspaces:
    opened = Workspaces.open(tmp_path / "workspaces")
    yield_ = opened
    return yield_


class TestMakingAndOpening:
    def test_a_workspace_is_a_private_directory_named_by_its_task(
        self, tmp_path: Path, workspaces: Workspaces
    ) -> None:
        task = TaskId.new()

        workspaces.create(task)

        path = tmp_path / "workspaces" / str(task)
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "workspaces").stat().st_mode) == 0o700
        workspaces.close()

    def test_it_opens_by_the_identity_it_was_made_with(self, workspaces: Workspaces) -> None:
        task = TaskId.new()
        identity = workspaces.create(task)

        with workspaces.open_workspace(task, identity) as opened:
            assert opened.kind is FileKind.DIRECTORY
            assert opened.identity == identity
        workspaces.close()

    def test_a_task_cannot_be_made_twice(self, workspaces: Workspaces) -> None:
        task = TaskId.new()
        workspaces.create(task)

        with pytest.raises(WorkspaceError):
            workspaces.create(task)
        workspaces.close()

    def test_a_symlink_swapped_in_for_the_workspace_is_refused(
        self, tmp_path: Path, workspaces: Workspaces
    ) -> None:
        task = TaskId.new()
        identity = workspaces.create(task)
        path = tmp_path / "workspaces" / str(task)
        elsewhere = tmp_path / "elsewhere"
        path.rename(elsewhere)
        path.symlink_to(elsewhere)

        with pytest.raises(PathError) as caught:
            workspaces.open_workspace(task, identity)

        assert caught.value.fault is PathFault.SYMLINK
        workspaces.close()

    def test_a_different_directory_with_the_same_name_is_refused(
        self, tmp_path: Path, workspaces: Workspaces
    ) -> None:
        task = TaskId.new()
        identity = workspaces.create(task)
        path = tmp_path / "workspaces" / str(task)
        path.rename(tmp_path / "moved")
        path.mkdir()

        with pytest.raises(PathError) as caught:
            workspaces.open_workspace(task, identity)

        assert caught.value.fault is PathFault.IDENTITY_MISMATCH
        workspaces.close()

    def test_the_directory_of_workspaces_may_not_be_a_symlink(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        (tmp_path / "link").symlink_to(real)

        with pytest.raises(Exception, match="not safe"):
            Workspaces.open(tmp_path / "link")


class TestListing:
    def test_names_kinds_and_sizes_sorted(self, tmp_path: Path, workspaces: Workspaces) -> None:
        task = TaskId.new()
        identity = workspaces.create(task)
        root = tmp_path / "workspaces" / str(task)
        (root / "b.txt").write_text("hello")
        (root / "a").mkdir()
        (root / "c.txt").write_text("")

        with workspaces.open_workspace(task, identity) as opened:
            listing = list_directory(opened)

        assert [(str(e.name), e.kind, e.size) for e in listing.entries] == [
            ("a", "directory", None),
            ("b.txt", "file", 5),
            ("c.txt", "file", 0),
        ]
        assert not listing.truncated
        assert listing.skipped == 0
        workspaces.close()

    def test_a_symlink_is_listed_as_one_and_never_followed(
        self, tmp_path: Path, workspaces: Workspaces
    ) -> None:
        task = TaskId.new()
        identity = workspaces.create(task)
        root = tmp_path / "workspaces" / str(task)
        secret = tmp_path / "secret-dir"
        secret.mkdir()
        (secret / "leak").write_text("x")
        (root / "to-dir").symlink_to(secret)
        (root / "to-file").symlink_to(secret / "leak")
        (root / "dangling").symlink_to(tmp_path / "nowhere")

        with workspaces.open_workspace(task, identity) as opened:
            listing = list_directory(opened)

        assert {(str(e.name), e.kind, e.size) for e in listing.entries} == {
            ("to-dir", "symlink", None),
            ("to-file", "symlink", None),
            ("dangling", "symlink", None),
        }
        workspaces.close()

    def test_fifos_and_sockets_are_listed_as_other(
        self, tmp_path: Path, workspaces: Workspaces
    ) -> None:
        task = TaskId.new()
        identity = workspaces.create(task)
        root = tmp_path / "workspaces" / str(task)
        make_fifo(root / "pipe")
        make_socket(root / "sock")

        with workspaces.open_workspace(task, identity) as opened:
            listing = list_directory(opened)

        assert {(str(e.name), e.kind) for e in listing.entries} == {
            ("pipe", "other"),
            ("sock", "other"),
        }
        workspaces.close()

    @pytest.mark.parametrize(
        "name",
        ["a\nb", "a\x1bb", "rtl‮evil", "zero​width", "line\u2028sep", "tab\there"],
    )
    def test_names_that_cannot_be_shown_safely_are_counted_and_left_out(
        self, tmp_path: Path, workspaces: Workspaces, name: str
    ) -> None:
        task = TaskId.new()
        identity = workspaces.create(task)
        root = tmp_path / "workspaces" / str(task)
        (root / name).write_text("x")
        (root / "fine").write_text("x")

        with workspaces.open_workspace(task, identity) as opened:
            listing = list_directory(opened)

        assert [str(e.name) for e in listing.entries] == ["fine"]
        assert listing.skipped == 1
        workspaces.close()

    def test_a_name_that_is_not_utf8_is_left_out(
        self, tmp_path: Path, workspaces: Workspaces
    ) -> None:
        task = TaskId.new()
        identity = workspaces.create(task)
        root = tmp_path / "workspaces" / str(task)
        try:
            (root / os.fsdecode(b"bad-\xff")).write_text("x")
        except OSError:
            pytest.skip("this file system refuses names that are not valid UTF-8")

        with workspaces.open_workspace(task, identity) as opened:
            listing = list_directory(opened)

        assert listing.entries == ()
        assert listing.skipped == 1
        workspaces.close()

    def test_a_long_directory_is_cut_and_says_so(
        self, tmp_path: Path, workspaces: Workspaces
    ) -> None:
        task = TaskId.new()
        identity = workspaces.create(task)
        root = tmp_path / "workspaces" / str(task)
        for number in range(MAX_LISTED + 5):
            (root / f"f{number:05}").touch()

        with workspaces.open_workspace(task, identity) as opened:
            listing = list_directory(opened)

        assert len(listing.entries) == MAX_LISTED
        assert listing.truncated
        workspaces.close()

    def test_it_reads_no_more_names_than_the_scan_limit(
        self, tmp_path: Path, workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tumbleweed.broker.workspaces.MAX_SCANNED", 10)
        task = TaskId.new()
        identity = workspaces.create(task)
        root = tmp_path / "workspaces" / str(task)
        for number in range(30):
            (root / f"f{number:02}").touch()

        with workspaces.open_workspace(task, identity) as opened:
            listing = list_directory(opened)

        assert len(listing.entries) == 10
        assert listing.truncated
        assert MAX_SCANNED == 10_000
        workspaces.close()

    def test_a_regular_file_cannot_be_listed(self, tmp_path: Path, workspaces: Workspaces) -> None:
        task = TaskId.new()
        identity = workspaces.create(task)
        (tmp_path / "workspaces" / str(task) / "f").write_text("x")

        with (
            workspaces.open_workspace(task, identity) as root,
            open_in_workspace(root, WorkspaceRelPath("f"), want=FileKind.REGULAR) as file,
            pytest.raises(PathError) as caught,
        ):
            list_directory(file)

        assert caught.value.fault is PathFault.WRONG_KIND
        workspaces.close()


class TestDeleting:
    def populate(self, root: Path, outside: Path) -> None:
        (root / "file").write_text("x")
        (root / "sub" / "deeper").mkdir(parents=True)
        (root / "sub" / "deeper" / "f").write_text("y")
        (root / "link-to-outside-dir").symlink_to(outside)
        (root / "link-to-outside-file").symlink_to(outside / "keep.txt")
        (root / "sub" / "link").symlink_to(outside)
        make_fifo(root / "pipe")

    def test_a_hostile_tree_is_deleted_and_nothing_outside_it_is_touched(
        self, tmp_path: Path, workspaces: Workspaces
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        task = TaskId.new()
        workspaces.create(task)
        root = tmp_path / "workspaces" / str(task)
        self.populate(root, outside)

        assert workspaces.delete(task) == "deleted"

        assert not root.exists()
        assert (outside / "keep.txt").read_text() == "keep"
        workspaces.close()

    def test_a_directory_the_program_made_unreadable_does_not_stop_it(
        self, tmp_path: Path, workspaces: Workspaces
    ) -> None:
        task = TaskId.new()
        workspaces.create(task)
        root = tmp_path / "workspaces" / str(task)
        locked = root / "locked"
        (locked / "inside").mkdir(parents=True)
        (locked / "inside" / "f").write_text("x")
        locked.chmod(0o000)

        assert workspaces.delete(task) == "deleted"

        assert not root.exists()
        workspaces.close()

    def test_a_missing_workspace_is_reported_as_such(self, workspaces: Workspaces) -> None:
        assert workspaces.delete(TaskId.new()) == "missing"
        workspaces.close()

    def test_a_symlink_in_place_of_a_workspace_is_removed_not_followed(
        self, tmp_path: Path, workspaces: Workspaces
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        task = TaskId.new()
        (tmp_path / "workspaces" / str(task)).symlink_to(outside)

        assert workspaces.delete(task) == "deleted"

        assert not (tmp_path / "workspaces" / str(task)).exists()
        assert (outside / "keep.txt").read_text() == "keep"
        workspaces.close()

    def test_a_tree_nested_too_deeply_is_refused_and_left_in_place(
        self, tmp_path: Path, workspaces: Workspaces
    ) -> None:
        task = TaskId.new()
        workspaces.create(task)
        root = tmp_path / "workspaces" / str(task)
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            current = directory_fd
            for _ in range(MAX_DEPTH + 2):
                os.mkdir("d", dir_fd=current)
                following = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=current)
                if current != directory_fd:
                    os.close(current)
                current = following
            os.close(current)
        finally:
            os.close(directory_fd)

        with pytest.raises(WorkspaceError, match="too deeply"):
            workspaces.delete(task)

        assert root.exists()
        workspaces.close()

    def test_names_lists_what_is_in_the_directory(self, workspaces: Workspaces) -> None:
        one, two = TaskId.new(), TaskId.new()
        workspaces.create(one)
        workspaces.create(two)

        assert sorted(workspaces.names()) == sorted([str(one), str(two)])
        workspaces.close()
