"""A real broker daemon on a thread of its own, for the tests of the agent's broker side (S19).

The agent's code talks to the broker through `BrokerLink`, which has a loop and a thread of its
own, so a test needs the broker on yet another. `BrokerThread` runs `tests.broker.rig.running` on
its loop (a real daemon, a real socket, a scripted user at the broker's `PromptInterface`) and
hands out links that connect to it. The scripted user is the fake at the Protocol boundary; nothing
in `src/` grants without a user's answer.
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import final

from tests.broker.rig import Rig, ScriptedPrompts, running
from tumbleweed.agent.broker_link import BrokerLink
from tumbleweed.broker.client import BrokerConnector
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.broker.service import Sandbox
from tumbleweed.ipc.connection import RequestHandler

DEADLINE = 20.0


@final
class BrokerThread:
    def __init__(
        self,
        *,
        prompts: ScriptedPrompts | None = None,
        config: Callable[[BrokerConfig], BrokerConfig] | None = None,
        runner: Sandbox | None = None,
    ) -> None:
        self._runner = runner
        self._prompts = prompts
        self._config = config
        self._ready = threading.Event()
        self._failure: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._rig: Rig | None = None
        self._links: list[BrokerLink] = []
        self._thread = threading.Thread(target=self._main, daemon=True, name="test-broker")
        self._thread.start()
        assert self._ready.wait(DEADLINE), "the broker did not start"
        if self._failure is not None:
            raise self._failure

    def _main(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as error:  # noqa: BLE001  (reported to the test that started us)
            self._failure = error
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with running(prompts=self._prompts, config=self._config, runner=self._runner) as rig:
            self._rig = rig
            self._ready.set()
            await self._stop.wait()

    @property
    def rig(self) -> Rig:
        assert self._rig is not None
        return self._rig

    def run[T](self, work: Awaitable[T]) -> T:
        """Run `work` on the broker's loop and wait for it."""

        async def go() -> T:
            return await work

        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(go(), self._loop).result(DEADLINE)

    def link(self, handler: RequestHandler | None = None) -> BrokerLink:
        rig = self.rig

        def connector(relay: RequestHandler) -> BrokerConnector:
            return BrokerConnector(
                rig.socket,
                handler=relay,
                first_delay=timedelta(milliseconds=10),
                max_delay=timedelta(milliseconds=50),
                give_up=timedelta(seconds=3),
            )

        link = BrokerLink(connector, retry_floor=0.05, retry_ceiling=0.2)
        if handler is not None:
            link.attach_handler(handler)
        self._links.append(link)
        return link

    def stop(self) -> None:
        for link in self._links:
            link.close()
        if self._loop is not None and self._stop is not None and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=DEADLINE)
