"""A value that arrives over a connection (S25, D31 (4)): what it writes and what it starts.

`connection_input` writes the value as a patch is written (whole or refused), then runs only the
`sandbox` handlers that read that input, each with an event that names the connection, which is
what makes the run a `connection` run (no tool grant). No `set` or `agent` handler fires.
"""

import pytest

from tests.ui.runtime.support import key, recorded
from tests.ui.runtime.test_runtime import with_sinks
from tumbleweed.core.ids import ConnectionId
from tumbleweed.ui.runtime import Dispatch, Origin, UiEvent
from tumbleweed.ui.spec import SpecError


class TestConnectionInput:
    def test_the_run_it_starts_names_the_connection(self) -> None:
        runtime, sandbox, _agent = with_sinks()
        connection = ConnectionId.new()

        result = runtime.connection_input(key("n"), 7, connection)

        assert result is Dispatch.SENT
        assert runtime.store.value(key("n")) == 7
        ((handler, event, inputs),) = sandbox.calls
        assert str(handler.name) == "run"
        assert event.connection == connection  # without it the run would be a user's
        assert (event.component, event.event, event.bind) == ("connection", "change", key("n"))
        assert inputs == {key("n"): 7, key("m"): 2}

    def test_only_handlers_that_read_the_input_run_and_never_a_set_or_agent_handler(self) -> None:
        runtime, sandbox, agent = with_sinks()

        runtime.connection_input(key("m"), 5, ConnectionId.new())
        assert len(sandbox.calls) == 1

        result = runtime.connection_input(key("b"), True, ConnectionId.new())  # no handler reads it

        assert result is Dispatch.IGNORED
        assert len(sandbox.calls) == 1
        assert agent.calls == []
        assert runtime.store.value(key("n")) == 1  # the `set` handler did not bump it

    def test_a_user_event_has_no_connection(self) -> None:
        runtime, sandbox, _agent = with_sinks()

        runtime.user_event(UiEvent("run", "button", "click"))  # type: ignore[arg-type]  # names

        ((_handler, event, _inputs),) = sandbox.calls
        assert event.connection is None

    def test_a_value_the_spec_refuses_changes_nothing_and_starts_nothing(self) -> None:
        runtime, sandbox, _agent = with_sinks()

        with pytest.raises(SpecError):
            runtime.connection_input(key("n"), "not a number", ConnectionId.new())

        assert runtime.store.value(key("n")) == 1
        assert sandbox.calls == []

    def test_the_change_is_announced_as_a_connections_own(self) -> None:
        runtime, _sandbox, _agent = with_sinks()
        changes = recorded(runtime)

        runtime.connection_input(key("n"), 3, ConnectionId.new())

        assert [(c.key, c.value, c.origin) for c in changes if c.key == key("n")] == [
            (key("n"), 3, Origin.CONNECTION)
        ]

    def test_with_no_sandbox_the_value_is_still_written(self) -> None:
        from tests.ui.runtime.support import number, runtime_with

        runtime = runtime_with([number("n", 1)])

        assert runtime.connection_input(key("n"), 4, ConnectionId.new()) is Dispatch.NO_SINK
        assert runtime.store.value(key("n")) == 4
