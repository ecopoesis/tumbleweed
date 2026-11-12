"""A real broker daemon on its own thread and event loop, for the GTK tests of the prompt (S18).

The shell's window lives on the test's main thread, which pumps the GLib main loop; the broker, its
IPC server and the shell's client connection run on another thread with their own asyncio loop,
as the shell's agent runtime does. The client's request handler is the window's own
`broker_prompts`, so a request that needs the user shows a real prompt in a real window, and the
test, standing in for the user, clicks a real button.

Nothing here blocks the main thread: `wait` pumps GLib until the broker's side is done.
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, final

from tests.broker.rig import Rig, running
from tests.shell.gtk_support import pump_until
from tumbleweed.broker.client import BrokerClient
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.core.ids import TaskId
from tumbleweed.ipc.connection import RequestHandler

if TYPE_CHECKING:
    import concurrent.futures

DEADLINE = 20.0


@final
class LiveBroker:
    """A started broker with one connected client and one live task, until `stop`."""

    def __init__(
        self,
        handler: RequestHandler,
        *,
        config: Callable[[BrokerConfig], BrokerConfig] | None = None,
    ) -> None:
        self._handler = handler
        self._config = config
        self._ready = threading.Event()
        self._failure: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._rig: Rig | None = None
        self._client: BrokerClient | None = None
        self._task: TaskId | None = None
        self._thread = threading.Thread(target=self._main, daemon=True, name="live-broker")
        self._thread.start()
        assert pump_until(self._ready.is_set, timeout=DEADLINE), "the broker did not start"
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
        async with running(ask_shell=True, config=self._config) as rig:
            self._rig = rig
            self._client = await rig.client(handler=self._handler)
            self._task = (await self._client.start_task()).task
            self._ready.set()
            await self._stop.wait()

    @property
    def rig(self) -> Rig:
        assert self._rig is not None
        return self._rig

    @property
    def client(self) -> BrokerClient:
        assert self._client is not None
        return self._client

    @property
    def task(self) -> TaskId:
        assert self._task is not None
        return self._task

    def submit[T](self, work: Awaitable[T]) -> "concurrent.futures.Future[T]":
        """Start `work` on the broker's loop."""

        async def run() -> T:
            return await work

        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(run(), self._loop)

    def wait[T](self, future: "concurrent.futures.Future[T]", timeout: float = DEADLINE) -> T:
        """The result of `future`, with the main loop running meanwhile."""
        assert pump_until(future.done, timeout=timeout), "the broker did not answer in time"
        return future.result()

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=DEADLINE)
