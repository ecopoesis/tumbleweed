"""The shell's one connection to the broker (S19, D26 (8), #89)."""
# ruff: noqa: PT018  (compound assertions read better here)

import asyncio
import time
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
