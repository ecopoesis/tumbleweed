"""The voice package's import contracts (S06) name every module that must obey them.

`uv run lint-imports` checks the graph, and `tests/test_import_contracts.py` that each contract
breaks when it is violated. This checks that the list of modules one contract enumerates is the
package: a new module cannot slip past it by not being listed.
"""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VOICE = ROOT / "src" / "tumbleweed" / "voice"
ADAPTER_SIDE = {"__init__", "_gst", "gstreamer", "factory"}  # may import the adapter (or are it)


def test_the_core_contract_names_every_module_but_the_adapter_side() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    (contract,) = [
        c
        for c in config["tool"]["importlinter"]["contracts"]
        if c["name"].startswith("voice outside its GStreamer adapter")
    ]
    modules = {path.stem for path in VOICE.glob("*.py")} - ADAPTER_SIDE

    assert sorted(contract["source_modules"]) == sorted(f"tumbleweed.voice.{m}" for m in modules)
    assert sorted(contract["forbidden_modules"]) == [
        "tumbleweed.voice._gst",
        "tumbleweed.voice.gstreamer",
    ]


def test_only_the_pin_module_imports_gi() -> None:
    importing = sorted(
        path.name
        for path in VOICE.glob("*.py")
        if "import gi" in path.read_text(encoding="utf-8")
        or "from gi" in path.read_text(encoding="utf-8")
    )

    assert importing == ["_gst.py"]


def test_the_voice_package_imports_without_gstreamer_or_gtk() -> None:
    import subprocess
    import sys

    code = (
        "import sys; import tumbleweed.voice.factory, tumbleweed.voice.input; "
        "bad = [m for m in ('gi', 'tumbleweed.voice.gstreamer', 'tumbleweed.voice._gst', "
        "'tumbleweed.shell') if m in sys.modules]; assert not bad, bad"
    )
    result = subprocess.run(  # noqa: S603  (argv list, our own interpreter)
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=60
    )

    assert result.returncode == 0, result.stderr


def test_onnxruntime_is_not_loaded_until_a_model_is() -> None:
    """It loads CoreML and other frameworks on macOS: not for a shell that has no use for it, and
    not at collection, where, as GTK does, it changes how the system's SQLite treats a moved
    database (#132)."""
    import subprocess
    import sys

    code = (
        "import sys; import tumbleweed.voice.factory, tumbleweed.voice.hands_free, "
        "tumbleweed.voice.vad, tumbleweed.voice.wake, tumbleweed.voice.store; "
        "assert 'onnxruntime' not in sys.modules, 'loaded by an import'; "
        "from tests.voice.tiny_onnx import tiny_identity_model; "
        "from tumbleweed.voice.onnx import load_model; load_model(tiny_identity_model()); "
        "assert 'onnxruntime' in sys.modules, 'not loaded by load_model'"
    )
    result = subprocess.run(  # noqa: S603  (argv list, our own interpreter)
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        cwd=ROOT,
    )

    assert result.returncode == 0, result.stderr
