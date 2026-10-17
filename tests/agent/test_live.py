"""One real, minimal call to the real API: the proof that the provider works end to end.

Skipped unless `TUMBLEWEED_LIVE=1` (never in CI, where it is refused). It also needs a key:

    TUMBLEWEED_LIVE=1 ANTHROPIC_API_KEY="$(cat path/to/key)" uv run pytest -m live tests/agent -v

It sends about 20 input tokens and asks for one word. The key is held in an `ApiKey`, which prints
as redacted, so a failing assertion cannot show it.
"""

import asyncio
import os
from pathlib import Path

import pytest

from tumbleweed.agent.anthropic_provider import AnthropicProvider, build_client
from tumbleweed.agent.config import DEFAULT_BASE_URL, DEFAULT_MODEL, AgentConfig, take_api_key
from tumbleweed.agent.limits import AgentLimits
from tumbleweed.agent.loop import AgentLoop
from tumbleweed.agent.prompts import SYSTEM_PROMPT_VERSION, load_system_prompt
from tumbleweed.agent.recorder import InvocationRecorder, JsonlInvocationLog
from tumbleweed.agent.tools import ToolRegistry
from tumbleweed.core.errors import TumbleweedError
from tumbleweed.core.ids import SessionId
from tumbleweed.core.types import TokenCount

pytestmark = pytest.mark.live


def test_a_minimal_real_call_streams_an_answer_and_is_recorded(tmp_path: Path) -> None:
    environ = dict(os.environ)  # a copy: taking the key must not change this process
    try:
        key = take_api_key(environ, AgentConfig())
    except TumbleweedError:
        pytest.skip("no API key: set ANTHROPIC_API_KEY to run the live test")
    log_path = tmp_path / "invocations.jsonl"

    async def scenario() -> tuple[str, list[str]]:
        client = build_client(api_key=key, base_url=DEFAULT_BASE_URL)
        try:
            recorder = InvocationRecorder(
                JsonlInvocationLog(log_path), prompt_version=SYSTEM_PROMPT_VERSION
            )
            loop = AgentLoop(
                provider=recorder.record(
                    AnthropicProvider(client, effort="low"), session_id=SessionId.new()
                ),
                registry=ToolRegistry(),
                model=DEFAULT_MODEL,
                system_prompt=load_system_prompt(),
                limits=AgentLimits(
                    max_turn_output_tokens=TokenCount(1024), reply_timeout_seconds=60
                ),
            )
            deltas: list[str] = []
            async with asyncio.timeout(60):
                reply = await loop.run((), "Reply with exactly one word: ok", deltas.append)
            return reply, deltas
        finally:
            await client.close()

    reply, deltas = asyncio.run(scenario())

    assert reply.strip()
    assert "".join(deltas) == reply
    line = log_path.read_text().splitlines()[-1]
    assert '"outcome":"ok"' in line
    assert '"request_id":"req_' in line  # the provider's ID of the call
    assert key.reveal() not in log_path.read_text()
