"""The invocation chokepoint: one JSONL record per call, however it ends, and no secrets in it."""

import asyncio
import contextlib
import json
import logging
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.agent.support import MODEL, MemoryLog, RaisingProvider
from tumbleweed.agent.errors import ErrorKind, ProviderRateLimitedError
from tumbleweed.agent.fake import FakeProvider, FakeToolCall, FakeTurn
from tumbleweed.agent.llm import (
    LlmDone,
    LlmEvent,
    LlmMessage,
    LlmProvider,
    LlmRequest,
    StopReason,
    TextBlock,
    TextDelta,
)
from tumbleweed.agent.recorder import (
    MAX_RECORD_BYTES,
    InvocationRecord,
    InvocationRecorder,
    JsonlInvocationLog,
)
from tumbleweed.core.ids import InvocationId, SessionId, TaskId
from tumbleweed.core.types import ProviderRequestId, TokenCount

PROMPT = "PRIVATE-PROMPT-TEXT"
SESSION = SessionId.new()


def request(text: str = PROMPT) -> LlmRequest:
    return LlmRequest(
        model=MODEL,
        system="PRIVATE-SYSTEM-PROMPT",
        messages=(LlmMessage(role="user", content=(TextBlock(text=text),)),),
        max_tokens=TokenCount(100),
    )


async def consume(
    recorder: InvocationRecorder, provider: LlmProvider, task: TaskId | None = None
) -> list[LlmEvent]:
    return [
        event
        async for event in recorder.stream(provider, request(), session_id=SESSION, task_id=task)
    ]


class TestRecords:
    def test_a_finished_call_is_recorded_with_everything_the_plan_lists(self) -> None:
        log = MemoryLog()
        ticks = iter([10.0, 10.25])
        recorder = InvocationRecorder(
            log,
            prompt_version="v1",
            clock=lambda: datetime(2026, 10, 16, tzinfo=UTC),
            monotonic=lambda: next(ticks),
        )
        task = TaskId.new()

        events = asyncio.run(consume(recorder, FakeProvider([FakeTurn(chunks=("hi",))]), task))

        assert [e.kind for e in events] == ["text_delta", "done"]  # passed on untouched
        (record,) = log.records
        assert isinstance(record.invocation_id, InvocationId)
        assert (record.session_id, record.task_id) == (SESSION, task)
        assert (str(record.model), record.prompt_version) == ("claude-test-1", "v1")
        assert record.latency.total_seconds() == 0.25
        assert (record.outcome, record.error_kind, record.stop_reason) == (
            "ok",
            None,
            StopReason.END_TURN,
        )
        assert record.usage is not None
        assert record.usage.output_tokens == 10
        assert record.request_id == ProviderRequestId("req_fake_1")

    def test_the_task_may_be_absent(self) -> None:
        log = MemoryLog()
        asyncio.run(
            consume(InvocationRecorder(log, prompt_version="v1"), FakeProvider([FakeTurn()]))
        )

        assert log.records[0].task_id is None

    def test_each_call_of_a_tool_loop_is_its_own_invocation(self) -> None:
        log = MemoryLog()
        recorder = InvocationRecorder(log, prompt_version="v1")
        provider = FakeProvider([FakeTurn(tool_calls=(FakeToolCall("t", {}),)), FakeTurn()])

        asyncio.run(consume(recorder, provider))
        asyncio.run(consume(recorder, provider))

        assert len({r.invocation_id for r in log.records}) == 2
        assert [r.stop_reason for r in log.records] == [StopReason.TOOL_USE, StopReason.END_TURN]

    def test_a_provider_error_is_recorded_with_its_kind_and_request_id_and_reraised(self) -> None:
        log = MemoryLog()
        rid = ProviderRequestId("req_failed")
        provider = FakeProvider(
            [FakeTurn(error=ProviderRateLimitedError("slow down", request_id=rid))]
        )

        with pytest.raises(ProviderRateLimitedError):
            asyncio.run(consume(InvocationRecorder(log, prompt_version="v1"), provider))

        (record,) = log.records
        assert (record.outcome, record.error_kind) == ("error", ErrorKind.RATE_LIMITED)
        assert record.request_id == rid
        assert record.usage is None

    def test_an_unexpected_exception_is_recorded_as_internal_without_its_text(self) -> None:
        log = MemoryLog()
        buggy = RaisingProvider(RuntimeError("sk-ant-api03-leaky"))
        with pytest.raises(RuntimeError):
            asyncio.run(consume(InvocationRecorder(log, prompt_version="v1"), buggy))

        assert (log.records[0].outcome, log.records[0].error_kind) == ("error", ErrorKind.INTERNAL)
        assert "leaky" not in log.records[0].model_dump_json()

    def test_a_consumer_that_stops_early_leaves_an_aborted_record(self) -> None:
        log = MemoryLog()
        recorder = InvocationRecorder(log, prompt_version="v1")
        provider = FakeProvider([FakeTurn(chunks=("a", "b", "c"))])

        async def scenario() -> None:
            events = recorder.stream(provider, request(), session_id=SESSION, task_id=None)
            async with contextlib.aclosing(events):
                async for event in events:
                    if isinstance(event, TextDelta):
                        break

        asyncio.run(scenario())

        assert [r.outcome for r in log.records] == ["aborted"]
        assert log.records[0].stop_reason is None

    def test_a_cancelled_call_is_recorded_as_cancelled(self) -> None:
        log = MemoryLog()
        recorder = InvocationRecorder(log, prompt_version="v1")
        provider = FakeProvider([FakeTurn(chunks=("a",), hang=True)])

        async def scenario() -> None:
            task = asyncio.ensure_future(consume(recorder, provider))
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

        assert [r.outcome for r in log.records] == ["cancelled"]

    def test_a_prompt_version_that_could_not_be_logged_safely_is_refused(self) -> None:
        with pytest.raises(ValueError, match="prompt_version"):
            InvocationRecorder(MemoryLog(), prompt_version="v1\nINJECTED")


