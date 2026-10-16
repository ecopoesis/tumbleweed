"""The system prompt is a versioned resource file that ships with the package."""

import re
import subprocess
import zipfile
from pathlib import Path

import pytest

from tumbleweed.agent.prompts import MAX_PROMPT_BYTES, SYSTEM_PROMPT_VERSION, load_system_prompt
from tumbleweed.core.errors import InvalidValueError

ROOT = Path(__file__).resolve().parents[2]


def test_the_current_version_loads_and_says_what_matters() -> None:
    text = load_system_prompt()

    assert SYSTEM_PROMPT_VERSION == "v1"
    assert 0 < len(text.encode()) <= MAX_PROMPT_BYTES
    assert "plain text" in text  # the chat shows no markup
    assert "data" in text  # untrusted content is framed as data
    assert text == text.strip()


def test_the_file_name_carries_the_version() -> None:
    assert (ROOT / "src/tumbleweed/agent/prompts" / f"system_{SYSTEM_PROMPT_VERSION}.md").is_file()


@pytest.mark.parametrize("version", ["v999", "", "../prompts/system_v1", "v1.md", "v 1", "v\n1"])
def test_an_unknown_or_unsafe_version_is_refused(version: str) -> None:
    with pytest.raises(InvalidValueError):
        load_system_prompt(version)


def test_the_prompt_holds_no_secret_or_personal_data() -> None:
    text = load_system_prompt()

    assert not re.search(r"sk-ant|/home/|/Users/|@\w+\.\w+", text)


@pytest.mark.timeout(120)
def test_the_built_package_ships_the_prompt(tmp_path: Path) -> None:

    result = subprocess.run(  # noqa: S603  (our own interpreter and uv, an argv list)
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path), "--no-build-logs", str(ROOT)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=100,
    )
    if result.returncode != 0:
        pytest.skip(f"uv build is not usable here: {result.stderr[-200:]}")
    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()

    assert f"tumbleweed/agent/prompts/system_{SYSTEM_PROMPT_VERSION}.md" in names
