"""The chat history inside the real shell window (S04 acceptance): the last session is restored on
start, what is typed is saved, "New session" starts afresh, and a history that cannot be used
leaves a working chat and a notice. Headless GTK, like the other window tests."""

import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.history.support import (
    assistant,
    history_directory,
    insert_message,
    insert_session,
    open_store,
    raw,
    user,
)
from tests.shell.gtk_support import (
    Shell,
    click_send,
    descendants,
    input_text,
    rows,
    type_text,
)
from tumbleweed.agent.echo import EchoResponder
from tumbleweed.core.ids import SessionId
from tumbleweed.history.db import DATABASE_NAME
from tumbleweed.history.store import NOT_RESTORED, HistoryStore
from tumbleweed.shell.viewmodel.session import RESTORE_MESSAGES

pytestmark = pytest.mark.gtk

type Build = Callable[[Path], Shell]


@pytest.fixture
def build_with_history() -> Iterator[Build]:
    """Builds a shell whose history is in the given scratch directory; ends every one safely."""
    built: list[Shell] = []

    def build(root: Path) -> Shell:
        from tumbleweed.shell._gi import Gio
        from tumbleweed.shell.app import ShellApplication
        from tumbleweed.shell.window import ShellWindow

        directory = history_directory(root)

        def open_history() -> HistoryStore:
            return HistoryStore.open(directory)

        responder = EchoResponder()
        app = ShellApplication(
            responder, flags=Gio.ApplicationFlags.NON_UNIQUE, open_history=open_history
        )
        app.register(None)
        shell = Shell(app, ShellWindow(app, responder, open_history))
        built.append(shell)
        return shell

    yield build
    for shell in built:
        shell.window.shutdown()  # releases the history (idempotent)
        if shell.window.get_application() is not None:
            shell.window.destroy()


def say(shell: Shell, text: str) -> None:
    type_text(shell, text)
    click_send(shell)
    assert input_text(shell) == ""


def notice(shell: Shell) -> str:
    label = shell.window.chat_panel.history_notice
    return label.get_text() if label.get_visible() else ""


def new_session(shell: Shell) -> None:
    button = shell.window.chat_panel.new_session_button
    assert button is not None
    button.emit("clicked")


class TestRestoreOnStart:
    def test_the_last_session_is_in_the_window_when_it_opens(
        self, tmp_path: Path, build_with_history: Build
    ) -> None:
        store = open_store(tmp_path)
        store.begin(limit=10)
        for message in (user("remember me"), assistant("I do")):
            store.append(message)
        store.close()

        shell = build_with_history(tmp_path)

        assert rows(shell) == [("You", "remember me", ""), ("Assistant", "I do", "")]
        assert notice(shell) == ""

    def test_a_first_start_is_an_empty_chat(
        self, tmp_path: Path, build_with_history: Build
    ) -> None:
        shell = build_with_history(tmp_path)

        assert rows(shell) == []
        assert notice(shell) == ""
        assert (history_directory(tmp_path) / DATABASE_NAME).is_file()

    def test_what_is_typed_survives_a_restart(
        self, tmp_path: Path, build_with_history: Build
    ) -> None:
        first = build_with_history(tmp_path)
        say(first, "does this survive?")
        say(first, "and this?")
        before = rows(first)
        first.window.shutdown()
        first.window.destroy()

        second = build_with_history(tmp_path)

        assert rows(second) == before
        assert [text for _, text, _ in before] == [
            "does this survive?",
            "Echo: does this survive?",
            "and this?",
            "Echo: and this?",
        ]

    def test_the_restored_chat_goes_on_and_is_saved_again(
        self, tmp_path: Path, build_with_history: Build
    ) -> None:
        first = build_with_history(tmp_path)
        say(first, "one")
        first.window.shutdown()
        first.window.destroy()
        second = build_with_history(tmp_path)

        say(second, "two")
        second.window.shutdown()
        second.window.destroy()

        third = build_with_history(tmp_path)
        assert [text for _, text, _ in rows(third)] == ["one", "Echo: one", "two", "Echo: two"]

    def test_restored_text_is_shown_as_plain_text(
        self, tmp_path: Path, build_with_history: Build
    ) -> None:
        markup = '<b>bold</b> <span foreground="red">&amp;</span> <unclosed'
        store = open_store(tmp_path)
        store.begin(limit=10)
        store.append(user(markup))
        store.close()

        shell = build_with_history(tmp_path)

        from tumbleweed.shell._gi import Gtk

        (row,) = rows(shell)
        assert row[1] == markup
        labels = [
            w
            for w in descendants(shell.window.chat_panel.transcript_list)
            if isinstance(w, Gtk.Label)
        ]
        assert not any(label.get_use_markup() for label in labels)

    def test_a_long_session_opens_in_reasonable_time(
        self, tmp_path: Path, build_with_history: Build
    ) -> None:
        session = SessionId.new()
        open_store(tmp_path).close()
        with raw(history_directory(tmp_path)) as connection:
            insert_session(connection, session, 1_700_000_000_000)
            connection.execute("BEGIN")
            for seq in range(1, 3001):
                insert_message(
                    connection,
                    session,
                    seq,
                    role="user" if seq % 2 else "assistant",
                    text=f"message {seq} " + "word " * 40,
                )
            connection.execute("COMMIT")
        started = time.monotonic()

        shell = build_with_history(tmp_path)

        elapsed = time.monotonic() - started
        assert len(rows(shell)) == RESTORE_MESSAGES
        assert elapsed < 20.0, f"restoring {RESTORE_MESSAGES} messages took {elapsed:.1f}s"

    def test_a_file_of_maximal_valid_messages_opens_the_window_in_reasonable_time(
        self, tmp_path: Path, build_with_history: Build
    ) -> None:
        session = SessionId.new()
        open_store(tmp_path).close()
        with raw(history_directory(tmp_path)) as connection:
            insert_session(connection, session, 1_700_000_000_000)
            connection.execute("BEGIN")
            for seq in range(1, 1001):
                insert_message(
                    connection,
                    session,
                    seq,
                    role="assistant",
                    text=("lorem ipsum " * 5500)[:65_000],
                )
            connection.execute("COMMIT")
        started = time.monotonic()

        shell = build_with_history(tmp_path)

        elapsed = time.monotonic() - started
        shown = len(rows(shell))
        assert 0 < shown <= 4 * 1024 * 1024 // 65_000  # the byte budget, not 998 messages
        assert elapsed < 10.0, f"restoring {shown} maximal messages took {elapsed:.1f}s"


