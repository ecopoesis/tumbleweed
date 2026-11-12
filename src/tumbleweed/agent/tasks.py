"""The agent's tasks: which one is current, and which turns of the conversation the model may see.

The broker owns tasks and taint (D17, I12); this module keeps what the agent needs to ask it
rightly and holds **no taint** (D26). It keeps the current `TaskId`, the title the model gave it,
and the **context set**: the tasks whose turns the model may be sent. A task whose context inherits
turns starts with the taint of the one it replaces (the broker copies it, `start_task` with
`fresh_context=false`), so the set grows with the same unbroken chain of non-fresh starts along
which taint is copied, and `fresh_context=true` resets it to the new task alone.

A task is started when a broker tool first needs one, not when a reply starts: a shell that is only
chatting never connects to the broker. One reply is one run (`RunId`): the broker binds a `once`
grant to the run it was asked for, so every broker call of a reply uses the reply's run.

State is read by the GTK main thread (the title in the chrome, the task a new message belongs to)
and written by the agent's worker, so it is held under a lock. Observers are told on the main
thread, through the dispatcher. A start that finishes after the chat session changed (`epoch`)
ends the task it made and fails, so a cancelled reply cannot leave a task behind.
"""

import asyncio
import logging
import threading
import unicodedata
from functools import partial
from typing import TYPE_CHECKING, Final, Protocol, final

from tumbleweed.agent.broker_link import BrokerLink, BrokerUnavailableError
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.core.errors import TumbleweedError
from tumbleweed.core.ids import RunId, TaskId

if TYPE_CHECKING:
    import concurrent.futures

    from tumbleweed.agent.runtime import MainThreadDispatcher

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS: Final = 80
TASK_CALL_SECONDS: Final = 20.0
_KEPT_TITLES: Final = 64


class TaskObserver(Protocol):
    """Told about tasks on the GTK main thread."""

    def task_started(self, task: TaskId, title: str | None) -> None: ...

    def task_ended(self, task: TaskId) -> None: ...


class TaskError(TumbleweedError):
    """A task could not be started; the message is fixed text for the model."""


class TaskUnsureError(TaskError):
    """The broker did not answer a start in time: it may or may not have made the task."""


def _capped_marks(text: str) -> str:
    """`text` with no more than two combining marks in a row (a pile of them is a tall smear)."""
    kept: list[str] = []
    run = 0
    for char in text:
        run = run + 1 if unicodedata.category(char) in ("Mn", "Me") else 0
        if run <= 2:
            kept.append(char)
    return "".join(kept)


def clean_title(text: str) -> str:
    """The model's title as one plain line of at most `MAX_TITLE_CHARS` characters: control,
    format, separator, private-use and unassigned characters become a space."""
    pieces = [
        " " if unicodedata.category(char)[0] in "CZ" and char != " " else char for char in text
    ]
    line = " ".join(_capped_marks("".join(pieces)).split())
    return line if len(line) <= MAX_TITLE_CHARS else line[: MAX_TITLE_CHARS - 1].rstrip() + "…"


