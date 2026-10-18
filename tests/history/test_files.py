"""The history's files: permissions, and a refusal of everything at the paths that is not ours."""

import logging
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from tests.history.support import assistant, history_directory, open_store, user
from tumbleweed.core.ids import SessionId
from tumbleweed.history import files
from tumbleweed.history.claims import SessionClaims
from tumbleweed.history.db import DATABASE_NAME, Database
from tumbleweed.history.errors import HistoryRefusedError
from tumbleweed.history.store import HistoryStore


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def written(tmp_path: Path) -> Path:
    """A history directory that has had a session, a message and a second shell's session."""
    store = open_store(tmp_path)
    store.begin(limit=10)
    store.append(user("a secret thought"))
    store.append(assistant("an answer"))
    other = open_store(tmp_path)  # a second connection, so that the WAL files exist
    other.begin(limit=10)
    other.close()
    store.close()
    return history_directory(tmp_path)


class TestPermissions:
    def test_the_directories_are_0700_and_every_file_in_them_0600(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        store.append(user("hello"))
        directory = history_directory(tmp_path)

        try:
            assert mode(directory) == 0o700
            assert mode(directory.parent) == 0o700  # tumbleweed/
            assert mode(directory / "chat.locks") == 0o700
            names = sorted(path.name for path in directory.iterdir())
            assert DATABASE_NAME in names
            assert f"{DATABASE_NAME}-wal" in names  # the write-ahead log exists while it is open
            for path in [*directory.iterdir(), *(directory / "chat.locks").iterdir()]:
                if path.is_file():
                    assert mode(path) == 0o600, path.name
        finally:
            store.close()

    def test_the_umask_cannot_loosen_or_break_them(self, tmp_path: Path) -> None:
        for umask, name in ((0o000, "open"), (0o277, "closed")):
            root = tmp_path / name
            history_directory(root).parent.parent.mkdir(parents=True)  # the user's own data home
            old = os.umask(umask)
            try:
                store = open_store(root)
                store.begin(limit=10)
                store.append(user("x"))
                directory = history_directory(root)
                assert mode(directory) == 0o700
                assert mode(directory / DATABASE_NAME) == 0o600
                store.close()
            finally:
                os.umask(old)

    def test_a_directory_of_ours_that_is_too_open_is_repaired_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        directory = written(tmp_path)
        directory.chmod(0o777)

        with caplog.at_level(logging.WARNING, logger="tumbleweed.history"):
            HistoryStore.open(directory).close()

        assert mode(directory) == 0o700
        assert "mode 0700" in caplog.text

    def test_a_file_of_ours_that_is_too_open_is_repaired_and_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        directory = written(tmp_path)
        (directory / DATABASE_NAME).chmod(0o666)

        with caplog.at_level(logging.WARNING, logger="tumbleweed.history"):
            restored = HistoryStore.open(directory).begin(limit=10)

        assert mode(directory / DATABASE_NAME) == 0o600
        assert "mode 0600" in caplog.text
        assert [m.text for m in restored.messages] == ["a secret thought", "an answer"]


class TestRefusals:
    def refuse(self, directory: Path) -> None:
        with pytest.raises(HistoryRefusedError) as raised:
            HistoryStore.open(directory)
        assert str(directory) not in str(raised.value)  # a fixed message: no path, no content

    def test_a_symlink_in_place_of_the_database_is_refused_and_its_target_untouched(
        self, tmp_path: Path
    ) -> None:
        directory = written(tmp_path)
        target = tmp_path / "elsewhere.sqlite3"
        (directory / DATABASE_NAME).rename(target)
        (directory / DATABASE_NAME).symlink_to(target)
        before = (target.read_bytes(), target.stat().st_mtime_ns)

        self.refuse(directory)

        assert (target.read_bytes(), target.stat().st_mtime_ns) == before

    def test_a_symlink_in_place_of_the_directory_is_refused(self, tmp_path: Path) -> None:
        real = written(tmp_path)
        moved = tmp_path / "moved"
        real.rename(moved)
        real.symlink_to(moved)

        self.refuse(real)

    def test_a_file_in_place_of_the_directory_is_refused(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        directory.parent.mkdir(parents=True)
        directory.write_text("not a directory")

        self.refuse(directory)

    def test_a_directory_in_place_of_the_database_is_refused(self, tmp_path: Path) -> None:
        directory = history_directory(tmp_path)
        (directory / DATABASE_NAME).mkdir(parents=True)

        self.refuse(directory)

    def test_a_fifo_in_place_of_the_database_is_refused_without_waiting(
        self, tmp_path: Path
    ) -> None:
        directory = history_directory(tmp_path)
        directory.mkdir(parents=True)
        os.mkfifo(directory / DATABASE_NAME)

        self.refuse(directory)  # (the test timeout is what fails if this waits for a writer)

    def test_a_database_with_a_second_hard_link_is_refused(self, tmp_path: Path) -> None:
        directory = written(tmp_path)
        os.link(directory / DATABASE_NAME, tmp_path / "second-name")

        self.refuse(directory)

    @pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
    def test_a_planted_symlink_in_place_of_a_side_file_is_refused(
        self, tmp_path: Path, suffix: str
    ) -> None:
        directory = written(tmp_path)
        victim = tmp_path / "victim"
        victim.write_text("important")
        for leftover in directory.glob(f"{DATABASE_NAME}-*"):
            leftover.unlink()
        (directory / f"{DATABASE_NAME}{suffix}").symlink_to(victim)

        self.refuse(directory)

        assert victim.read_text() == "important"

    @pytest.mark.parametrize("what", ["directory", "file", "lock directory"])
    def test_something_that_belongs_to_another_user_is_refused_not_repaired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, what: str
    ) -> None:
        directory = written(tmp_path)
        wrong = {
            "directory": directory,
            "file": directory / DATABASE_NAME,
            "lock directory": directory / "chat.locks",
        }[what]
        wrong.chmod(0o755 if what != "file" else 0o644)
        owner = wrong.stat().st_uid
        real = files.current_uid
        # Only the one thing "belongs to" someone else: everything else is still ours.
        original_fstat = os.fstat

        def fstat(descriptor: int) -> os.stat_result:
            info = original_fstat(descriptor)
            if (info.st_dev, info.st_ino) == (wrong.stat().st_dev, wrong.stat().st_ino):
                return os.stat_result(
                    (info.st_mode, info.st_ino, info.st_dev, info.st_nlink, owner + 1, *info[5:])
                )
            return info

        monkeypatch.setattr(os, "fstat", fstat)
        assert real() == owner

        self.refuse(directory)

        assert mode(wrong) in (0o755, 0o644)  # not "repaired" (it is not ours to chmod)


class TestTheDescriptorFunctionsThemselves:
    """Each defence on its own: the checks after SQLite opens the file would catch a symlink too,
    so the store's tests alone cannot tell whether `O_NOFOLLOW` is there."""

    def test_a_symlink_is_not_opened_and_what_it_points_at_is_not_touched(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "target"
        target.write_text("not ours to change")
        target.chmod(0o644)
        directory = tmp_path / "dir"
        directory.mkdir(mode=0o700)
        (directory / "name").symlink_to(target)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)

        try:
            with pytest.raises(HistoryRefusedError):
                files.open_private_file(descriptor, "name")
        finally:
            os.close(descriptor)

        assert mode(target) == 0o644  # not "repaired" through the link
        assert target.read_text() == "not ours to change"

    def test_a_symlink_in_place_of_a_directory_is_not_opened(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        link = tmp_path / "link"
        link.symlink_to(real)

        with pytest.raises(HistoryRefusedError):
            files.open_private_directory(link)

    def test_a_new_file_is_0600_and_an_existing_one_keeps_its_content(self, tmp_path: Path) -> None:
        directory = tmp_path / "dir"
        directory.mkdir(mode=0o700)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            first = files.open_private_file(descriptor, "name")
            os.write(first, b"content")
            os.close(first)
            second = files.open_private_file(descriptor, "name")
            assert os.pread(second, 100, 0) == b"content"
            os.close(second)
        finally:
            os.close(descriptor)

        assert mode(directory / "name") == 0o600


class TestALockFileThatIsGone:
    def claims(self, tmp_path: Path) -> tuple[SessionClaims, int]:
        directory = tmp_path / "dir"
        directory.mkdir(mode=0o700)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        return SessionClaims(descriptor), descriptor

    def test_a_lock_file_unlinked_since_it_was_opened_is_no_claim_and_no_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        claims, descriptor = self.claims(tmp_path)
        real = os.fstat

        def fstat(fd: int) -> os.stat_result:
            info = real(fd)
            if stat.S_ISREG(info.st_mode):
                return os.stat_result(
                    (info.st_mode, info.st_ino, info.st_dev, 0, info.st_uid, *info[5:])
                )
            return info

        monkeypatch.setattr(os, "fstat", fstat)
        try:
            with caplog.at_level(logging.WARNING):
                assert claims.try_claim(SessionId.new()) is None
        finally:
            claims.close()
            os.close(descriptor)

        assert caplog.text == ""  # "gone" is ordinary: nothing to warn about

    def test_a_lock_file_with_a_second_link_is_still_refused_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        claims, descriptor = self.claims(tmp_path)
        session = SessionId.new()
        try:
            first = claims.try_claim(session)
            assert first is not None
            first.release()
            os.link(tmp_path / "dir" / "chat.locks" / f"{session}.lock", tmp_path / "second")
            with caplog.at_level(logging.WARNING):
                assert claims.try_claim(session) is None
        finally:
            claims.close()
            os.close(descriptor)

        assert "more than one link" in caplog.text


class TestSideFiles:
    def directory(self, tmp_path: Path) -> int:
        (tmp_path / "dir").mkdir(mode=0o700)
        return os.open(tmp_path / "dir", os.O_RDONLY | os.O_DIRECTORY)

    def test_an_absent_side_file_is_fine_and_a_required_one_is_not(self, tmp_path: Path) -> None:
        descriptor = self.directory(tmp_path)
        try:
            files.check_sidecar(descriptor, "chat.sqlite3-wal")
            with pytest.raises(HistoryRefusedError):
                files.require_sidecar(descriptor, "chat.sqlite3-wal")
        finally:
            os.close(descriptor)

    def test_a_side_file_a_closing_connection_just_deleted_counts_as_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        descriptor = self.directory(tmp_path)
        (tmp_path / "dir" / "chat.sqlite3-wal").write_bytes(b"")
        real = os.stat

        def gone(path: str, **options: object) -> os.stat_result:
            info = real(path, **options)  # type: ignore[arg-type]  # a test double
            return os.stat_result(
                (info.st_mode, info.st_ino, info.st_dev, 0, info.st_uid, *info[5:])
            )

        monkeypatch.setattr(os, "stat", gone)
        try:
            with caplog.at_level(logging.WARNING):
                files.check_sidecar(descriptor, "chat.sqlite3-wal")  # no refusal, no warning
        finally:
            os.close(descriptor)
        assert caplog.text == ""

    def test_a_side_file_with_a_second_link_is_still_refused(self, tmp_path: Path) -> None:
        descriptor = self.directory(tmp_path)
        (tmp_path / "dir" / "chat.sqlite3-wal").write_bytes(b"")
        os.link(tmp_path / "dir" / "chat.sqlite3-wal", tmp_path / "second")
        try:
            with pytest.raises(HistoryRefusedError):
                files.check_sidecar(descriptor, "chat.sqlite3-wal")
        finally:
            os.close(descriptor)

    def test_same_resolved_does_not_follow_a_symlink_for_the_file(self, tmp_path: Path) -> None:
        directory = tmp_path / "dir"
        directory.mkdir(mode=0o700)
        (directory / "real").write_bytes(b"")
        (directory / "link").symlink_to(directory / "real")
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        file_fd = os.open(directory / "real", os.O_RDWR)
        try:
            assert files.same_resolved(str(directory / "real"), file_fd, dir_fd)
            # The same inode, reached through a link: SQLite never reports a link (it resolves
            # it), so a link here means something is wrong: not accepted.
            assert not files.same_resolved(str(directory / "link"), file_fd, dir_fd)
        finally:
            os.close(file_fd)
            os.close(dir_fd)


class TestSwappedUnderUs:
    def test_a_database_replaced_between_our_check_and_sqlite_opening_it_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = history_directory(tmp_path)
        real_connect = sqlite3.connect

        def swapping_connect(path: str, *, timeout: float, autocommit: bool) -> sqlite3.Connection:
            # What an attacker who can write in the directory would do in that window.
            (directory / DATABASE_NAME).unlink()
            (directory / DATABASE_NAME).write_bytes(b"")
            return real_connect(path, timeout=timeout, autocommit=autocommit)

        monkeypatch.setattr(sqlite3, "connect", swapping_connect)

        with pytest.raises(HistoryRefusedError, match="replaced"):
            Database.open(directory)

    def test_a_side_file_planted_after_the_check_never_makes_us_write_through_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = history_directory(tmp_path)
        real_connect = sqlite3.connect
        victim = tmp_path / "victim"
        victim.write_text("important")

        def planting_connect(path: str, *, timeout: float, autocommit: bool) -> sqlite3.Connection:
            (directory / f"{DATABASE_NAME}-wal").symlink_to(victim)
            return real_connect(path, timeout=timeout, autocommit=autocommit)

        monkeypatch.setattr(sqlite3, "connect", planting_connect)

        try:
            Database.open(directory).close()
        except HistoryRefusedError:
            pass  # caught by our second look
        else:
            # Or SQLite replaced the stale link with a file of its own (measured).
            assert not (directory / f"{DATABASE_NAME}-wal").is_symlink()
        assert victim.read_text() == "important"
