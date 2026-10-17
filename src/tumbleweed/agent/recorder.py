"""The invocation chokepoint (DECISIONS D3): every model call goes through a `RecordedProvider`.

`InvocationRecorder.record(provider, session_id=..., task_id=...)` wraps a provider in a
`RecordedProvider`, and that is the only thing the agent loop accepts (`AgentLoop` is typed with
it, and `tests/agent/test_chokepoint.py` fails if anything but the loop, the recorder and the
provider itself calls a provider's `stream`). The wrapper passes the events on untouched and, when
the call ends, however it ends, appends one JSONL record: `InvocationId`, `SessionId`,
`TaskId | None`, the model, token usage (the last the provider reported, also for a call that
failed or was cut short), latency, the stop reason, the provider's request ID, how it ended
(`ok`, `error`, `cancelled`, `timeout`, `aborted`) and, for a failure, the `ErrorKind`. A call the
provider retried (`AttemptFailed`) has one record per attempt, numbered by `attempt`.

What a record never holds (I6): the API key, any prompt, message or tool text, a provider's error
text. It is built only from validated, closed or length-capped fields, so there is nothing to
scrub, and `MAX_RECORD_BYTES` bounds a line. The file is created `0600` and tightened to it if it
was looser, in a directory tightened to `0700` (an owner other than the user makes the write fail
and be reported); past `MAX_LOG_BYTES` it is rotated to `<name>.1` (one backup), so the log is
bounded too. A log that cannot be written is reported to the logger and never fails the reply.
"""

import asyncio
import contextlib
import logging
import os
import re
import stat
import threading
import time
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Protocol, final

from pydantic import BaseModel, ConfigDict

from tumbleweed.agent.errors import ErrorKind, ProviderError
from tumbleweed.agent.llm import (
    AttemptFailed,
    LlmDone,
    LlmEvent,
    LlmProvider,
    LlmRequest,
    StopReason,
    Usage,
    UsageUpdate,
)
from tumbleweed.core.ids import InvocationId, SessionId, TaskId
from tumbleweed.core.types import ByteCount, ModelName, ProviderRequestId

logger = logging.getLogger(__name__)

MAX_RECORD_BYTES: Final = ByteCount(4096)
MAX_LOG_BYTES: Final = ByteCount(10 * 1024 * 1024)
_PROMPT_VERSION_PATTERN = r"[A-Za-z0-9._-]{1,32}"


