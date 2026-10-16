"""`build_agent`: the responder the configuration asks for, and the API key's whole journey (I6).

The secret tests drive a real reply through everything `build_agent` builds (config, key, client,
provider, recorder, responder, chat controller) against a mock of the API that is hostile on
purpose: it echoes the key back in its errors. The key may appear in exactly one place, the
request's `x-api-key` header.
"""

import logging
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import anthropic
import httpx2  # the SDK's HTTP library (a dependency of anthropic)
import pytest

from tests.agent import sse
from tests.agent.support import ManualDispatcher
from tumbleweed.agent import factory
from tumbleweed.agent.config import ApiKey
from tumbleweed.agent.echo import EchoResponder
from tumbleweed.agent.factory import AgentSetup, build_agent
from tumbleweed.agent.runtime import AgentResponder, UnavailableResponder
from tumbleweed.shell.viewmodel.chat import ChatController
from tumbleweed.shell.viewmodel.transcript import MessageState, Transcript

type AgentFactory = Callable[[dict[str, str]], tuple[AgentSetup, ManualDispatcher]]

CANARY = "sk-ant-api03-CANARY0123456789abcdefghijklmnop"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "config" / "tumbleweed").mkdir(parents=True, mode=0o700)
    return tmp_path


def environ_for(home: Path, **extra: str) -> dict[str, str]:
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        **extra,
    }


@pytest.fixture
def agents() -> Iterator[AgentFactory]:
    built: list[AgentSetup] = []

    def build(environ: dict[str, str]) -> tuple[AgentSetup, ManualDispatcher]:
        dispatcher = ManualDispatcher()
        agent = build_agent(environ, dispatcher)
        built.append(agent)
        return agent, dispatcher

    yield build
    for agent in built:
        agent.close()


def chat(agent: AgentSetup, dispatcher: ManualDispatcher, text: str = "hello") -> Transcript:
    transcript = Transcript()
    controller = ChatController(transcript, agent.responder)
    controller.send(text)
    dispatcher.run_until(lambda: not controller.busy)
    return transcript


class TestChoice:
    def test_echo_is_chosen_by_the_config(self, home: Path, agents: AgentFactory) -> None:
        (home / "config" / "tumbleweed" / "config.toml").write_text('[agent]\nresponder = "echo"\n')

        agent, _ = agents(environ_for(home))

        assert isinstance(agent.responder, EchoResponder)

    def test_echo_is_chosen_by_the_environment(self, home: Path, agents: AgentFactory) -> None:
        agent, _ = agents(environ_for(home, TUMBLEWEED_RESPONDER="echo"))

        assert isinstance(agent.responder, EchoResponder)

    def test_a_key_selects_the_model_responder(self, home: Path, agents: AgentFactory) -> None:
        agent, _ = agents(environ_for(home, ANTHROPIC_API_KEY=CANARY))

        assert isinstance(agent.responder, AgentResponder)

    def test_no_key_says_so_in_the_chat_instead_of_failing_to_start(
        self, home: Path, agents: AgentFactory
    ) -> None:
        agent, dispatcher = agents(environ_for(home))

        assert isinstance(agent.responder, UnavailableResponder)
        message = chat(agent, dispatcher).messages[1]
        assert message.state is MessageState.FAILED
        assert message.error is not None
        assert "ANTHROPIC_API_KEY" in message.error

    def test_a_key_file_that_is_too_open_says_so_in_the_chat(
        self, home: Path, agents: AgentFactory
    ) -> None:
        key_file = home / "config" / "tumbleweed" / "api-key"
        key_file.write_text(CANARY)
        key_file.chmod(0o644)

        agent, dispatcher = agents(environ_for(home))

        message = chat(agent, dispatcher).messages[1]
        assert message.error is not None
        assert "mode 0644" in message.error
        assert "must be 0600" in message.error
        assert CANARY not in message.error

    def test_a_broken_config_file_says_so_in_the_chat(
        self, home: Path, agents: AgentFactory
    ) -> None:
        path = home / "config" / "tumbleweed" / "config.toml"
        path.write_text("[agent\n")

        agent, dispatcher = agents(environ_for(home, ANTHROPIC_API_KEY=CANARY))

        message = chat(agent, dispatcher).messages[1]
        assert message.error is not None
        assert "not valid TOML" in message.error

    def test_the_key_leaves_the_environment_whichever_responder_is_chosen(
        self, home: Path, agents: AgentFactory
    ) -> None:
        for choice in ("echo", "anthropic"):
            environ = environ_for(
                home,
                ANTHROPIC_API_KEY=CANARY,
                ANTHROPIC_AUTH_TOKEN="tok-0123456789",  # noqa: S106  (a canary)
                TUMBLEWEED_RESPONDER=choice,
            )

            agents(environ)

            assert "ANTHROPIC_API_KEY" not in environ
            assert "ANTHROPIC_AUTH_TOKEN" not in environ


