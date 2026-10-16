"""The invocation chokepoint (DECISIONS D3): every model call goes through `InvocationRecorder`.

The agent loop does not hold a provider, it holds the recorder, and asks it to stream a request
through a provider. The recorder passes the events on untouched and, when the call ends, however
it ends, appends one JSONL record: `InvocationId`, `SessionId`, `TaskId | None`, the model, token
usage, latency, the stop reason, the provider's request ID, how it ended and, for a failure, the
`ErrorKind`.

What a record never holds (I6): the API key, any prompt, message or tool text, a provider's error
text. It is built only from validated, closed or length-capped fields, so there is nothing to
scrub, and `MAX_RECORD_BYTES` bounds a line. The file is `0600` in a `0700` directory; past
`MAX_LOG_BYTES` it is rotated to `<name>.1` (one backup), so the log is bounded too. A log that
cannot be written is reported to the logger and never fails the reply.
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
from tumbleweed.agent.llm import LlmDone, LlmEvent, LlmProvider, LlmRequest, StopReason, Usage
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
    started_at: datetime
    latency: timedelta
    outcome: Literal["ok", "error", "cancelled", "aborted"]
    """`aborted`: the consumer stopped reading before the call ended (a limit was reached)."""
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
            self._rotate_if_large()
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
            descriptor = os.open(self._path, flags, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise OSError("the invocation log is not a regular file")
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


@final
class InvocationRecorder:
    """Streams requests through providers and records each call."""

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

    async def stream(
        self,
        provider: LlmProvider,
        request: LlmRequest,
        *,
        session_id: SessionId,
        task_id: TaskId | None,
    ) -> AsyncGenerator[LlmEvent]:
        """The events of `provider.stream(request)`, and one record when the call is over.

        Close it (`contextlib.aclosing`) when you stop reading early, so the record is written
        then and the provider's stream is released.
        """
        invocation_id = InvocationId.new()
        started_at = self._clock()
        started = self._monotonic()
        outcome: Literal["ok", "error", "cancelled", "aborted"] = "aborted"
        error_kind: ErrorKind | None = None
        request_id: ProviderRequestId | None = None
        done: LlmDone | None = None
        events = provider.stream(request)
        try:
            async for event in events:
                if isinstance(event, LlmDone):
                    done = event
                yield event
            outcome = "ok"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except ProviderError as exc:
            outcome, error_kind, request_id = "error", exc.kind, exc.request_id
            raise
        except Exception:
            outcome, error_kind = "error", ErrorKind.INTERNAL
            raise
        finally:
            with contextlib.suppress(Exception):
                aclose = getattr(events, "aclose", None)
                if aclose is not None:
                    await aclose()
            self._write(
                InvocationRecord(
                    invocation_id=invocation_id,
                    session_id=session_id,
                    task_id=task_id,
                    model=request.model,
                    prompt_version=self._prompt_version,
                    started_at=started_at,
                    latency=timedelta(seconds=max(0.0, self._monotonic() - started)),
                    outcome=outcome,
                    error_kind=error_kind,
                    stop_reason=done.stop_reason if done else None,
                    usage=done.usage if done else None,
                    request_id=done.request_id if done else request_id,
                )
            )

    def _write(self, record: InvocationRecord) -> None:
        try:
            self._log.append(record)
        except (OSError, ValueError) as exc:
            logger.warning("could not write the invocation log: %s", type(exc).__name__)