@final
class TaskSession:
    """See the module docstring."""

    def __init__(self, link: BrokerLink, dispatcher: "MainThreadDispatcher") -> None:
        self._link = link
        self._dispatcher = dispatcher
        self._lock = threading.Lock()
        self._observers: list[TaskObserver] = []
        self._current: TaskId | None = None
        self._head: TaskId | None = None  # the task to inherit from when none is current
        self._context: frozenset[TaskId] = frozenset()
        self._titles: dict[TaskId, str] = {}
        self._generation = 0
        self._last_fresh = False
        self._epoch = 0
        self._unverified = False
        self._run = RunId.new()
        self._reply_first: TaskId | None = None
        self._reply_fresh = False
        self._reply_open = False
        self._reply_had = False  # a task was current at some point in this reply
        link.on_lost(self._connection_lost)

    # --- reading (any thread) --------------------------------------------------------------

    @property
    def current(self) -> TaskId | None:
        with self._lock:
            return self._current

    @property
    def generation(self) -> int:
        """Counts the starts of a task that change what the model may keep (D26 (4))."""
        with self._lock:
            return self._generation

    @property
    def last_switch_fresh(self) -> bool:
        with self._lock:
            return self._last_fresh

    @property
    def unverified(self) -> bool:
        """Whether the lineage of a restored chat has not yet been checked with the broker."""
        with self._lock:
            return self._unverified

    @property
    def run(self) -> RunId:
        with self._lock:
            return self._run

    def title_of(self, task: TaskId) -> str | None:
        with self._lock:
            return self._titles.get(task)

    def visible(self, turn_task: TaskId | None) -> bool:
        """Whether a turn of `turn_task` may be sent to the model now."""
        with self._lock:
            return turn_task is None or turn_task in self._context

    def subscribe(self, observer: TaskObserver) -> None:
        self._observers.append(observer)

    # --- the chat session (main thread) ----------------------------------------------------

    def restore(self, head: TaskId | None) -> None:
        """A chat was restored whose newest tagged message belongs to `head`: the model may see
        that task's turns, and the next task inherits from it (D26 (3))."""
        with self._lock:
            self._epoch += 1
            self._head = head
            self._context = frozenset() if head is None else frozenset({head})
            self._unverified = head is not None

    def end_session(self) -> None:
        """A new chat session: the task ends, and nothing carries over."""
        with self._lock:
            self._epoch += 1
            old, self._current = self._current, None
            self._head = None
            self._context = frozenset()
            self._unverified = False
            self._generation += 1
            self._last_fresh = True
        if old is not None:
            self._notify_ended(old)
            self._end_on_broker(old)

    # --- a reply (the worker) --------------------------------------------------------------

    def begin_reply(self) -> RunId:
        with self._lock:
            self._run = RunId.new()
            self._reply_first = self._current if self._current is not None else self._head
            self._reply_had = self._current is not None
            self._reply_fresh = False
            self._reply_open = True
            return self._run

    def reply_tag(self) -> TaskId | None:
        """The task to tag the reply with: the one it began in if a fresh start happened in it,
        else the last one (D26 (3))."""
        with self._lock:
            self._reply_open = False
            if self._reply_fresh:
                return self._reply_first
            # Never None while the context set is not empty: None is visible to every task. After
            # a lost connection or a restore that could not be checked, the task to inherit from
            # (the head) is the one the reply belongs to.
            for task in (self._current, self._head, self._reply_first):
                if task is not None:
                    return task
            return None

    # --- starting tasks (the worker) -------------------------------------------------------

    async def ensure(self) -> TaskId:
        """The current task, starting the default one if there is none."""
        with self._lock:
            current = self._current
        if current is not None:
            return current
        return await self._begin(None, fresh=False)

    async def start(self, title: str, *, fresh_context: bool) -> TaskId:
        """`start_task`: end the current task and start another (D26 (1))."""
        return await self._begin(clean_title(title) or None, fresh=fresh_context)

    async def _begin(self, title: str | None, *, fresh: bool) -> TaskId:
        with self._lock:
            epoch = self._epoch
            previous = self._current if self._current is not None else self._head
        refused_previous = False
        try:
            try:
                started = await self._start_on_broker(previous, fresh)
            except BrokerRefused as refusal:
                if previous is None or refusal.reason != "unknown_task":
                    raise TaskError(f"The task could not be started: {refusal.message}.") from None
                # The broker does not know the task to inherit from: nothing of it can be trusted to
                # carry over, so start clean and drop the turns of the lineage (fail closed).
                refused_previous = True
                try:
                    started = await self._start_on_broker(None, False)
                except BrokerRefused as again:
                    raise TaskError(f"The task could not be started: {again.message}.") from None
        except (asyncio.CancelledError, TaskUnsureError):
            # The broker may have ended the task we had and made another: we cannot tell. Do not
            # go on as if the old task were current; the next start inherits from it as the head.
            self._abandon(epoch, previous)
            raise
        task = started
        old: TaskId | None = None
        with self._lock:
            stale = epoch != self._epoch
            if not stale:
                old = self._current
                self._current = task
                self._head = None
                if title is not None:
                    self._titles[task] = title
                    while len(self._titles) > _KEPT_TITLES:
                        del self._titles[next(iter(self._titles))]
                reset = fresh or refused_previous
                self._context = frozenset({task}) if reset else self._context | {task}
                self._unverified = False
                self._last_fresh = reset
                if self._reply_open:
                    if self._reply_first is None:
                        self._reply_first = task
                    if self._reply_had or reset:
                        self._generation += 1
                    self._reply_fresh = self._reply_fresh or fresh
                    self._reply_had = True
                elif reset:
                    self._generation += 1
        if stale:
            self._end_on_broker(task)
            raise TaskError("The chat session changed; the task was not started.")
        if old is not None and old != task:
            self._notify_ended(old)
        self._notify_started(task, title)
        return task

    async def _start_on_broker(self, previous: TaskId | None, fresh: bool) -> TaskId:
        async def work(client: BrokerClient) -> TaskId:
            return (await client.start_task(previous=previous, fresh_context=fresh)).task

        try:
            return await self._link.call(work, deadline=TASK_CALL_SECONDS)
        except BrokerUnavailableError as error:
            raise TaskUnsureError(str(error)) from None

    def _abandon(self, epoch: int, previous: TaskId | None) -> None:
        with self._lock:
            if epoch != self._epoch or previous is None or self._current != previous:
                return
            self._head, self._current = previous, None
        self._notify_ended(previous)

    # --- the end of a connection (the link's thread) ---------------------------------------

    def _connection_lost(self) -> None:
        with self._lock:
            old, self._current = self._current, None
            if old is not None:
                self._head = old
        if old is not None:
            self._notify_ended(old)

    # --- helpers ---------------------------------------------------------------------------

    def _end_on_broker(self, task: TaskId) -> None:
        async def work(client: BrokerClient) -> None:
            await client.end_task(task)

        try:
            future = self._link.submit(work, deadline=TASK_CALL_SECONDS)
        except BrokerUnavailableError:
            return  # the broker ends a task whose connection ends
        future.add_done_callback(_log_end)

    def _notify_started(self, task: TaskId, title: str | None) -> None:
        for observer in tuple(self._observers):
            self._dispatcher.post(partial(observer.task_started, task, title))

    def _notify_ended(self, task: TaskId) -> None:
        for observer in tuple(self._observers):
            self._dispatcher.post(partial(observer.task_ended, task))


def _log_end(future: "concurrent.futures.Future[None]") -> None:
    if not future.cancelled() and (error := future.exception()) is not None:
        logger.info("ending a task on the broker failed: %s", type(error).__name__)