class TestTheKeyStaysSecret:
    @pytest.fixture
    def server(self, monkeypatch: pytest.MonkeyPatch) -> sse.Server:
        server = sse.Server()

        def build_client(
            *, api_key: ApiKey, base_url: str, http_client: object | None = None
        ) -> anthropic.AsyncAnthropic:
            assert base_url == "https://api.anthropic.com"
            return server.client_with_build(api_key)

        monkeypatch.setattr(factory, "build_client", build_client)
        return server

    def everything_visible(
        self, home: Path, transcript: Transcript, caplog: pytest.LogCaptureFixture
    ) -> str:
        files = "".join(p.read_text(errors="replace") for p in home.rglob("*") if p.is_file())
        texts = "".join(f"{m.text}{m.error}" for m in transcript.messages)
        return files + texts + caplog.text

    def test_it_is_sent_in_the_auth_header_and_nowhere_else(
        self,
        home: Path,
        agents: AgentFactory,
        server: sse.Server,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        server.responses.append(sse.stream_response(sse.message(sse.text_block(0, "Hi there"))))
        environ = environ_for(home, ANTHROPIC_API_KEY=CANARY)
        caplog.set_level(logging.DEBUG)
        for name in ("anthropic", "httpx2", "httpcore2"):
            logging.getLogger(name).setLevel(logging.DEBUG)  # as ANTHROPIC_LOG=debug would

        agent, dispatcher = agents(environ)
        transcript = chat(agent, dispatcher, "my question")

        assert transcript.messages[1].text == "Hi there"
        (request,) = server.requests
        assert request.headers["x-api-key"] == CANARY  # the one place
        assert CANARY not in str(server.bodies)  # not in the model's context
        assert CANARY not in self.everything_visible(home, transcript, caplog)
        assert CANARY not in str(environ)
        log_text = (home / "data" / "tumbleweed" / "shell" / "invocations.jsonl").read_text()
        assert '"outcome":"ok"' in log_text
        assert "request_id" in log_text
        assert "my question" not in log_text
        assert "Hi there" not in log_text

    @pytest.mark.parametrize(
        "failure",
        [
            sse.error_response(401, f"invalid x-api-key: {CANARY}"),
            sse.error_response(400, f"bad request, key {CANARY}"),
            sse.error_response(529, f"overloaded {CANARY}"),
            sse.raises(httpx2.ConnectError(f"cannot connect, key {CANARY}")),
        ],
    )
    def test_a_hostile_error_that_echoes_it_reaches_neither_the_chat_nor_the_logs(
        self,
        home: Path,
        agents: AgentFactory,
        server: sse.Server,
        caplog: pytest.LogCaptureFixture,
        failure: object,
    ) -> None:
        server.responses.append(failure)  # type: ignore[arg-type]  # one scripted response
        caplog.set_level(logging.DEBUG)
        for name in ("anthropic", "httpx2", "httpcore2"):
            logging.getLogger(name).setLevel(logging.DEBUG)  # as ANTHROPIC_LOG=debug would

        agent, dispatcher = agents(environ_for(home, ANTHROPIC_API_KEY=CANARY))
        transcript = chat(agent, dispatcher)

        message = transcript.messages[1]
        assert message.state is MessageState.FAILED
        assert message.error
        assert CANARY not in self.everything_visible(home, transcript, caplog)
        assert '"outcome":"error"' in (home / "data/tumbleweed/shell/invocations.jsonl").read_text()

    def test_a_child_process_of_the_shell_does_not_have_it(
        self,
        home: Path,
        agents: AgentFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import os

        monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))

        agents(os.environ)  # type: ignore[arg-type]  # os.environ is a MutableMapping
        child = subprocess.run(
            [sys.executable, "-c", "import os, sys; sys.stdout.write(repr(dict(os.environ)))"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

        assert CANARY not in child.stdout
        assert "ANTHROPIC_API_KEY" not in child.stdout
