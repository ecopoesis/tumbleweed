"""`[voice.tts]` and what `build_voice` makes of it (S07): speech is built only when Piper and a
voice model are named and check out, and a mistake turns speech off with a fixed message and leaves
push-to-talk working."""

from pathlib import Path

import pytest

from tests.voice.fake_piper import make_fake_piper
from tests.voice.support import ManualDispatcher, environ_for, write_config
from tumbleweed.agent import config as agent_config
from tumbleweed.agent.errors import ConfigError
from tumbleweed.voice.config import TtsConfig, VoiceConfig
from tumbleweed.voice.factory import build_voice
from tumbleweed.voice.output import SpeechPhase


def parse(text: str) -> VoiceConfig:
    return agent_config.parse_voice_config(text, {}, source="test")


class TestTheTable:
    def test_the_defaults_say_nothing_is_set_up(self) -> None:
        tts = parse("").tts

        assert tts == TtsConfig()
        assert tts.enabled is True
        assert tts.piper_path is None
        assert tts.voice_model is None
        assert tts.configured is False
        assert (tts.sample_rate_hz, tts.max_sentence_chars, tts.max_reply_chars) == (
            22_050,
            400,
            4000,
        )
        assert (tts.synthesis_timeout_seconds, tts.max_audio_seconds) == (20, 60)

    def test_every_setting_can_be_set(self) -> None:
        tts = parse(
            """
            [voice.tts]
            enabled = false
            piper_path = "/opt/piper/piper"
            voice_model = "/opt/piper/voice.onnx"
            sample_rate_hz = 16000
            max_sentence_chars = 200
            max_reply_chars = 1000
            synthesis_timeout_seconds = 5
            max_audio_seconds = 10
            """
        ).tts

        assert tts.piper_path == "/opt/piper/piper"
        assert tts.voice_model == "/opt/piper/voice.onnx"
        assert tts.sample_rate_hz == 16_000
        assert tts.max_audio_bytes == 16_000 * 2 * 10
        assert tts.configured is False  # switched off

    def test_both_paths_make_it_configured(self) -> None:
        tts = parse('[voice.tts]\npiper_path = "/a/piper"\nvoice_model = "/a/v.onnx"\n').tts

        assert tts.configured is True

    @pytest.mark.parametrize(
        "text",
        [
            '[voice.tts]\npiper_path = "piper"\n',
            '[voice.tts]\nvoice_model = "voice.onnx"\n',
            '[voice.tts]\nvoice_model = "~/voice.onnx"\n',
            '[voice.tts]\nvoice_model = "/a/b\\u0000c"\n',
            '[voice.tts]\nvoice_model = "/a/b\\nc"\n',
            f'[voice.tts]\nvoice_model = "/{"a" * 5000}"\n',
            "[voice.tts]\npiper_path = 3\n",
            '[voice.tts]\nenabled = "yes"\n',
            "[voice.tts]\nsurprise = 1\n",
            '[voice.tts]\nvoice_modle = "/a/v.onnx"\n',
            "[voice.tts]\nsample_rate_hz = 100\n",
            "[voice.tts]\nsample_rate_hz = 96000\n",
            "[voice.tts]\nmax_sentence_chars = 5\n",
            "[voice.tts]\nmax_sentence_chars = 100000\n",
            "[voice.tts]\nmax_reply_chars = 99\n",
            "[voice.tts]\nmax_reply_chars = 10000000\n",
            "[voice.tts]\nsynthesis_timeout_seconds = 0\n",
            "[voice.tts]\nsynthesis_timeout_seconds = 3600\n",
            "[voice.tts]\nmax_audio_seconds = 0\n",
            "[voice.tts]\nmax_audio_seconds = 100000\n",
            "[voice]\ntts = 3\n",
            '[voice]\ntts = "piper"\n',
        ],
    )
    def test_a_bad_tts_table_turns_speech_off_and_nothing_else(self, text: str) -> None:
        config = parse(text)  # does not raise: the rest of [voice] is fine

        assert config.tts_problem == "invalid"
        assert config.tts.configured is False
        assert config.enabled is True

    def test_a_bad_tts_table_leaves_the_rest_of_voice_as_written(self) -> None:
        config = parse('[voice]\nlanguage = "de"\n[voice.tts]\nsample_rate_hz = 1\n')

        assert config.language == "de"
        assert config.tts_problem is not None

    def test_the_problem_is_not_a_setting(self) -> None:
        with pytest.raises(ConfigError):
            parse('[voice]\ntts_problem = "hello"\n')

    def test_a_good_table_has_no_problem(self) -> None:
        assert parse('[voice.tts]\npiper_path = "/a/p"\n').tts_problem is None

    def test_the_voice_table_itself_is_still_refused_when_it_is_bad(self) -> None:
        with pytest.raises(ConfigError):
            parse('[voice]\nlanguage = "English"\n[voice.tts]\nsample_rate_hz = 1\n')

    def test_the_longest_sentence_always_fits_the_stdin_bound(self) -> None:
        from tumbleweed.voice.piper import MAX_STDIN_BYTES

        longest = parse("[voice.tts]\nmax_sentence_chars = 1000\n").tts.max_sentence_chars

        assert longest * 4 + 1 <= MAX_STDIN_BYTES  # four bytes a character at most, and a newline


