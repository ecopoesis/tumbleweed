"""`[voice.vad]` and `[voice.wake]`, and what `build_voice` makes of them (S08): hands-free mode and
the trimming of push-to-talk recordings are built only when a models directory is named and its
files have their pinned hashes, and a mistake in either table turns *those* off, with one fixed
message, and leaves push-to-talk, speech and the speech server working.

The pinned files are real models, so these tests pin tiny stand-ins in their place
(`monkeypatch`), made in the test (`tiny_onnx`).
"""

import hashlib
import sys
from pathlib import Path

import pytest

from tests.voice.fake_piper import make_fake_piper
from tests.voice.support import ManualDispatcher, environ_for, write_config
from tests.voice.tiny_onnx import tiny_identity_model
from tumbleweed.agent import config as agent_config
from tumbleweed.agent.errors import ConfigError
from tumbleweed.core.types import ContentHash
from tumbleweed.voice import factory
from tumbleweed.voice.config import VadConfig, VoiceConfig, WakeConfig
from tumbleweed.voice.errors import AudioError
from tumbleweed.voice.factory import build_voice
from tumbleweed.voice.hands_free import HandsFreePhase
from tumbleweed.voice.models import ModelFile
from tumbleweed.voice.vad import VadTrimmer


def parse(text: str) -> VoiceConfig:
    return agent_config.parse_voice_config(text, {}, source="test")


class TestTheTables:
    def test_the_defaults_say_nothing_is_set_up(self) -> None:
        config = parse("")

        assert config.vad == VadConfig()
        assert config.wake == WakeConfig()
        assert config.hands_free_problem is None
        assert (config.vad.threshold, config.vad.end_silence_ms) == (0.5, 800)
        assert (config.vad.min_speech_ms, config.vad.wait_for_speech_seconds) == (128, 6)
        assert (config.wake.model, config.wake.threshold) == ("hey_jarvis", 0.5)
        assert (config.wake.speaking_threshold, config.wake.speaking_holdoff_ms) == (0.9, 500)
        assert config.wake.enabled is True

    def test_every_setting_can_be_set(self) -> None:
        config = parse(
            """
            [voice.vad]
            models_dir = "/opt/models"
            threshold = 0.6
            end_silence_ms = 500
            min_speech_ms = 96
            wait_for_speech_seconds = 3

            [voice.wake]
            enabled = false
            models_dir = "/opt/wake"
            model = "alexa"
            threshold = 0.7
            speaking_threshold = 0.95
            speaking_holdoff_ms = 100
            """
        )

        assert config.vad == VadConfig(
            models_dir="/opt/models",
            threshold=0.6,
            end_silence_ms=500,
            min_speech_ms=96,
            wait_for_speech_seconds=3,
        )
        assert config.wake == WakeConfig(
            enabled=False,
            models_dir="/opt/wake",
            model="alexa",
            threshold=0.7,
            speaking_threshold=0.95,
            speaking_holdoff_ms=100,
        )
        assert config.hands_free_problem is None

    def test_an_integer_is_a_fine_speaking_threshold(
        self,
    ) -> None:
        wake = parse("[voice.wake]\nspeaking_threshold = 1\n").wake

        assert wake.speaking_threshold == 1

    @pytest.mark.parametrize(
        "text",
        [
            '[voice.vad]\nmodels_dir = "models"\n',
            '[voice.vad]\nmodels_dir = "~/models"\n',
            '[voice.vad]\nmodels_dir = "/a\\u0000b"\n',
            f'[voice.vad]\nmodels_dir = "/{"a" * 5000}"\n',
            "[voice.vad]\nthreshold = 0.05\n",
            "[voice.vad]\nthreshold = 0.99\n",
            "[voice.vad]\nend_silence_ms = 10\n",
            "[voice.vad]\nend_silence_ms = 99999\n",
            "[voice.vad]\nmin_speech_ms = 1\n",
            "[voice.vad]\nwait_for_speech_seconds = 0\n",
            "[voice.vad]\nwait_for_speech_seconds = 3600\n",
            '[voice.vad]\nthreshold = "high"\n',
            "[voice.vad]\nsurprise = 1\n",
            '[voice.wake]\nmodels_dir = "relative"\n',
            '[voice.wake]\nmodel = "computer"\n',
            '[voice.wake]\nmodel = "../../etc/passwd"\n',
            "[voice.wake]\nmodel = 3\n",
            "[voice.wake]\nthreshold = 0.0\n",
            "[voice.wake]\nthreshold = 1.5\n",
            "[voice.wake]\nthreshold = 0.8\nspeaking_threshold = 0.5\n",
            "[voice.wake]\nspeaking_holdoff_ms = -1\n",
            "[voice.wake]\nspeaking_holdoff_ms = 100000\n",
            '[voice.wake]\nenabled = "yes"\n',
            "[voice.wake]\nsurprise = 1\n",
            "[voice]\nvad = 3\n",
            '[voice]\nwake = "jarvis"\n',
        ],
    )
    def test_a_bad_table_turns_hands_free_off_and_nothing_else(self, text: str) -> None:
        config = parse(text)  # does not raise: the rest of [voice] is fine

        assert config.hands_free_problem == "invalid"
        assert config.wake.enabled is False
        assert config.vad.models_dir is None
        assert config.enabled is True
        assert config.tts_problem is None

    def test_the_two_tables_are_judged_together(self) -> None:
        config = parse('[voice.vad]\nthreshold = 0.05\n[voice.wake]\nmodel = "alexa"\n')

        assert config.hands_free_problem == "invalid"
        assert config.wake.model == "hey_jarvis"  # the good table is not kept on its own

    def test_a_bad_table_leaves_speech_and_the_rest_of_voice_as_written(self) -> None:
        config = parse(
            '[voice]\nlanguage = "de"\n[voice.wake]\nthreshold = 9\n'
            '[voice.tts]\npiper_path = "/a/p"\nvoice_model = "/a/v"\n'
        )

        assert config.language == "de"
        assert config.tts.configured is True
        assert config.hands_free_problem == "invalid"

    def test_a_bad_tts_table_leaves_hands_free_as_written(self) -> None:
        config = parse('[voice.tts]\nsample_rate_hz = 1\n[voice.wake]\nmodel = "alexa"\n')

        assert config.tts_problem == "invalid"
        assert config.hands_free_problem is None
        assert config.wake.model == "alexa"

    def test_the_problem_is_not_a_setting(self) -> None:
        with pytest.raises(ConfigError):
            parse('[voice]\nhands_free_problem = "invalid"\n')

    def test_the_voice_table_itself_is_still_refused_when_it_is_bad(self) -> None:
        with pytest.raises(ConfigError):
            parse('[voice]\nlanguage = "English"\n[voice.wake]\nthreshold = 9\n')