class TestNewSession:
    def test_the_button_empties_the_chat_and_the_next_start_is_empty_too(
        self, tmp_path: Path, build_with_history: Build
    ) -> None:
        first = build_with_history(tmp_path)
        say(first, "old business")

        new_session(first)

        assert rows(first) == []
        say(first, "fresh start")
        assert [text for _, text, _ in rows(first)] == ["fresh start", "Echo: fresh start"]
        first.window.shutdown()
        first.window.destroy()
        second = build_with_history(tmp_path)
        assert [text for _, text, _ in rows(second)] == ["fresh start", "Echo: fresh start"]

    def test_there_is_a_button_for_it(self, tmp_path: Path, build_with_history: Build) -> None:
        from tumbleweed.shell._gi import Gtk

        shell = build_with_history(tmp_path)

        button = shell.window.chat_panel.new_session_button
        assert isinstance(button, Gtk.Button)
        assert button.get_label() == "New session"
        assert button.is_ancestor(shell.window)

    def test_it_clears_a_notice_about_a_send_that_was_refused(
        self, tmp_path: Path, build_with_history: Build
    ) -> None:
        shell = build_with_history(tmp_path)
        say(shell, "q")

        new_session(shell)

        assert shell.window.chat_panel.notice.get_text() == ""
        assert shell.window.chat_panel.send_button.get_sensitive() is False  # nothing typed yet
        type_text(shell, "again")
        assert shell.window.chat_panel.send_button.get_sensitive() is True


class TestTwoShells:
    def test_the_second_shell_does_not_take_the_first_ones_session(
        self, tmp_path: Path, build_with_history: Build
    ) -> None:
        first = build_with_history(tmp_path)
        say(first, "mine")

        second = build_with_history(tmp_path)

        assert rows(second) == []
        say(second, "theirs")
        assert [text for _, text, _ in rows(first)] == ["mine", "Echo: mine"]


class TestAHistoryThatCannotBeUsed:
    def test_a_file_that_is_not_a_database_leaves_a_working_chat_and_a_notice(
        self, tmp_path: Path, build_with_history: Build
    ) -> None:
        directory = history_directory(tmp_path)
        directory.mkdir(parents=True)
        directory.chmod(0o700)
        (directory / DATABASE_NAME).write_bytes(b"GARBAGE-NOT-SQL " * 64)

        shell = build_with_history(tmp_path)

        assert "will not be saved" in notice(shell)
        assert "GARBAGE" not in notice(shell)
        say(shell, "hello anyway")
        assert [text for _, text, _ in rows(shell)] == ["hello anyway", "Echo: hello anyway"]
        assert (directory / DATABASE_NAME).read_bytes() == b"GARBAGE-NOT-SQL " * 64  # untouched

    def test_a_symlink_where_the_database_goes_is_refused(
        self, tmp_path: Path, build_with_history: Build
    ) -> None:
        directory = history_directory(tmp_path)
        directory.mkdir(parents=True)
        directory.chmod(0o700)
        target = tmp_path / "somewhere-else"
        target.write_text("not ours")
        (directory / DATABASE_NAME).symlink_to(target)

        shell = build_with_history(tmp_path)

        assert "not safe to use" in notice(shell)
        say(shell, "hi")
        assert target.read_text() == "not ours"

    def test_a_damaged_last_session_is_not_restored_and_a_fresh_one_starts(
        self, tmp_path: Path, build_with_history: Build
    ) -> None:
        directory, session = history_directory(tmp_path), SessionId.new()
        open_store(tmp_path).close()
        with raw(directory) as connection:
            insert_session(connection, session, 1_700_000_000_000)
            insert_message(connection, session, 1, text="x" * 70_000, role="assistant")

        shell = build_with_history(tmp_path)

        assert notice(shell) == NOT_RESTORED
        assert rows(shell) == []
        say(shell, "a new start")
        shell.window.shutdown()
        shell.window.destroy()
        again = build_with_history(tmp_path)
        assert [text for _, text, _ in rows(again)] == ["a new start", "Echo: a new start"]
        assert notice(again) == ""
