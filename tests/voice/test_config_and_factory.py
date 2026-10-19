"""The `[voice]` configuration and `build_voice`: from a config file to a working push-to-talk,
with the real HTTP backend against a stub server and a fake microphone."""

import logging
import sys
from pathlib import Path

import pytest

from tests.voice.stub_server import Reply, StubServer
from tests.voice.support import ManualDispatcher, Recorder, environ_for, pcm, write_config
from tumbleweed.agent import config as agent_config
from tumbleweed.agent.errors import ConfigError
from tumbleweed.voice import factory
from tumbleweed.voice.audio import AudioSource
from tumbleweed.voice.config import DEFAULT_STT_URL, VoiceConfig
from tumbleweed.voice.factory import build_voice
from tumbleweed.voice.fake import FakeAudioSource
from tumbleweed.voice.input import VoicePhase


def parse(text: str, **env: str) -> VoiceConfig:
    return agent_config.parse_voice_config(text, env, source="test")


class TestTheTable:
    def test_the_defaults(self) -> None:
        config = parse("")

        assert config.enabled is True
        assert config.stt_url == DEFAULT_STT_URL == "http://127.0.0.1:8080"
        assert config.language == "auto"
        assert config.edit_before_send is False
        assert config.limits.max_utterance_seconds == 30

    def test_every_setting_can_be_set(self) -> None:
        config = parse(
            """
            [voice]
            enabled = false
            stt_url = "http://10.0.2.2:9000/stt"
            language = "de"
            edit_before_send = true
            [voice.limits]
            max_utterance_seconds = 10
            request_timeout_seconds = 5
            max_response_bytes = 2048
            """
        )

        assert (config.enabled, config.language, config.edit_before_send) == (False, "de", True)
        assert config.endpoint.inference_url == "http://10.0.2.2:9000/stt/inference"
        assert config.limits.max_utterance_seconds == 10
        assert config.limits.request_timeout_seconds == 5
        assert config.limits.max_response_bytes == 2048

    def test_the_environment_overrides_the_url(self) -> None:
        config = parse(
            '[voice]\nstt_url = "http://file.example"\n', TUMBLEWEED_STT_URL="http://env.example:1"
        )

        assert config.stt_url == "http://env.example:1"

    def test_an_empty_override_is_ignored(self) -> None:
        assert parse("", TUMBLEWEED_STT_URL="").stt_url == DEFAULT_STT_URL

    @pytest.mark.parametrize(
        "text",
        [
            '[voice]\nstt_url = "ftp://x.example"\n',
            '[voice]\nstt_url = "http://user:pw@x.example"\n',
            '[voice]\nstt_url = "http://x.example/?a=b"\n',
            "[voice]\nstt_url = 5\n",
            '[voice]\nlanguage = "English"\n',
            '[voice]\nenabled = "yes"\n',
            "[voice]\nsurprise = 1\n",
            "[voice.limits]\nmax_utterance_seconds = 0\n",
            "[voice.limits]\nmax_utterance_seconds = 9999\n",
            "[voice.limits]\nrequest_timeout_seconds = 0\n",
            "[voice.limits]\nmax_response_bytes = 10\n",
            "voice = 3\n",
        ],
    )
    def test_a_bad_table_is_refused(self, text: str) -> None:
        with pytest.raises(ConfigError):
            parse(text)

    def test_a_bad_override_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="stt_url"):
            parse("", TUMBLEWEED_STT_URL="file:///etc/passwd")

    def test_the_override_is_refused_if_voice_is_not_a_table(self) -> None:
        with pytest.raises(ConfigError, match=r"\[voice\] must be a table"):
            parse("voice = 3\n", TUMBLEWEED_STT_URL="http://x.example")

    def test_a_refused_url_is_never_repeated_in_the_error(self) -> None:
        for text, env in (
            ('[voice]\nstt_url = "http://user:hunter2@x.example"\n', {}),
            ("", {"TUMBLEWEED_STT_URL": "http://user:hunter2@x.example"}),
        ):
            with pytest.raises(ConfigError) as raised:
                parse(text, **env)

            assert "hunter2" not in str(raised.value)

    def test_a_mistake_in_the_voice_table_does_not_disable_the_agent(self, home: Path) -> None:
        text = '[agent]\nresponder = "echo"\n[voice]\nstt_url = "ftp://nope.example"\n'
        write_config(home, text)
        environ = environ_for(home)

        assert agent_config.load_config(environ).agent.responder.value == "echo"
        with pytest.raises(ConfigError, match="voice"):
            agent_config.load_voice_config(environ)

    def test_a_mistake_in_the_agent_table_does_not_disable_voice(self, home: Path) -> None:
        write_config(home, '[agent]\nresponder = "nonsense"\n[voice]\nlanguage = "de"\n')
        environ = environ_for(home)

        with pytest.raises(ConfigError):
            agent_config.load_config(environ)
        assert agent_config.load_voice_config(environ).language == "de"

    def test_a_syntax_error_in_the_file_is_both_their_problem(self, home: Path) -> None:
        write_config(home, "this is not toml\n")

        with pytest.raises(ConfigError):
            agent_config.load_config(environ_for(home))
        with pytest.raises(ConfigError):
            agent_config.load_voice_config(environ_for(home))


