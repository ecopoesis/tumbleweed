"""`PiperBackend` against a stand-in program (S07): the argv, the environment, the bounds on every
stream, the timeout, the kill on abort, and that no process outlives a call.

The stand-in is a real subprocess (`tests/voice/fake_piper.py`), so the tests exercise the real
pipes, process group and reaping; only Piper's own synthesis is missing (a `live` test has that).
"""

import contextlib
import functools
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.voice.fake_piper import alive, make_fake_piper, zombie_children
from tumbleweed.core.types import CanonicalPath
from tumbleweed.voice.config import TtsConfig
from tumbleweed.voice.errors import TtsCancelledError, TtsError, TtsResponseError
from tumbleweed.voice.piper import PIPER_ENVIRONMENT, PiperBackend, piper_paths
from tumbleweed.voice.stt import AbortSignal


def backend_for(
    directory: Path,
    *,
    sample_rate_hz: int = 22_050,
    synthesis_timeout_seconds: int = 20,
    max_audio_seconds: int = 60,
) -> tuple[PiperBackend, Path]:
    program, model = make_fake_piper(directory)
    tts = TtsConfig(
        piper_path=str(program),
        voice_model=str(model),
        sample_rate_hz=sample_rate_hz,
        synthesis_timeout_seconds=synthesis_timeout_seconds,
        max_audio_seconds=max_audio_seconds,
    )
    piper, voice = piper_paths(str(program), str(model))
    return PiperBackend(piper, voice, config=tts), model


def report(backend: PiperBackend) -> dict[str, object]:
    audio = backend.synthesize("REPORT", AbortSignal())
    loaded: dict[str, object] = json.loads(audio.pcm)
    return loaded