class TestBuildVoice:
    def configure(self, home: Path, tmp_path: Path, extra: str = "") -> Path:
        program, model = make_fake_piper(tmp_path)
        write_config(
            home,
            f'[voice.tts]\npiper_path = "{program}"\nvoice_model = "{model}"\n{extra}',
        )
        return model

    def test_nothing_is_configured_so_there_is_no_speech_and_no_message(
        self,
        home: Path,
    ) -> None:
        setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.speech is None
        assert setup.notice is None

    def test_a_good_configuration_gives_speech_that_starts_idle(
        self,
        home: Path,
        tmp_path: Path,
    ) -> None:
        self.configure(home, tmp_path)

        setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.notice is None
        assert setup.speech is not None
        assert setup.speech.phase is SpeechPhase.IDLE
        assert setup.speech.muted is False
        setup.speech.close()
        setup.input.close()

    def test_switched_off_there_is_no_speech(self, home: Path, tmp_path: Path) -> None:
        self.configure(home, tmp_path, "enabled = false\n")

        setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.speech is None
        assert setup.notice is None

    def test_half_a_configuration_says_what_is_missing(self, home: Path, tmp_path: Path) -> None:
        program, _model = make_fake_piper(tmp_path)
        write_config(home, f'[voice.tts]\npiper_path = "{program}"\n')

        setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.speech is None
        assert setup.notice == (
            "Speech output is off: [voice.tts] needs both piper_path and voice_model."
        )
        assert setup.input is not None  # push-to-talk still works

    @pytest.mark.parametrize(
        ("damage", "message"),
        [
            ("missing_model", "The voice model was not found."),
            ("model_is_symlink", "The voice model path must have no symlink"),
            ("missing_program", "The Piper program was not found."),
            ("program_not_executable", "The Piper program is not an executable file."),
        ],
    )
    def test_a_bad_path_turns_speech_off_with_a_fixed_message_and_leaves_input_working(
        self,
        home: Path,
        tmp_path: Path,
        damage: str,
        message: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        model = self.configure(home, tmp_path)
        if damage == "missing_model":
            model.unlink()
        elif damage == "model_is_symlink":
            real = tmp_path / "real.onnx"
            model.rename(real)
            model.symlink_to(real)
        elif damage == "missing_program":
            (tmp_path / "piper").unlink()
        else:
            (tmp_path / "piper").chmod(0o600)

        with caplog.at_level("WARNING"):
            setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.input is not None
        assert setup.speech is None
        assert setup.notice is not None
        assert setup.notice.startswith("Speech output is off: ")
        assert message in setup.notice
        assert str(tmp_path) not in setup.notice  # a path is not echoed into the UI
        setup.input.close()

    @pytest.mark.parametrize(
        "table",
        [
            'voice_model = "relative.onnx"',
            'voice_model = "~/voice.onnx"',
            'surprise = "x"',
            "sample_rate_hz = 1",
            "max_reply_chars = 99999999",
            'piper_path = "/a/p"\nvoice_modle = "/a/v"',
        ],
    )
    def test_a_mistake_in_the_tts_table_turns_speech_off_and_keeps_the_microphone(
        self, home: Path, table: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        write_config(home, f"[voice.tts]\n{table}\n")

        with caplog.at_level("WARNING"):
            setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None  # the Mic, the key and the speech server are all still there
        assert setup.input is not None
        assert setup.speech is None
        assert setup.notice == "Speech output is off: [voice.tts] has an invalid setting."
        assert "invalid [voice.tts]" in caplog.text
        setup.input.close()

    def test_a_mistake_in_voice_itself_still_turns_voice_off(self, home: Path) -> None:
        write_config(home, '[voice]\nlanguage = "English"\n')

        assert build_voice(environ_for(home), ManualDispatcher()) is None
