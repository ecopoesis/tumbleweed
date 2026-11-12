"""The shell's one connection to the broker (S19, D26 (8), #89)."""
# ruff: noqa: PT018  (compound assertions read better here)

import asyncio
import time
from pathlib import Path
from typing import cast

import pytest

from tests.agent.broker_thread import BrokerThread
from tumbleweed.agent.broker_link import BrokerLink, BrokerUnavailableError
from tumbleweed.broker.client import BrokerClient, BrokerConnector
from tumbleweed.core.errors import TumbleweedError
from tumbleweed.ipc.broker_messages import Refused
from tumbleweed.ipc.connection import Connection, RequestHandler
from tumbleweed.ipc.messages import RequestBody, ResponseBody


async def start(client: BrokerClient) -> object:
    return (await client.start_task()).task


def test_it_connects_on_first_use_and_one_client_carries_every_request() -> None:
    broker = BrokerThread()
    try:
        link = broker.link()
        clients: list[BrokerClient] = []

        async def note(client: BrokerClient) -> None:
            clients.append(client)

        for _ in range(3):
            link.call_blocking(note, deadline=5.0)

        assert len(clients) == 3 and all(c is clients[0] for c in clients)
    finally:
        broker.stop()


def test_nothing_connects_until_something_is_asked(tmp_path: object) -> None:
    made: list[int] = []

    def factory(handler: RequestHandler) -> BrokerConnector:
        made.append(1)
        raise TumbleweedError("never reached")

    link = BrokerLink(factory)
    link.close()
    assert made == []


def test_a_broker_that_is_not_there_costs_one_attempt_then_a_pause() -> None:
    attempts: list[float] = []

    def factory(handler: RequestHandler) -> BrokerConnector:
        attempts.append(time.monotonic())
        raise TumbleweedError("no socket")

    link = BrokerLink(factory, retry_floor=0.3, retry_ceiling=1.0)
    try:
        for _ in range(3):
            with pytest.raises(BrokerUnavailableError):
                link.call_blocking(start, deadline=2.0)

        assert len(attempts) == 1  # the pause refuses the next two without trying
        time.sleep(0.35)
        with pytest.raises(BrokerUnavailableError):
            link.call_blocking(start, deadline=2.0)
        assert len(attempts) == 2
    finally:
        link.close()


def test_a_request_before_a_handler_is_attached_is_refused_busy() -> None:
    broker = BrokerThread()
    try:
        link = broker.link()
        link.call_blocking(start, deadline=5.0)
        relay = link._relay  # noqa: SLF001  (the handler the connection was made with)

        async def ask() -> ResponseBody:
            request: RequestBody = _show_prompt()
            # With no handler attached the relay never touches the connection.
            return await relay(request, cast("Connection", None))

        response = asyncio.run(ask())

        assert isinstance(response, Refused) and response.reason == "busy"
    finally:
        broker.stop()


def test_a_blocking_call_gives_up_at_its_deadline() -> None:
    broker = BrokerThread()
    try:
        link = broker.link()

        async def hang(client: BrokerClient) -> None:
            await asyncio.Event().wait()

        began = time.monotonic()
        with pytest.raises(BrokerUnavailableError):
            link.call_blocking(hang, deadline=0.3)
        assert time.monotonic() - began < 3.0
    finally:
        broker.stop()


def test_a_closed_link_refuses_calls() -> None:
    link = BrokerLink(lambda handler: _unreachable())
    link.close()
    with pytest.raises(BrokerUnavailableError):
        link.call_blocking(start, deadline=1.0)


def _unreachable() -> BrokerConnector:
    raise AssertionError("not connected")


def _show_prompt() -> RequestBody:
    from tests.shell.prompt_support import show_message

    return show_message()


def test_a_connect_that_outlasts_the_call_deadline_still_starts_the_pause() -> None:
    # The connector's own patience (30 s) is longer than any call deadline; the link bounds one
    # attempt by less, so the failure counts and the next call is refused at once (round-1 M4).
    from tests.broker.rig import short_directory
    from tests.ipc.paths import unchecked_socket_path

    with short_directory() as base:
        nobody = unchecked_socket_path(Path(str(base)) / "nobody.sock")

        def factory(handler: RequestHandler) -> BrokerConnector:
            return BrokerConnector(nobody, handler=handler)  # give_up: 30 s

        link = BrokerLink(factory, retry_floor=5.0, connect_seconds=0.6)
        try:
            began = time.monotonic()
            with pytest.raises(BrokerUnavailableError):
                link.call_blocking(start, deadline=2.0)
            assert time.monotonic() - began < 1.5  # ended by the attempt's bound, not the call's

            began = time.monotonic()
            with pytest.raises(BrokerUnavailableError):
                link.call_blocking(start, deadline=2.0)
            assert time.monotonic() - began < 0.3  # refused by the pause, not tried again
        finally:
            link.close()


def test_a_call_ended_by_its_own_deadline_while_connecting_also_starts_the_pause() -> None:
    from tests.broker.rig import short_directory
    from tests.ipc.paths import unchecked_socket_path

    with short_directory() as base:
        nobody = unchecked_socket_path(Path(str(base)) / "nobody.sock")
        link = BrokerLink(
            lambda handler: BrokerConnector(nobody, handler=handler),
            retry_floor=5.0,
            connect_seconds=60.0,  # longer than the call: the call's deadline ends the attempt
        )
        try:
            with pytest.raises(BrokerUnavailableError):
                link.call_blocking(start, deadline=0.3)
            began = time.monotonic()
            with pytest.raises(BrokerUnavailableError):
                link.call_blocking(start, deadline=2.0)
            assert time.monotonic() - began < 0.5
        finally:
            link.close()


def test_a_broker_that_is_still_coming_up_is_not_started_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from tests.broker.rig import short_directory
    from tumbleweed.agent import broker_link

    class Pending:
        returncode = None

    started: list[int] = []

    async def fake_spawn(*args: object, **kwargs: object) -> Pending:
        started.append(1)
        return Pending()

    monkeypatch.setattr(broker_link, "spawn_broker_process", fake_spawn)
    with short_directory() as base:
        make = broker_link.connector_from_environment({"XDG_RUNTIME_DIR": str(base)})
        link = BrokerLink(make, retry_floor=0.01, connect_seconds=0.5)
        try:
            for _ in range(3):
                with pytest.raises(BrokerUnavailableError):
                    link.call_blocking(start, deadline=3.0)
                time.sleep(0.05)
        finally:
            link.close()

    assert started == [1]