@final
class InvocationRecord(BaseModel):
    """One model call, as logged."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    invocation_id: InvocationId
    session_id: SessionId
    task_id: TaskId | None
    model: ModelName
    prompt_version: str
    attempt: int
    """1 for the first try of a call; a call the provider retried has one record per try."""
    started_at: datetime
    latency: timedelta
    outcome: Literal["ok", "error", "cancelled", "timeout", "aborted"]
    """`timeout`: the reply's deadline cut the call short. `aborted`: the consumer stopped reading
    before the call ended (a limit was reached)."""
    error_kind: ErrorKind | None
    stop_reason: StopReason | None
    usage: Usage | None
    request_id: ProviderRequestId | None


class InvocationLog(Protocol):
    def append(self, record: InvocationRecord) -> None: ...


@final
class JsonlInvocationLog:
    """Appends records to a JSONL file, creating it `0600`, and rotating it when it is large."""

    def __init__(self, path: Path, *, max_bytes: int = MAX_LOG_BYTES) -> None:
        self._path = path
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    def append(self, record: InvocationRecord) -> None:
        line = record.model_dump_json().encode("utf-8") + b"\n"
        if len(line) > MAX_RECORD_BYTES:  # not reachable with validated fields: a guard
            raise ValueError("invocation record over the size bound")
        with self._lock:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._path.parent.chmod(0o700)  # only the user's, whatever it was before
            self._rotate_if_large()
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
            descriptor = os.open(self._path, flags, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError("the invocation log is not a regular file")
                os.fchmod(descriptor, 0o600)  # a file that was looser is tightened, not trusted
                os.write(descriptor, line)  # one write: lines do not interleave
            finally:
                os.close(descriptor)

    def _rotate_if_large(self) -> None:
        try:
            size = self._path.lstat().st_size
        except FileNotFoundError:
            return
        if size > self._max_bytes:
            self._path.replace(self._path.with_name(self._path.name + ".1"))


def _error_kind(value: str) -> ErrorKind:
    try:
        return ErrorKind(value)
    except ValueError:
        return ErrorKind.INTERNAL


@final
class RecordedProvider:
    """A provider whose every call is recorded. Made only by `InvocationRecorder.record`."""

    def __init__(
        self,
        recorder: "InvocationRecorder",
        provider: LlmProvider,
        session_id: SessionId,
        task_id: TaskId | None,
    ) -> None:
        self._recorder = recorder
        self._provider = provider
        self._session_id = session_id
        self._task_id = task_id

    def stream(
        self, request: LlmRequest, *, timed_out: Callable[[], bool] = lambda: False
    ) -> AsyncGenerator[LlmEvent]:
        """The events of the provider's stream, and one record when the call is over.

        `timed_out` says whether a cancellation that arrives is the reply's deadline, which is
        recorded as `timeout` instead of `cancelled`. Close the generator (`contextlib.aclosing`)
        when you stop reading early, so the record is written then and the provider's stream is
        released.
        """
        return self._recorder.stream(
            self._provider, request, self._session_id, self._task_id, timed_out
        )


@final
class InvocationRecorder:
    """Wraps providers so that their calls are recorded."""

    def __init__(
        self,
        log: InvocationLog,
        *,
        prompt_version: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not re.fullmatch(_PROMPT_VERSION_PATTERN, prompt_version):
            raise ValueError("prompt_version must be 1-32 of A-Z a-z 0-9 . _ -")
        self._log = log
        self._prompt_version = prompt_version
        self._clock = clock
        self._monotonic = monotonic

    def record(
        self, provider: LlmProvider, *, session_id: SessionId, task_id: TaskId | None = None
    ) -> RecordedProvider:
        return RecordedProvider(self, provider, session_id, task_id)

    async def stream(
        self,
        provider: LlmProvider,
        request: LlmRequest,
        session_id: SessionId,
        task_id: TaskId | None,
        timed_out: Callable[[], bool],
    ) -> AsyncGenerator[LlmEvent]:
        attempt = 1
        invocation_id = InvocationId.new()
        started_at = self._clock()
        started = self._monotonic()
        outcome: Literal["ok", "error", "cancelled", "timeout", "aborted"] = "aborted"
        error_kind: ErrorKind | None = None
        request_id: ProviderRequestId | None = None
        usage: Usage | None = None
        done: LlmDone | None = None
        events = provider.stream(request)

        def record() -> InvocationRecord:
            return InvocationRecord(
                invocation_id=invocation_id,
                session_id=session_id,
                task_id=task_id,
                model=request.model,
                prompt_version=self._prompt_version,
                attempt=attempt,
                started_at=started_at,
                latency=timedelta(seconds=max(0.0, self._monotonic() - started)),
                outcome=outcome,
                error_kind=error_kind,
                stop_reason=done.stop_reason if done else None,
                usage=done.usage if done else usage,
                request_id=done.request_id if done else request_id,
            )

        try:
            async for event in events:
                if isinstance(event, AttemptFailed):
                    # The provider is about to retry: this try gets its record, the next a new one.
                    outcome, error_kind = "error", _error_kind(event.error_kind)
                    request_id, usage = event.request_id, event.usage or usage
                    self._write(record())
                    attempt += 1
                    invocation_id = InvocationId.new()
                    started_at, started = self._clock(), self._monotonic()
                    outcome, error_kind, request_id, usage = "aborted", None, None, None
                    continue
                if isinstance(event, UsageUpdate):
                    usage = event.usage
                elif isinstance(event, LlmDone):
                    done = event
                yield event
            outcome = "ok"
        except asyncio.CancelledError:
            outcome = "timeout" if timed_out() else "cancelled"
            raise
        except ProviderError as exc:
            outcome, error_kind, request_id = "error", exc.kind, exc.request_id
            usage = exc.usage or usage
            raise
        except Exception:
            outcome, error_kind = "error", ErrorKind.INTERNAL
            raise
        finally:
            with contextlib.suppress(Exception):
                aclose = getattr(events, "aclose", None)
                if aclose is not None:
                    await aclose()
            self._write(record())

    def _write(self, record: InvocationRecord) -> None:
        try:
            self._log.append(record)
        except (OSError, ValueError) as exc:
            logger.warning("could not write the invocation log: %s", type(exc).__name__)
