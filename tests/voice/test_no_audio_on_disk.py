"""Audio is never persisted (S06, ARCHITECTURE section 9).

Two proofs. Behaviour: a whole utterance (capture, WAV, HTTP, transcript) runs under an audit hook
that records every write to the file system, and there is none. Structure: the voice package's
sources contain no call that could write a file (an AST scan), and the GStreamer pipeline is built
from a fixed list of elements, none of which writes (a file sink would be the way audio could reach
a disk without a line of Python saying so).
"""

import ast
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
_WRITE_FLAGS = 0o3 | 0o100 | 0o2000 | 0o1000  # O_WRONLY | O_RDWR | O_CREAT | O_APPEND | O_TRUNC
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
        _path, mode, flags = args
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
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                problems += [
                    f"{name}: imports {a.name}"
                    for a in node.names
                    if a.name.split(".")[0] in forbidden_modules
                ]
            elif (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").split(".")[0] in forbidden_modules
            ):
                problems.append(f"{name}: imports from {node.module}")
            elif isinstance(node, ast.Call):
                function = node.func
                called = (
                    function.id
                    if isinstance(function, ast.Name)
                    else (function.attr if isinstance(function, ast.Attribute) else "")
                )
                if called in forbidden_calls:
                    problems.append(f"{name}:{node.lineno}: calls {called}")

    # The one `open` there is: the WAV wrapper writes to an in-memory buffer.
    assert problems == [f"whisper.py:{_wave_open_line()}: calls open"]


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

    assert made == {"audiotestsrc", "audioconvert", "audioresample", "capsfilter", "appsink"}
    assert {"filesink", "multifilesink", "splitmuxsink", "wavenc"}.isdisjoint(named)
    assert named <= {
        "audiotestsrc",
        "appsink",
        "osxaudiosrc",
        "pipewiresrc",
        "pulsesrc",
        "autoaudiosrc",
    }
    source = (VOICE / "gstreamer.py").read_text(encoding="utf-8")
    assert "parse_launch" not in source
    assert "Gst.parse" not in source
