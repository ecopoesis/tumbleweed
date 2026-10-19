"""Audio is never persisted (S06, ARCHITECTURE section 9). What each proof covers, honestly:

- *Python writes*: a whole utterance (capture buffer, WAV, HTTP, transcript) runs under an audit
  hook that records every write the interpreter makes to the file system, and there is none. An
  audit hook cannot see what native code does, so it says nothing about GStreamer's own writes;
- *speech output* (S07) is held to the same: a whole spoken reply (Piper's process, the synthesised
  audio, the player) makes no Python write either; what Piper and GStreamer do natively is, as
  above, out of an audit hook's sight (a Linux test plays through a real pipeline and checks that
  no file appears);
- *structure*: the voice package's sources contain no call that could write a file (an AST scan);
- *GStreamer*: the pipeline is built from a fixed list of elements, none of which writes (a file
  sink would be the way audio could reach a disk without a line of Python saying so), checked
  here by name, and `test_gstreamer.py` runs a real capture and checks that no file appears.
"""

import ast
import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.voice.stub_server import StubServer
from tests.voice.support import ManualDispatcher, Recorder, limits, pcm
from tumbleweed.voice.endpoint import SttEndpoint
from tumbleweed.voice.fake import FakeAudioSource
from tumbleweed.voice.input import VoiceInput, VoicePhase
from tumbleweed.voice.whisper import WhisperServerBackend

VOICE = Path(__file__).resolve().parents[2] / "src" / "tumbleweed" / "voice"
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_FILE_EVENTS = {
    "open",
    "os.mkdir",
    "os.rename",
    "os.replace",
    "os.link",
    "os.symlink",
    "os.truncate",
    "os.chmod",
    "shutil.copyfile",
    "tempfile.mkstemp",
    "tempfile.mkdtemp",
}

_recording = threading.Event()
_writes: list[tuple[str, tuple[object, ...]]] = []


def _hook(event: str, args: tuple[object, ...]) -> None:
    if not _recording.is_set() or event not in _FILE_EVENTS:
        return
    if event == "open":
        path, mode, flags = args
        if isinstance(path, int):
            return  # a descriptor wrapped in a file object (a subprocess's pipes): no file is made
        flags_int = flags if isinstance(flags, int) else 0
        mode_text = mode if isinstance(mode, str) else ""
        if not (set(mode_text) & set("wax+")) and not flags_int & _WRITE_FLAGS:
            return  # a read: certificates, modules, /dev/urandom
    _writes.append((event, args))


@pytest.fixture(scope="module", autouse=True)
def _audit_hook_installed() -> None:
    # An audit hook cannot be removed; this one does nothing unless a test is recording.
    sys.addaudithook(_hook)


@pytest.fixture
def writes() -> Iterator[list[tuple[str, tuple[object, ...]]]]:
    _writes.clear()
    yield _writes
    _recording.clear()


def utterance_through_everything(stub: StubServer, *, seconds: float = 2.0) -> list[str]:
    dispatcher = ManualDispatcher()
    voice_limits = limits(request_timeout_seconds=5)
    source = FakeAudioSource(voice_limits.max_utterance_bytes)
    voice = VoiceInput(
        audio=lambda: source,
        stt=WhisperServerBackend(SttEndpoint(stub.url), language="auto", limits=voice_limits),
        dispatcher=dispatcher,
        limits=voice_limits,
    )
    recorder = Recorder()
    voice.subscribe(recorder)
    try:
        voice.press()
        assert dispatcher.pump_until(lambda: source.capturing)
        source.feed(pcm(seconds))
        voice.release()
        assert dispatcher.pump_until(lambda: voice.phase is VoicePhase.IDLE)
    finally:
        voice.close()
    return recorder.texts


def test_a_whole_utterance_writes_nothing_to_the_file_system(
    stub: StubServer, writes: list[tuple[str, tuple[object, ...]]], tmp_path: Path
) -> None:
    utterance_through_everything(stub)  # a first run imports everything (bytecode is written once)
    before = sorted(tmp_path.rglob("*"))

    _recording.set()
    texts = utterance_through_everything(stub)
    _recording.clear()

    assert texts == ["hello world"]
    assert writes == []
    assert sorted(tmp_path.rglob("*")) == before


def spoken_reply_through_everything(piper_dir: Path) -> list[str]:
    from tumbleweed.voice.config import TtsConfig
    from tumbleweed.voice.fake import FakeAudioPlayer
    from tumbleweed.voice.output import SpeechOutput, SpeechPhase
    from tumbleweed.voice.piper import PiperBackend, piper_paths

    program, model = piper_dir / "piper", piper_dir / "voice.onnx"
    config = TtsConfig(piper_path=str(program), voice_model=str(model))
    piper, voice = piper_paths(str(program), str(model))
    dispatcher = ManualDispatcher()
    player = FakeAudioPlayer()
    speech = SpeechOutput(
        backend=PiperBackend(piper, voice, config=config),
        player=lambda: player,
        dispatcher=dispatcher,
        config=config,
    )
    try:
        speech.begin_reply()
        speech.feed("The first sentence is here. The second one follows it.")
        speech.end_reply()
        assert dispatcher.pump_until(lambda: len(player.played) == 2)
        assert dispatcher.pump_until(lambda: speech.phase is SpeechPhase.IDLE)
    finally:
        speech.close()
    return [str(len(audio.pcm)) for audio in player.played]


def test_a_whole_spoken_reply_writes_nothing_to_the_file_system(
    writes: list[tuple[str, tuple[object, ...]]], tmp_path: Path
) -> None:
    from tests.voice.fake_piper import make_fake_piper

    piper_dir = tmp_path / "piper"
    piper_dir.mkdir()
    make_fake_piper(piper_dir)
    spoken_reply_through_everything(piper_dir)  # a first run imports everything
    before = sorted(tmp_path.rglob("*"))

    _recording.set()
    played = spoken_reply_through_everything(piper_dir)
    _recording.clear()

    assert len(played) == 2
    assert writes == []
    assert sorted(tmp_path.rglob("*")) == before


