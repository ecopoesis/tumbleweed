"""Several shells at once: WAL and busy timeouts keep every write, migrations are safe, and a
session has one owner. These start real processes, as the non-unique shell does."""

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

from tests.history.support import history_directory, open_store, raw
from tumbleweed.history.db import Database
from tumbleweed.history.store import HistoryStore

ROOT = Path(__file__).resolve().parents[2]
DEADLINE = 60


def start(tmp_path: Path, *arguments: str, go: Path | None = None) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603  (argv list, no shell, our own interpreter)
        [
            sys.executable,
            "-m",
            "tests.history.writer",
            str(history_directory(tmp_path)),
            str(go or tmp_path / "go"),
            *arguments,
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish_all(tmp_path: Path, processes: list[subprocess.Popen[str]]) -> list[str]:
    """Let every process report (each holds its history open until all have), then collect the
    session each one used."""
    sessions: list[str] = []
    for process in processes:
        assert process.stdout is not None
        line = process.stdout.readline()
        assert line, process.stderr.read() if process.stderr else ""
        sessions.append(json.loads(line)["session"])
    (tmp_path / "go.done").touch()
    for process in processes:
        _, stderr = process.communicate(timeout=DEADLINE)
        assert process.returncode == 0, stderr
        assert stderr == ""  # no warning, no traceback
    return sessions


class TestTwoWritersAtOnce:
    def test_shells_that_start_together_all_succeed_and_keep_every_message(
        self, tmp_path: Path
    ) -> None:
        # A brand new file: every process migrates and switches to WAL at the same moment.
        shells = [start(tmp_path, "own", f"shell{n}", "150") for n in range(4)]
        (tmp_path / "go").touch()

        sessions = finish_all(tmp_path, shells)

        assert len(set(sessions)) == 4  # nobody shared a session
        with raw(history_directory(tmp_path)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (1,)
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            assert connection.execute("SELECT count(*) FROM messages").fetchone() == (600,)
            for session in sessions:
                rows = connection.execute(
                    "SELECT seq, text FROM messages WHERE session_id = ? ORDER BY seq", (session,)
                ).fetchall()
                assert [seq for seq, _ in rows] == list(range(1, 151))
                tags = {text.rsplit("-", 1)[0] for _, text in rows}
                assert len(tags) == 1  # one shell's messages, in its own session

    def test_two_writers_in_one_session_never_collide_on_a_position(self, tmp_path: Path) -> None:
        store = open_store(tmp_path)
        session = store.begin(limit=10).session
        writers = [start(tmp_path, "shared", f"w{n}", "200", str(session.id)) for n in range(2)]
        (tmp_path / "go").touch()

        finish_all(tmp_path, writers)

        with raw(history_directory(tmp_path)) as connection:
            rows = connection.execute(
                "SELECT seq, text FROM messages WHERE session_id = ? ORDER BY seq",
                (str(session.id),),
            ).fetchall()
        assert [seq for seq, _ in rows] == list(range(1, 401))
        for tag in ("w0", "w1"):  # each writer's own messages stay in its order
            own = [int(text.split("-")[1]) for _, text in rows if text.startswith(tag)]
            assert own == list(range(200))
        store.close()

    def test_a_reader_is_not_blocked_by_a_writer(self, tmp_path: Path) -> None:
        writer = open_store(tmp_path)
        writer.begin(limit=10)
        reader = Database.open(history_directory(tmp_path))

        with writer._database.write() as connection:  # noqa: SLF001  (holds the write lock)
            connection.execute("UPDATE sessions SET updated_at = updated_at + 1")
            started = time.monotonic()
            rows = reader.query("SELECT count(*) FROM sessions")  # WAL: no waiting
            waited = time.monotonic() - started

        assert rows == [(1,)]
        assert waited < 1.0
        reader.close()
        writer.close()


class TestAShellThatDies:
    def test_a_killed_shell_does_not_keep_its_session_claimed(self, tmp_path: Path) -> None:
        history_directory(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        holder = start(tmp_path, "hold")
        (tmp_path / "go").touch()
        assert holder.stdout is not None
        held = json.loads(holder.stdout.readline())["session"]
        try:
            other = open_store(tmp_path)
            assert other.begin(limit=10).session.id != held  # while it lives, it owns it
            other.close()
            holder.send_signal(signal.SIGKILL)
            holder.wait(timeout=DEADLINE)

            third = HistoryStore.open(history_directory(tmp_path))
            restored = third.begin(limit=10)
            third.close()

            assert str(restored.session.id) == held  # the kernel dropped the lock with the process
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=DEADLINE)
            assert holder.stdout is not None
            holder.stdout.close()
            assert holder.stderr is not None
            holder.stderr.close()