def pin(directory: Path, name: str, data: bytes) -> ModelFile:
    (directory / name).write_bytes(data)
    return ModelFile(name, ContentHash(hashlib.sha256(data).hexdigest()), "https://example.test/")


@pytest.fixture
def models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A models directory with four tiny files, pinned in place of the real models."""
    directory = tmp_path / "wake-models"
    directory.mkdir()
    monkeypatch.setattr(
        factory, "SILERO_VAD", pin(directory, "silero_vad.onnx", tiny_identity_model(1))
    )
    monkeypatch.setattr(
        factory, "MELSPECTROGRAM", pin(directory, "mel.onnx", tiny_identity_model(2))
    )
    monkeypatch.setattr(factory, "EMBEDDING", pin(directory, "emb.onnx", tiny_identity_model(3)))
    monkeypatch.setattr(
        factory,
        "WAKE_WORDS",
        {"hey_jarvis": pin(directory, "jarvis.onnx", tiny_identity_model(4))},
    )
    return directory


class TestBuildVoice:
    def configure(self, home: Path, models: Path, extra: str = "") -> None:
        write_config(
            home,
            f'[voice.vad]\nmodels_dir = "{models}"\n[voice.wake]\nmodels_dir = "{models}"\n{extra}',
        )

    def test_nothing_is_configured_so_there_is_no_hands_free_and_no_message(
        self, home: Path
    ) -> None:
        setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.hands_free is None
        assert setup.notice is None
        assert setup.input._trimmer is None  # noqa: SLF001  (nothing to trim with)
        setup.input.close()

    def test_a_good_configuration_gives_hands_free_off_with_the_microphone_closed(
        self, home: Path, models: Path
    ) -> None:
        self.configure(home, models)

        setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.notice is None
        assert setup.hands_free is not None
        assert setup.hands_free.phase is HandsFreePhase.OFF
        assert not setup.hands_free.enabled
        assert not setup.hands_free.microphone_open  # not opened until the user turns it on
        assert isinstance(setup.input._trimmer, VadTrimmer)  # noqa: SLF001
        setup.hands_free.close()
        setup.input.close()

    def test_the_vad_alone_trims_push_to_talk_and_gives_no_hands_free(
        self, home: Path, models: Path
    ) -> None:
        # hands-free is opt-in: it needs its own models_dir, so a user of the VAD alone is not
        # told that hands-free is off
        write_config(home, f'[voice.vad]\nmodels_dir = "{models}"\n')

        setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.hands_free is None
        assert setup.notice is None
        assert isinstance(setup.input._trimmer, VadTrimmer)  # noqa: SLF001
        setup.input.close()

    def test_a_wake_table_alone_names_its_own_directory(self, home: Path, models: Path) -> None:
        write_config(home, f'[voice.wake]\nmodels_dir = "{models}"\n')

        setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.hands_free is not None
        setup.hands_free.close()
        setup.input.close()

    def test_switched_off_there_is_no_hands_free_and_no_message(
        self, home: Path, models: Path
    ) -> None:
        write_config(
            home,
            f'[voice.vad]\nmodels_dir = "{models}"\n'
            f'[voice.wake]\nmodels_dir = "{models}"\nenabled = false\n',
        )

        setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.hands_free is None
        assert setup.notice is None
        assert setup.input._trimmer is not None  # noqa: SLF001  (the trimming is the VAD's)
        setup.input.close()

    @pytest.mark.parametrize(
        "damage", ["wrong_hash", "missing_silero", "missing_wake_model", "silero_is_symlink"]
    )
    def test_a_file_that_is_not_as_pinned_turns_hands_free_off_with_a_fixed_message(
        self,
        home: Path,
        models: Path,
        tmp_path: Path,
        damage: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self.configure(home, models)
        if damage == "wrong_hash":
            (models / "jarvis.onnx").write_bytes(tiny_identity_model(9))
        elif damage == "missing_silero":
            (models / "silero_vad.onnx").unlink()
        elif damage == "missing_wake_model":
            (models / "jarvis.onnx").unlink()
        else:
            real = tmp_path / "real.onnx"
            (models / "silero_vad.onnx").rename(real)
            (models / "silero_vad.onnx").symlink_to(real)

        with caplog.at_level("WARNING"):
            setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.input is not None  # push-to-talk still works
        assert setup.hands_free is None
        assert setup.notice == "Hands-free is off: models missing, or not as pinned (see the log)."
        assert str(models) not in setup.notice  # a path is not echoed into the UI
        assert "is off" in caplog.text
        setup.input.close()

    def test_a_bad_wake_model_still_leaves_the_trimming_when_the_vad_file_is_good(
        self, home: Path, models: Path
    ) -> None:
        self.configure(home, models)
        (models / "jarvis.onnx").unlink()

        setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.hands_free is None
        assert setup.input._trimmer is not None  # noqa: SLF001
        setup.input.close()

    def test_a_models_directory_that_is_a_symlink_is_refused(
        self, home: Path, models: Path, tmp_path: Path
    ) -> None:
        alias = tmp_path / "alias"
        alias.symlink_to(models)
        write_config(home, f'[voice.vad]\nmodels_dir = "{alias}"\n')

        setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.hands_free is None
        assert setup.input._trimmer is None  # noqa: SLF001  (no model was read through a link)
        assert setup.notice == "Hands-free is off: models missing, or not as pinned (see the log)."
        setup.input.close()

    @pytest.mark.parametrize(
        "table",
        [
            "[voice.wake]\nthreshold = 7",
            '[voice.vad]\nmodels_dir = "relative"',
            '[voice.wake]\nmodel = "computer"',
            "[voice.vad]\nsurprise = 1",
        ],
    )
    def test_a_mistake_in_either_table_turns_hands_free_off_and_keeps_push_to_talk(
        self, home: Path, table: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        write_config(home, f"{table}\n")

        with caplog.at_level("WARNING"):
            setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None  # the Mic, the key and the speech server are all still there
        assert setup.input is not None
        assert setup.hands_free is None
        assert setup.notice == "Hands-free is off: invalid [voice.vad] or [voice.wake] setting."
        assert "hands-free is off" in caplog.text
        setup.input.close()

    def test_both_kinds_of_mistake_are_told_together(self, home: Path) -> None:
        write_config(home, "[voice.tts]\nsample_rate_hz = 1\n[voice.wake]\nthreshold = 7\n")

        setup = build_voice(environ_for(home), ManualDispatcher())

        assert setup is not None
        assert setup.notice == (
            "Speech output is off: [voice.tts] has an invalid setting. "
            "Hands-free is off: invalid [voice.vad] or [voice.wake] setting."
        )
        setup.input.close()

    def test_a_mistake_in_voice_itself_still_turns_voice_off(self, home: Path) -> None:
        write_config(home, '[voice]\nlanguage = "English"\n[voice.wake]\nthreshold = 2\n')

        assert build_voice(environ_for(home), ManualDispatcher()) is None

    def test_a_reply_that_says_the_wake_phrase_reaches_the_synthesiser_without_it(
        self, home: Path, models: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Through `build_voice`, with hands-free set up: Piper (a stand-in that makes
        `len(sentence) * 10` samples) is asked for the sentence with the phrase replaced."""
        from tumbleweed.voice.fake import FakeAudioPlayer

        player = FakeAudioPlayer()
        monkeypatch.setattr(factory, "_gstreamer_player", lambda: player)
        program, voice = make_fake_piper(tmp_path)
        write_config(
            home,
            f'[voice.vad]\nmodels_dir = "{models}"\n[voice.wake]\nmodels_dir = "{models}"\n'
            f'[voice.tts]\npiper_path = "{program}"\nvoice_model = "{voice}"\n',
        )
        dispatcher = ManualDispatcher()
        setup = build_voice(environ_for(home), dispatcher)
        assert setup is not None
        assert setup.speech is not None
        try:
            setup.speech.begin_reply()
            setup.speech.feed("Please say hey jarvis now.")
            setup.speech.end_reply()
            assert dispatcher.pump_until(lambda: len(player.played) == 1)
        finally:
            setup.speech.close()
            setup.input.close()

        # "Please say the wake word now." is 29 characters: ten samples each, two bytes a sample
        assert len(player.played[0].pcm) == 29 * 10 * 2

    def test_without_hands_free_the_phrase_is_not_touched(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tumbleweed.voice.fake import FakeAudioPlayer

        player = FakeAudioPlayer()
        monkeypatch.setattr(factory, "_gstreamer_player", lambda: player)
        program, voice = make_fake_piper(tmp_path)
        write_config(home, f'[voice.tts]\npiper_path = "{program}"\nvoice_model = "{voice}"\n')
        dispatcher = ManualDispatcher()
        setup = build_voice(environ_for(home), dispatcher)
        assert setup is not None
        assert setup.speech is not None
        try:
            setup.speech.begin_reply()
            setup.speech.feed("Please say hey jarvis now.")
            setup.speech.end_reply()
            assert dispatcher.pump_until(lambda: len(player.played) == 1)
        finally:
            setup.speech.close()
            setup.input.close()

        assert len(player.played[0].pcm) == 26 * 10 * 2  # as written: 26 characters

    def test_without_gstreamer_turning_it_on_says_so_and_stays_off(
        self, home: Path, models: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "tumbleweed.voice.gstreamer", None)  # import fails
        self.configure(home, models)
        dispatcher = ManualDispatcher()
        setup = build_voice(environ_for(home), dispatcher)
        assert setup is not None
        assert setup.hands_free is not None
        problems: list[str] = []

        class Watch:
            def phase_changed(self, phase: HandsFreePhase) -> None: ...
            def microphone_changed(self, is_open: bool) -> None: ...
            def problem(self, message: str) -> None:
                problems.append(message)

        setup.hands_free.subscribe(Watch())
        setup.hands_free.set_enabled(True)

        assert dispatcher.pump_until(lambda: bool(problems))
        assert "needs GStreamer" in problems[0]
        assert setup.hands_free.phase is HandsFreePhase.OFF
        setup.hands_free.close()
        setup.input.close()

    def test_the_stream_factory_names_gstreamer_when_it_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "tumbleweed.voice.gstreamer", None)

        with pytest.raises(AudioError, match="needs GStreamer"):
            factory._gstreamer_stream()  # noqa: SLF001