class TestBuildVoice:
    def test_a_config_to_a_transcript_through_the_real_http_backend(
        self, home: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_config(
            home,
            '[voice]\nlanguage = "en"\nedit_before_send = true\n'
            "[voice.limits]\nmax_utterance_seconds = 5\n",
        )
        microphone: list[FakeAudioSource] = []

        def fake_source(config: VoiceConfig) -> AudioSource:
            microphone.append(FakeAudioSource(config.limits.max_utterance_bytes))
            return microphone[-1]

        monkeypatch.setattr(factory, "_gstreamer_source", fake_source)
        dispatcher = ManualDispatcher()
        setup = build_voice(environ_for(home, TUMBLEWEED_STT_URL=stub.url), dispatcher)
        assert setup is not None
        recorder = Recorder()
        setup.input.subscribe(recorder)
        try:
            setup.input.press()
            assert dispatcher.pump_until(lambda: bool(microphone) and microphone[0].capturing)
            microphone[0].feed(pcm(1.0))
            setup.input.release()
            assert dispatcher.pump_until(lambda: setup.input.phase is VoicePhase.IDLE)

            assert setup.edit_before_send is True
            assert recorder.texts == ["hello world"]
            assert stub.requests[0].parts()["language"] == b"en"
        finally:
            setup.input.close()

    @pytest.mark.parametrize(
        "config",
        [
            "[voice]\nenabled = false\n",
            '[voice]\nstt_url = "ftp://nope.example"\n',
            "this is not toml\n",
        ],
    )
    def test_when_there_is_nothing_to_give_there_is_none(
        self, home: Path, config: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        write_config(home, config)

        with caplog.at_level(logging.INFO):
            assert build_voice(environ_for(home), ManualDispatcher()) is None

    @pytest.mark.parametrize(
        ("url", "configured", "waits"),
        [
            ("http://10.0.0.5:8080", False, True),  # plain http to another host: always
            ("http://stt.example.com", False, True),
            ("http://10.0.0.5:8080", True, True),
            ("http://127.0.0.1:8080", False, False),
            ("http://localhost:8080", False, False),
            ("http://[::1]:8080", False, False),
            ("https://stt.example.com", False, False),
            ("http://127.0.0.1:8080", True, True),
        ],
    )
    def test_over_plain_http_to_another_host_a_transcript_always_waits_to_be_read(
        self,
        home: Path,
        url: str,
        configured: bool,
        waits: bool,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        write_config(home, f"[voice]\nedit_before_send = {str(configured).lower()}\n")

        with caplog.at_level(logging.WARNING):
            setup = build_voice(environ_for(home, TUMBLEWEED_STT_URL=url), ManualDispatcher())
        try:
            assert setup is not None
            assert setup.edit_before_send is waits
            if waits and not configured:
                assert "not sent automatically" in caplog.text
                assert url.split("//")[1].split("/")[0] in caplog.text
        finally:
            if setup is not None:
                setup.input.close()

    def test_the_default_is_voice_on(self, home: Path) -> None:
        setup = build_voice(environ_for(home), ManualDispatcher())
        try:
            assert setup is not None
            assert setup.edit_before_send is False
        finally:
            if setup is not None:
                setup.input.close()

    def test_a_logged_refusal_does_not_repeat_the_url(
        self, home: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        write_config(home, '[voice]\nstt_url = "http://user:hunter2@x.example"\n')

        with caplog.at_level(logging.DEBUG):
            build_voice(environ_for(home), ManualDispatcher())

        assert "hunter2" not in caplog.text

    def test_a_host_without_gstreamer_says_so_when_the_user_first_talks(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "tumbleweed.voice.gstreamer", None)  # import fails
        dispatcher = ManualDispatcher()
        setup = build_voice(environ_for(home), dispatcher)
        assert setup is not None
        recorder = Recorder()
        setup.input.subscribe(recorder)
        try:
            setup.input.press()
            assert dispatcher.pump_until(lambda: setup.input.phase is VoicePhase.IDLE)

            assert recorder.problems == [
                "Voice input needs GStreamer, which is not installed (README, section Voice)."
            ]
        finally:
            setup.input.close()

    def test_the_microphone_is_not_touched_until_the_first_press(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def never(config: VoiceConfig) -> AudioSource:
            raise AssertionError("the microphone was opened at start-up")

        monkeypatch.setattr(factory, "_gstreamer_source", never)

        setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        setup.input.close()

    def test_a_server_that_says_no_is_a_problem_in_the_ui_not_a_crash(
        self, home: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub.reply = Reply(status=500, body=b"SECRET-server-words")
        microphone = FakeAudioSource(VoiceConfig().limits.max_utterance_bytes)
        monkeypatch.setattr(factory, "_gstreamer_source", lambda config: microphone)
        dispatcher = ManualDispatcher()
        setup = build_voice(environ_for(home, TUMBLEWEED_STT_URL=stub.url), dispatcher)
        assert setup is not None
        recorder = Recorder()
        setup.input.subscribe(recorder)
        try:
            setup.input.press()
            assert dispatcher.pump_until(lambda: microphone.capturing)
            microphone.feed(pcm(1.0))
            setup.input.release()
            assert dispatcher.pump_until(lambda: setup.input.phase is VoicePhase.IDLE)

            assert recorder.problems == ["The speech server answered with status 500."]
        finally:
            setup.input.close()