class TestTheProcess:
    def test_speech_comes_back_as_audio_at_the_configured_rate(self, tmp_path: Path) -> None:
        backend, _model = backend_for(tmp_path, sample_rate_hz=16_000)

        audio = backend.synthesize("hello there", AbortSignal())

        assert audio.pcm == b"\x01\x00" * 110
        assert audio.sample_rate_hz == 16_000

    def test_it_is_started_with_an_argv_list_and_no_text_on_it(self, tmp_path: Path) -> None:
        backend, model = backend_for(tmp_path)

        info = report(backend)

        assert info["argv"] == [str(tmp_path / "piper"), "--model", str(model), "--output_raw"]
        assert info["stdin"] == "REPORT"

    def test_the_text_goes_on_stdin_never_on_the_command_line(self, tmp_path: Path) -> None:
        backend, _model = backend_for(tmp_path)
        text = "$(touch pwned); `id` && echo $HOME | tee /dev/null"

        audio = backend.synthesize(text, AbortSignal())

        assert len(audio.pcm) == len(text) * 20  # the script read all of it from stdin
        assert not (tmp_path / "pwned").exists()

    def test_it_is_started_through_no_shell(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend, _model = backend_for(tmp_path)
        calls: list[dict[str, object]] = []
        real = subprocess.Popen

        def spy(*args: object, **kwargs: object) -> "subprocess.Popen[bytes]":
            calls.append({"args": args, **kwargs})
            return real(*args, **kwargs)  # type: ignore[call-overload, no-any-return]  # a test spy forwards what it was given

        monkeypatch.setattr("tumbleweed.voice.piper.subprocess.Popen", spy)

        backend.synthesize("hello there", AbortSignal())

        (call,) = calls
        assert call["shell"] is False
        assert isinstance(call["args"], tuple)
        assert isinstance(call["args"][0], list)
        assert call["start_new_session"] is True
        assert call["close_fds"] is True
        assert call["cwd"] == "/"

    def test_it_gets_a_minimal_environment_and_none_of_ours(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secrets = {
            "ANTHROPIC_API_KEY": "sk-ant-secret",
            "ANTHROPIC_AUTH_TOKEN": "token-secret",
            "TUMBLEWEED_BROKER_SOCKET": "/run/broker.sock",
            "TUMBLEWEED_STT_URL": "http://internal:8080",
            "SSH_AUTH_SOCK": "/run/agent.sock",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "HOME": "/home/someone",
        }
        for name, value in secrets.items():
            monkeypatch.setenv(name, value)
        backend, _model = backend_for(tmp_path)

        info = report(backend)

        env = info["env"]
        assert isinstance(env, dict)
        assert not set(secrets) & set(env)
        assert not any("secret" in str(value) for value in env.values())
        for name, value in PIPER_ENVIRONMENT.items():
            assert env[name] == value
        # macOS adds its own variable to every process; nothing else may be there.
        assert set(env) - set(PIPER_ENVIRONMENT) <= {"__CF_USER_TEXT_ENCODING"}

    def test_it_runs_in_a_directory_and_a_session_of_its_own(self, tmp_path: Path) -> None:
        backend, _model = backend_for(tmp_path)

        info = report(backend)

        assert info["cwd"] == "/"
        assert info["pid"] == info["pgid"] == info["sid"]  # a session leader: its own group
        assert info["pgid"] != os.getpgrp()

    def test_the_environment_is_a_fixed_read_only_table(self) -> None:
        with pytest.raises(TypeError):
            PIPER_ENVIRONMENT["EXTRA"] = "x"  # type: ignore[index]  # the point of the test


class TestTheBounds:
    def test_audio_is_read_to_a_limit_and_no_further(self, tmp_path: Path) -> None:
        backend, _model = backend_for(tmp_path, max_audio_seconds=1)  # 44,100 bytes
        started = time.monotonic()

        with pytest.raises(TtsResponseError, match="more audio than is allowed"):
            backend.synthesize("FLOOD", AbortSignal())

        assert time.monotonic() - started < 5

    def test_error_output_is_drained_but_cannot_hold_the_call_hostage(self, tmp_path: Path) -> None:
        backend, _model = backend_for(tmp_path)

        with pytest.raises(TtsResponseError):
            backend.synthesize("NOISE", AbortSignal())  # 4 MiB of stderr: over the limit

    def test_a_synthesiser_that_never_answers_is_cut_off_at_the_timeout(
        self, tmp_path: Path
    ) -> None:
        backend, model = backend_for(tmp_path, synthesis_timeout_seconds=1)
        started = time.monotonic()

        with pytest.raises(TtsResponseError, match="took too long"):
            backend.synthesize("HANG", AbortSignal())

        assert 0.9 < time.monotonic() - started < 4
        assert not alive(int((model.parent / "voice.onnx.pid").read_text()))

    def test_a_synthesiser_that_closes_its_streams_but_never_exits_is_cut_off_too(
        self, tmp_path: Path
    ) -> None:
        backend, _model = backend_for(tmp_path, synthesis_timeout_seconds=1)
        started = time.monotonic()

        with pytest.raises(TtsResponseError, match="took too long"):
            backend.synthesize("LINGER", AbortSignal())

        assert time.monotonic() - started < 4

    def test_a_sentence_that_is_too_long_is_refused_before_anything_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend, _model = backend_for(tmp_path)
        started: list[object] = []
        monkeypatch.setattr(
            "tumbleweed.voice.piper.subprocess.Popen", lambda *a, **k: started.append(a)
        )

        with pytest.raises(TtsError, match="too long"):
            backend.synthesize("é" * 3000, AbortSignal())

        assert started == []

    @pytest.mark.parametrize("text", ["", "two\nlines", "tab\there", "bell\x07", "‮over"])
    def test_text_that_is_not_one_clean_line_is_refused_before_anything_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
    ) -> None:
        backend, _model = backend_for(tmp_path)
        started: list[object] = []
        monkeypatch.setattr(
            "tumbleweed.voice.piper.subprocess.Popen", lambda *a, **k: started.append(a)
        )

        with pytest.raises(TtsError):
            backend.synthesize(text, AbortSignal())

        assert started == []

    def test_a_failing_synthesiser_is_an_error_with_fixed_text(self, tmp_path: Path) -> None:
        backend, _model = backend_for(tmp_path)

        with pytest.raises(TtsResponseError) as caught:
            backend.synthesize("FAIL", AbortSignal())

        assert str(caught.value) == "Piper could not speak that."
        assert "secret-looking" not in str(caught.value)

    def test_a_synthesiser_that_says_nothing_is_an_error(self, tmp_path: Path) -> None:
        backend, _model = backend_for(tmp_path)

        with pytest.raises(TtsResponseError):
            backend.synthesize("EMPTY", AbortSignal())

    def test_a_trailing_odd_byte_is_dropped(self, tmp_path: Path) -> None:
        backend, _model = backend_for(tmp_path)

        assert backend.synthesize("ODD", AbortSignal()).pcm == b"\x01\x00"

    def test_a_program_that_cannot_start_is_an_error_not_a_crash(self, tmp_path: Path) -> None:
        backend, _model = backend_for(tmp_path)
        (tmp_path / "piper").unlink()

        with pytest.raises(TtsError, match="could not be started"):
            backend.synthesize("hello there", AbortSignal())

    def test_error_output_is_logged_bounded_and_not_shown(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        backend, _model = backend_for(tmp_path)

        with caplog.at_level("WARNING"), pytest.raises(TtsResponseError):
            backend.synthesize("FAIL", AbortSignal())

        assert "boom" in caplog.text
        assert max(len(line) for line in caplog.text.splitlines()) < 400


class TestBargeIn:
    def test_an_abort_before_the_start_starts_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend, _model = backend_for(tmp_path)
        started: list[object] = []
        monkeypatch.setattr(
            "tumbleweed.voice.piper.subprocess.Popen", lambda *a, **k: started.append(a)
        )
        abort = AbortSignal()
        abort.set()

        with pytest.raises(TtsCancelledError):
            backend.synthesize("hello there", abort)

        assert started == []

    def test_an_abort_kills_the_process_at_once_from_the_aborting_thread(
        self, tmp_path: Path
    ) -> None:
        backend, model = backend_for(tmp_path)
        abort = AbortSignal()
        outcome: list[BaseException] = []

        def run() -> None:
            try:
                backend.synthesize("HANG", abort)
            except TtsCancelledError as exc:
                outcome.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        pid_file = Path(f"{model}.pid")
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        pid = int(pid_file.read_text())
        assert alive(pid)

        started = time.monotonic()
        abort.set()
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert len(outcome) == 1
        assert time.monotonic() - started < 1
        assert not alive(pid)  # killed and reaped: not even a zombie

    def test_an_abort_kills_the_whole_process_group(self, tmp_path: Path) -> None:
        backend, model = backend_for(tmp_path)
        abort = AbortSignal()
        thread = threading.Thread(
            target=lambda: _swallow(lambda: backend.synthesize("TREE", abort), TtsCancelledError)
        )
        thread.start()
        pids_file = Path(f"{model}.pids")
        deadline = time.monotonic() + 10
        while not pids_file.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        time.sleep(0.1)
        parent, child = (int(p) for p in pids_file.read_text().split())
        assert alive(parent)
        assert alive(child)

        abort.set()
        thread.join(timeout=5)

        deadline = time.monotonic() + 3
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not alive(parent)
        assert not alive(child)

    def test_a_timeout_kills_the_whole_process_group_too(self, tmp_path: Path) -> None:
        backend, model = backend_for(tmp_path, synthesis_timeout_seconds=1)

        with pytest.raises(TtsResponseError):
            backend.synthesize("TREE", AbortSignal())

        parent, child = (int(p) for p in Path(f"{model}.pids").read_text().split())
        deadline = time.monotonic() + 3
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not alive(parent)
        assert not alive(child)

    def test_no_zombie_is_left_after_any_outcome(self, tmp_path: Path) -> None:
        backend, _model = backend_for(tmp_path, synthesis_timeout_seconds=1)
        before = set(zombie_children())  # other tests of a whole run may have left some
        for text in ("hello there", "FAIL", "EMPTY", "HANG", "FLOOD"):
            _swallow(functools.partial(backend.synthesize, text, AbortSignal()), TtsError)

        assert set(zombie_children()) - before == set()

    def test_a_late_abort_after_the_call_signals_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend, _model = backend_for(tmp_path)
        abort = AbortSignal()
        backend.synthesize("hello there", abort)
        signalled: list[tuple[int, int]] = []
        monkeypatch.setattr(
            "tumbleweed.voice.piper.os.killpg", lambda pid, sig: signalled.append((pid, sig))
        )

        abort.set()  # barge-in after the sentence was done: the pid may be someone else's now

        assert signalled == []


def test_a_process_group_is_never_signalled_once_its_leader_has_been_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reaped child's pid may be someone else's: `kill` after `poll` reaped it does nothing."""
    from tumbleweed.voice import piper as piper_module

    process = subprocess.Popen(["/usr/bin/true"], start_new_session=True)
    group = piper_module._ProcessGroup(process)  # noqa: SLF001  (the class under test)
    deadline = time.monotonic() + 5
    while group.poll() is None and time.monotonic() < deadline:
        time.sleep(0.005)
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "tumbleweed.voice.piper.os.killpg", lambda pid, sig: signalled.append((pid, sig))
    )

    group.kill()

    assert group.poll() == 0
    assert signalled == []


def _swallow(call: Callable[[], object], error: type[BaseException]) -> None:
    with contextlib.suppress(error):
        call()


class TestThePaths:
    def test_a_good_pair_is_accepted(self, tmp_path: Path) -> None:
        program, model = make_fake_piper(tmp_path)

        piper, voice = piper_paths(str(program), str(model))

        assert str(piper) == str(program.resolve())
        assert str(voice) == str(model)

    def test_the_program_may_be_a_symlink_to_an_executable(self, tmp_path: Path) -> None:
        program, model = make_fake_piper(tmp_path)
        (tmp_path / "link").symlink_to(program)

        piper, _voice = piper_paths(str(tmp_path / "link"), str(model))

        assert str(piper) == str(program.resolve())  # argv[0] is the resolved target

    def test_a_voice_model_that_is_a_symlink_is_refused(self, tmp_path: Path) -> None:
        program, model = make_fake_piper(tmp_path)
        (tmp_path / "link.onnx").symlink_to(model)

        with pytest.raises(TtsError, match="no symlink"):
            piper_paths(str(program), str(tmp_path / "link.onnx"))

    def test_a_voice_model_below_a_symlinked_directory_is_refused(self, tmp_path: Path) -> None:
        program, model = make_fake_piper(tmp_path)
        (tmp_path / "dir").symlink_to(tmp_path)

        with pytest.raises(TtsError, match="no symlink"):
            piper_paths(str(program), str(tmp_path / "dir" / model.name))

    def test_a_voice_model_with_dots_is_refused(self, tmp_path: Path) -> None:
        program, model = make_fake_piper(tmp_path)

        with pytest.raises(TtsError, match="no symlink"):
            piper_paths(str(program), f"{tmp_path}/../{tmp_path.name}/{model.name}")

    def test_a_relative_path_is_refused(self, tmp_path: Path) -> None:
        program, _model = make_fake_piper(tmp_path)

        with pytest.raises(TtsError):
            piper_paths(str(program), "voice.onnx")
        with pytest.raises(TtsError):
            piper_paths("piper", str(tmp_path / "voice.onnx"))

    def test_a_directory_is_not_a_voice_model(self, tmp_path: Path) -> None:
        program, _model = make_fake_piper(tmp_path)

        with pytest.raises(TtsError, match="not a regular file"):
            piper_paths(str(program), str(tmp_path))

    def test_a_missing_voice_model_is_refused(self, tmp_path: Path) -> None:
        program, _model = make_fake_piper(tmp_path)

        with pytest.raises(TtsError, match="not found"):
            piper_paths(str(program), str(tmp_path / "missing.onnx"))

    def test_a_program_that_is_not_executable_is_refused(self, tmp_path: Path) -> None:
        program, model = make_fake_piper(tmp_path)
        program.chmod(0o600)

        with pytest.raises(TtsError, match="not an executable"):
            piper_paths(str(program), str(model))

    def test_a_missing_program_is_refused(self, tmp_path: Path) -> None:
        _program, model = make_fake_piper(tmp_path)

        with pytest.raises(TtsError, match="not found"):
            piper_paths(str(tmp_path / "nope"), str(model))

    def test_a_voice_model_swapped_for_a_symlink_after_the_check_is_refused_at_the_start(
        self, tmp_path: Path
    ) -> None:
        backend, model = backend_for(tmp_path)
        other = tmp_path / "other.onnx"
        other.write_bytes(b"other")
        model.unlink()
        model.symlink_to(other)

        with pytest.raises(TtsError, match="not a regular file"):
            backend.synthesize("hello there", AbortSignal())

    def test_the_model_path_in_argv_is_the_canonical_one_never_text_from_the_model(
        self, tmp_path: Path
    ) -> None:
        backend, model = backend_for(tmp_path)

        info = report(backend)

        assert isinstance(info["argv"], list)
        assert info["argv"][2] == str(CanonicalPath.resolve(model))
