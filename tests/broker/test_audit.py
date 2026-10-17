"""The audit log: append-only JSONL of typed entries, written whole or not at all (I7)."""

import errno
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tumbleweed.broker.audit import (
    AUDIT_FILE_NAME,
    MAX_ENTRY_BYTES,
    AuditLog,
    GrantDecision,
    GrantRevoked,
    TaskEnded,
    TaskEndReason,
    TaskStarted,
    cut,
    parse_entry,
    read_entries,
)
from tumbleweed.broker.errors import AuditError, PrivateFsError
from tumbleweed.core.ids import GrantId, RunId, TaskId

AT = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def started(task: TaskId | None = None) -> TaskStarted:
    return TaskStarted(
        at=AT, task=task or TaskId.new(), previous=None, fresh_context=False, taint=()
    )


def decision(path: str = "/home/u/Photos") -> GrantDecision:
    return GrantDecision(
        at=AT,
        task=TaskId.new(),
        run=RunId.new(),
        capability="fs.read",
        path=path,
        outcome="denied",
        grant=None,
        scope=None,
        reason="user_denied",
        model_reason="to look",
    )


class TestTheFile:
    def test_it_is_created_private_in_a_private_directory(self, tmp_path: Path) -> None:
        directory = tmp_path / "broker"

        AuditLog.open(directory).close()

        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / AUDIT_FILE_NAME).stat().st_mode) == 0o600

    def test_entries_are_one_json_object_per_line_in_order(self, tmp_path: Path) -> None:
        log = AuditLog.open(tmp_path / "b")
        first, second = started(), decision()

        log.append(first)
        log.append(second)
        log.close()

        lines = (tmp_path / "b" / AUDIT_FILE_NAME).read_bytes().splitlines()
        assert [json.loads(line)["kind"] for line in lines] == ["task_started", "grant_decision"]
        assert all(json.loads(line)["v"] == 1 for line in lines)
        assert read_entries(tmp_path / "b" / AUDIT_FILE_NAME) == [first, second]

    def test_it_is_appended_to_across_openings_and_never_rewritten(self, tmp_path: Path) -> None:
        directory = tmp_path / "b"
        log = AuditLog.open(directory)
        log.append(started())
        log.close()
        before = (directory / AUDIT_FILE_NAME).read_bytes()

        again = AuditLog.open(directory)
        again.append(started())
        again.close()

        after = (directory / AUDIT_FILE_NAME).read_bytes()
        assert after.startswith(before)
        assert len(after.splitlines()) == 2

    def test_a_newline_in_text_cannot_start_another_entry(self, tmp_path: Path) -> None:
        log = AuditLog.open(tmp_path / "b")

        log.append(decision(path="/a\n" + '{"kind":"task_ended"}'))
        log.close()

        lines = (tmp_path / "b" / AUDIT_FILE_NAME).read_bytes().splitlines()
        assert len(lines) == 1

    def test_an_entry_is_opened_without_following_a_symlink(self, tmp_path: Path) -> None:
        directory = tmp_path / "b"
        directory.mkdir(mode=0o700)
        victim = tmp_path / "victim"
        victim.write_text("keep")
        (directory / AUDIT_FILE_NAME).symlink_to(victim)

        with pytest.raises(PrivateFsError):
            AuditLog.open(directory)

        assert victim.read_text() == "keep"

    def test_a_directory_that_is_a_symlink_is_refused(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        (tmp_path / "b").symlink_to(real)

        with pytest.raises(PrivateFsError):
            AuditLog.open(tmp_path / "b")

    def test_a_loose_mode_on_our_own_file_is_tightened(self, tmp_path: Path) -> None:
        directory = tmp_path / "b"
        AuditLog.open(directory).close()
        (directory / AUDIT_FILE_NAME).chmod(0o666)

        AuditLog.open(directory).close()

        assert stat.S_IMODE((directory / AUDIT_FILE_NAME).stat().st_mode) == 0o600

    def test_a_file_with_two_links_is_refused(self, tmp_path: Path) -> None:
        directory = tmp_path / "b"
        AuditLog.open(directory).close()
        os.link(directory / AUDIT_FILE_NAME, tmp_path / "other-name")

        with pytest.raises(PrivateFsError):
            AuditLog.open(directory)

    def test_a_fifo_in_its_place_is_refused_without_waiting_for_a_writer(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "b"
        directory.mkdir(mode=0o700)
        os.mkfifo(directory / AUDIT_FILE_NAME)

        with pytest.raises(PrivateFsError):
            AuditLog.open(directory)


class TestWhatCannotBeWritten:
    def test_a_write_that_fails_halfway_leaves_no_fragment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        directory = tmp_path / "b"
        log = AuditLog.open(directory)
        log.append(started())
        good = (directory / AUDIT_FILE_NAME).read_bytes()
        real_write = os.write
        calls: list[int] = []

        def failing(fd: int, data: bytes | memoryview) -> int:
            calls.append(len(data))
            if len(calls) == 1:
                return real_write(fd, bytes(data)[:20])  # a short write
            raise OSError(errno.ENOSPC, "no space left on device")

        monkeypatch.setattr(os, "write", failing)
        with pytest.raises(AuditError):
            log.append(started())
        monkeypatch.undo()
        log.close()

        assert (directory / AUDIT_FILE_NAME).read_bytes() == good

    def test_the_error_does_not_repeat_the_entry_or_the_system_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = AuditLog.open(tmp_path / "b")

        def failing(fd: int, data: bytes | memoryview) -> int:
            raise OSError(errno.EIO, "SECRET-DISK-MESSAGE")

        monkeypatch.setattr(os, "write", failing)
        with pytest.raises(AuditError) as caught:
            log.append(decision(path="/home/u/SECRET-PATH"))
        monkeypatch.undo()
        log.close()

        assert "SECRET" not in str(caught.value)

    def test_no_field_can_hold_enough_to_pass_the_entry_limit(self) -> None:
        with pytest.raises(ValueError, match="at most 4096"):
            decision(path="x" * (MAX_ENTRY_BYTES * 2))

    def test_the_log_itself_refuses_an_entry_over_its_limit_and_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = AuditLog.open(tmp_path / "b")
        monkeypatch.setattr("tumbleweed.broker.audit.MAX_ENTRY_BYTES", 50)

        with pytest.raises(AuditError, match="too large"):
            log.append(started())
        monkeypatch.undo()
        log.close()

        assert (tmp_path / "b" / AUDIT_FILE_NAME).read_bytes() == b""

    def test_text_from_outside_is_cut_to_its_field(self) -> None:
        assert len(cut("é" * 10_000)) == 500
        assert cut("abc") == "abc"


class TestReadingBack:
    def test_a_line_that_is_not_an_entry_is_refused_with_a_fixed_message(self) -> None:
        for line in (b"{}", b"not json", b'{"kind":"task_started"}', b'{"v":2,"kind":"x"}'):
            with pytest.raises(AuditError) as caught:
                parse_entry(line)
            assert "not a valid entry" in str(caught.value)

    def test_unknown_fields_are_refused_by_this_version(self) -> None:
        line = started().model_dump_json().encode()
        extra = line[:-1] + b',"content_hash":"abc"}'

        with pytest.raises(AuditError):
            parse_entry(extra)

    def test_every_kind_round_trips(self) -> None:
        entries = [
            started(),
            TaskEnded(at=AT, task=TaskId.new(), reason=TaskEndReason.STARTUP, revoked=()),
            decision(),
            GrantRevoked(at=AT, task=TaskId.new(), grant=GrantId.new(), reason="task_ended"),
        ]

        for entry in entries:
            assert parse_entry(entry.model_dump_json().encode()) == entry