def test_the_audit_hook_would_have_noticed_a_write(
    writes: list[tuple[str, tuple[object, ...]]], tmp_path: Path
) -> None:
    """The proof above is only worth something if the hook sees writes."""
    _recording.set()
    (tmp_path / "leak.pcm").write_bytes(pcm(0.1))
    _recording.clear()

    assert [event for event, _ in writes] == ["open"]


def _sources() -> dict[str, ast.Module]:
    return {
        path.name: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(VOICE.glob("*.py"))
    }


def test_no_voice_module_can_write_a_file() -> None:
    forbidden_modules = {"tempfile", "shutil", "os", "pathlib", "sqlite3", "pickle", "shelve"}
    forbidden_calls = {
        "open",
        "write_bytes",
        "write_text",
        "mkstemp",
        "mkdtemp",
        "NamedTemporaryFile",
    }
    problems: list[str] = []
    for name, tree in _sources().items():
        allowed = _ALLOWED_IMPORTS.get(name, frozenset())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                problems += [
                    f"{name}: imports {a.name}"
                    for a in node.names
                    if a.name.split(".")[0] in forbidden_modules - allowed
                ]
            elif (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").split(".")[0] in forbidden_modules - allowed
            ):
                problems.append(f"{name}: imports from {node.module}")
            elif isinstance(node, ast.Call):
                function = node.func
                called = (
                    function.id
                    if isinstance(function, ast.Name)
                    else (function.attr if isinstance(function, ast.Attribute) else "")
                )
                if called in forbidden_calls and not (
                    name == "modelfile.py" and ast.unparse(node.func) == "os.open"
                ):  # modelfile's read-only opens are pinned by their own test below
                    problems.append(f"{name}:{node.lineno}: calls {called}")

    # The one `open` there is: the WAV wrapper writes to an in-memory buffer.
    assert problems == [f"whisper.py:{_wave_open_line()}: calls open"]


# Piper (S07) is the one module that may import `os`: it checks the voice model's file type, kills
# a process group and reads and writes the child's pipes. The next test pins everything it does
# with it, so the exception cannot grow into a way to write a file.
_ALLOWED_IMPORTS = {"piper.py": frozenset({"os"}), "modelfile.py": frozenset({"os"})}
_PIPER_OS_CALLS = {"stat", "access", "killpg", "set_blocking", "read", "write"}


def test_piper_uses_os_only_for_file_types_process_groups_and_pipes() -> None:
    tree = _sources()["piper.py"]
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    }

    assert used <= _PIPER_OS_CALLS | {"X_OK"}
    assert {"killpg", "set_blocking"} <= used


# `modelfile` (S08) is the other: it opens the speech-detection models, read-only, with the
# symlink-proof walk, and reads them. The test below pins every `os` name it uses and the flags of
# every open, so the exception cannot become a way to write a file.
_MODELFILE_OS = {"open", "close", "fstat", "read"}
_MODELFILE_FLAGS = {
    "O_RDONLY",
    "O_CLOEXEC",
    "O_NOFOLLOW",
    "O_NOCTTY",
    "O_DIRECTORY",
    "O_NONBLOCK",
}


def test_modelfile_opens_files_read_only_and_nothing_else() -> None:
    tree = _sources()["modelfile.py"]
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    }
    opens = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "os.open"
    ]

    assert used <= _MODELFILE_OS | _MODELFILE_FLAGS
    assert {name for name in used if name.startswith("O_")} == _MODELFILE_FLAGS
    assert len(opens) == 3
    assert sorted(ast.unparse(call.args[1]) for call in opens) == [
        "_DIRECTORY",
        "_DIRECTORY",
        "_FILE",
    ]
    flags = {
        node.target.id: ast.unparse(node.value)
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    }
    assert flags["_COMMON"] == "os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NOCTTY"
    assert flags["_DIRECTORY"] == "_COMMON | os.O_DIRECTORY"
    assert flags["_FILE"] == "_COMMON | os.O_NONBLOCK"


def _wave_open_line() -> int:
    tree = _sources()["whisper.py"]
    (call,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "open"
    ]
    assert ast.unparse(call).startswith("wave.open(out, ")
    assert "out = io.BytesIO()" in ast.unparse(tree)
    return call.lineno


def test_the_gstreamer_pipeline_is_built_from_fixed_elements_none_of_which_writes() -> None:
    tree = _sources()["gstreamer.py"]
    made = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_make"
        and isinstance(node.args[0], ast.Constant)
    }
    named = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.endswith(("src", "sink", "enc", "mux"))
        and " " not in node.value
    }

    assert made == {
        "audiotestsrc",
        "audioconvert",
        "audioresample",
        "capsfilter",
        "appsink",
        "appsrc",
        "volume",
        "fakesink",
    }
    assert {"filesink", "multifilesink", "splitmuxsink", "wavenc"}.isdisjoint(named)
    assert named <= {
        "audiotestsrc",
        "appsink",
        "osxaudiosrc",
        "pipewiresrc",
        "pulsesrc",
        "autoaudiosrc",
        # Playback (S07): the speakers' sinks, and the test sink, which discards.
        "appsrc",
        "fakesink",
        "osxaudiosink",
        "pipewiresink",
        "pulsesink",
        "autoaudiosink",
    }
    source = (VOICE / "gstreamer.py").read_text(encoding="utf-8")
    assert "parse_launch" not in source
    assert "Gst.parse" not in source
