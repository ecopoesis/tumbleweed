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
