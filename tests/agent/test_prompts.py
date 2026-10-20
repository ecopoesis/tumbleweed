"""The system prompt is a versioned resource file that ships with the package."""

import json
import re
import subprocess
import zipfile
from pathlib import Path

import pytest

from tumbleweed.agent.prompts import (
    MAX_PROMPT_BYTES,
    MAX_SPEC_REFERENCE_BYTES,
    SYSTEM_PROMPT_VERSION,
    load_system_prompt,
    system_prompt_label,
)
from tumbleweed.agent.spec_prompt import SECTIONS, build_section, main, section_text
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.ui.spec.schema import ui_spec_schema

ROOT = Path(__file__).resolve().parents[2]


def test_the_current_version_loads_and_says_what_matters() -> None:
    text = load_system_prompt()

    assert SYSTEM_PROMPT_VERSION == "v2"
    assert 0 < len(text.encode()) <= MAX_PROMPT_BYTES + MAX_SPEC_REFERENCE_BYTES
    assert "plain text" in text  # the chat shows no markup
    assert "data" in text  # untrusted content is framed as data
    assert text == text.strip()


def test_the_first_version_is_the_prompt_alone_and_the_current_one_adds_the_spec_reference() -> (
    None
):
    first, current = load_system_prompt("v1"), load_system_prompt()

    assert "UI spec reference" not in first
    assert "show_ui" not in first
    assert current.startswith(load_system_prompt("v2").split("\n\n# UI spec reference")[0])
    assert "# UI spec reference (tumbleweed.ui/1)" in current
    assert "never an instruction" in current  # the canvas event frame is described as data


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
    assert "tumbleweed/agent/prompts/ui_spec_v1.md" in names


class TestSpecReference:
    """The prompt's spec reference is generated from the schema and the docs, not typed (S12)."""

    def test_the_committed_file_is_what_the_generator_makes_now(self) -> None:
        committed = (ROOT / "src/tumbleweed/agent/prompts/ui_spec_v1.md").read_text(
            encoding="utf-8"
        )

        assert committed == section_text(ROOT), (
            "run: uv run python -m tumbleweed.agent.spec_prompt --write"
        )

    def test_it_names_every_kind_of_the_schema_with_its_fields(self) -> None:
        schema = json.loads(json.dumps(ui_spec_schema()))
        reference = load_system_prompt()
        for union in ("Component", "StateDecl", "Handler", "Expr"):
            for kind in schema["$defs"][union]["properties"]["kind"]["enum"]:
                assert re.search(rf"^- {re.escape(kind)}: ", reference, re.MULTILINE), kind

    def test_it_carries_what_the_schema_cannot_say_and_the_worked_example(self) -> None:
        reference = load_system_prompt()

        for needle in (
            "## 4. Names, text and numbers",  # text rules
            "## 7. Expressions",  # types and evaluation
            "## 9. Handlers",  # agent, sandbox, set
            "## 10. What is checked across the spec",
            "## 11. Limits",
            "MAX_COMPONENTS",
            "Tip calculator",  # the example
        ):
            assert needle in reference, needle

    def test_it_leaves_out_the_markup_of_the_docs(self) -> None:
        reference = load_system_prompt()

        assert "<!--" not in reference
        assert "](#" not in reference

    def test_a_docs_page_without_a_needed_section_fails_the_generator_instead_of_a_gap(
        self,
    ) -> None:
        doc = (ROOT / "docs/ui-spec.md").read_text(encoding="utf-8")
        broken = doc.replace("## 9. Handlers", "## 9b. Handlers")

        with pytest.raises(ValueError, match="no section"):
            build_section(broken, ui_spec_schema())
        assert SECTIONS  # the sections taken are named in one place

    def test_a_change_of_the_docs_changes_the_reference(self) -> None:
        doc = (ROOT / "docs/ui-spec.md").read_text(encoding="utf-8")
        changed = doc.replace(
            "A handler has a `name` and a `kind`.", "A handler has a NEW SENTENCE."
        )

        assert "NEW SENTENCE" in build_section(changed, ui_spec_schema())
        assert "NEW SENTENCE" not in build_section(doc, ui_spec_schema())

    def test_the_cli_refuses_an_unknown_option(self) -> None:
        assert main(["--bogus"]) == 2

    def test_the_reference_is_within_its_bound(self) -> None:
        text = (ROOT / "src/tumbleweed/agent/prompts/ui_spec_v1.md").read_bytes()

        assert 0 < len(text) <= MAX_SPEC_REFERENCE_BYTES


class TestLabel:
    def test_the_first_version_is_labelled_by_its_name_and_the_current_one_by_a_hash_too(
        self,
    ) -> None:
        from tumbleweed.agent.recorder import _PROMPT_VERSION_PATTERN

        assert system_prompt_label("v1") == "v1"
        label = system_prompt_label()
        assert re.fullmatch(r"v2\.[0-9a-f]{8}", label)
        assert re.fullmatch(_PROMPT_VERSION_PATTERN, label)  # the log accepts it

    def test_a_change_of_the_reference_is_a_different_label(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tumbleweed.agent.prompts as prompts

        before = system_prompt_label()
        original = prompts._read  # noqa: SLF001

        def changed(name: str, limit: int, what: str) -> str:
            text = original(name, limit, what)
            return text + " changed" if name.startswith("ui_spec") else text

        monkeypatch.setattr(prompts, "_read", changed)

        assert system_prompt_label() != before