class TestTheFile:
    def log_path(self, tmp_path: Path) -> Path:
        return tmp_path / "data" / "tumbleweed" / "shell" / "invocations.jsonl"

    def test_lines_are_json_with_the_fields_and_hold_no_prompt_text(self, tmp_path: Path) -> None:
        path = self.log_path(tmp_path)
        recorder = InvocationRecorder(JsonlInvocationLog(path), prompt_version="v1")
        asyncio.run(consume(recorder, FakeProvider([FakeTurn(chunks=(PROMPT + "-ANSWER",))])))

        raw = path.read_text()
        (line,) = raw.splitlines()
        data = json.loads(line)
        assert set(data) == {
            "invocation_id",
            "session_id",
            "task_id",
            "model",
            "prompt_version",
            "started_at",
            "latency",
            "outcome",
            "error_kind",
            "stop_reason",
            "usage",
            "request_id",
        }
        assert data["invocation_id"].startswith("inv_")
        assert data["session_id"].startswith("ses_")
        assert data["usage"]["output_tokens"] == 10
        assert "PRIVATE" not in raw  # neither the prompt, the system prompt nor the answer

    def test_the_file_is_private_and_so_is_its_directory(self, tmp_path: Path) -> None:
        path = self.log_path(tmp_path)
        recorder = InvocationRecorder(JsonlInvocationLog(path), prompt_version="v1")

        asyncio.run(consume(recorder, FakeProvider([FakeTurn()])))

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    def test_records_append(self, tmp_path: Path) -> None:
        path = self.log_path(tmp_path)
        recorder = InvocationRecorder(JsonlInvocationLog(path), prompt_version="v1")

        asyncio.run(consume(recorder, FakeProvider([FakeTurn(), FakeTurn()])))
        asyncio.run(consume(recorder, FakeProvider([FakeTurn()])))

        assert len(path.read_text().splitlines()) == 2

    def test_a_record_stays_far_under_the_size_bound(self, tmp_path: Path) -> None:
        path = self.log_path(tmp_path)
        recorder = InvocationRecorder(JsonlInvocationLog(path), prompt_version="v" * 32)
        asyncio.run(consume(recorder, FakeProvider([FakeTurn()]), TaskId.new()))

        assert len(path.read_bytes()) < MAX_RECORD_BYTES / 2

    def test_the_log_is_rotated_when_it_is_large(self, tmp_path: Path) -> None:
        path = self.log_path(tmp_path)
        log = JsonlInvocationLog(path, max_bytes=500)
        recorder = InvocationRecorder(log, prompt_version="v1")

        for _ in range(6):
            asyncio.run(consume(recorder, FakeProvider([FakeTurn()])))

        assert path.with_name(path.name + ".1").exists()
        assert path.stat().st_size <= 500 + MAX_RECORD_BYTES
        assert sum(len(p.read_text().splitlines()) for p in path.parent.iterdir()) <= 6

    def test_a_log_that_cannot_be_written_is_reported_and_does_not_fail_the_call(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = self.log_path(tmp_path)
        path.mkdir(parents=True)  # a directory where the file should be
        recorder = InvocationRecorder(JsonlInvocationLog(path), prompt_version="v1")

        with caplog.at_level(logging.WARNING):
            events = asyncio.run(consume(recorder, FakeProvider([FakeTurn(chunks=("fine",))])))

        assert isinstance(events[-1], LlmDone)  # the reply is unaffected
        assert "could not write the invocation log" in caplog.text

    def test_a_symlink_where_the_file_should_be_is_not_followed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = self.log_path(tmp_path)
        path.parent.mkdir(parents=True)
        victim = tmp_path / "victim"
        victim.write_text("keep")
        path.symlink_to(victim)
        recorder = InvocationRecorder(JsonlInvocationLog(path), prompt_version="v1")

        with caplog.at_level(logging.WARNING):
            asyncio.run(consume(recorder, FakeProvider([FakeTurn()])))

        assert victim.read_text() == "keep"
        assert "could not write the invocation log" in caplog.text


def test_a_record_cannot_hold_free_text() -> None:
    fields = set(InvocationRecord.model_fields)

    assert not {"prompt", "messages", "content", "text", "error", "message", "api_key"} & fields
