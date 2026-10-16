"""`docs/ui-spec.md` cannot drift from the code: its examples, limits and tables are checked.

The generated regions (see `docgen`) are compared with what the code renders. Every JSON example
on the page is inside one, so every example is a golden spec or a part of one, and the golden
specs are valid (test_golden). The two tables of components are compared with the data the
tests of the cross-reference rules use.
"""

import json
import re
from pathlib import Path

import pytest

from tests.ui.spec import docgen
from tests.ui.spec.docgen import DESCRIPTIONS, DOC, REGION, regenerate, render
from tests.ui.spec.support import BOUND, EVENTS, GOLDEN_DIR, UNBOUND, golden, golden_text
from tumbleweed.ui.spec.limits import all_limits
from tumbleweed.ui.spec.parse import parse_ui_spec

TEXT = DOC.read_text(encoding="utf-8")
REGIONS = [match["name"] for match in REGION.finditer(TEXT)]
REGENERATE = "uv run python -m tests.ui.spec.docgen --write"


def test_the_page_has_every_region_it_should() -> None:
    assert set(REGIONS) >= {
        "limits",
        "ports",
        "kinds expr",
        "kinds state",
        "kinds component",
        "kinds handler",
        "fixture tip_calculator",
        "fixture photo_adjuster",
        "fixture csv_viewer",
    }
    assert len(REGIONS) == len(set(REGIONS)), "a region appears twice"


@pytest.mark.parametrize("name", sorted(set(REGIONS)))
def test_a_generated_region_is_what_the_code_renders(name: str) -> None:
    body = next(match["body"] for match in REGION.finditer(TEXT) if match["name"] == name)

    assert body == render(name), f"docs/ui-spec.md is out of date: run `{REGENERATE}`"


def test_regenerating_changes_nothing() -> None:
    assert regenerate(TEXT) == TEXT, f"run `{REGENERATE}`"


def test_every_region_is_closed_and_none_is_nested() -> None:
    assert TEXT.count("<!-- generated:") == TEXT.count("<!-- /generated -->") == len(REGIONS)
    for match in REGION.finditer(TEXT):
        assert "<!-- generated:" not in match["body"]


def test_an_unknown_region_is_an_error_not_a_silent_skip() -> None:
    with pytest.raises(ValueError, match="unknown region"):
        render("kinds nonsense")
    with pytest.raises(ValueError, match="unknown region"):
        render("fixture nonexistent")
    with pytest.raises(ValueError, match="unknown region"):
        regenerate("<!-- generated: bogus -->\nx\n<!-- /generated -->\n")


def test_every_json_example_is_inside_a_generated_region() -> None:
    outside = REGION.sub("", TEXT)

    assert "```json" not in outside


def test_every_golden_spec_is_shown_in_full() -> None:
    shown = {name.split(" ", 1)[1] for name in REGIONS if name.startswith("fixture ")}

    assert shown == {path.stem for path in GOLDEN_DIR.glob("*.json")}


def test_the_examples_on_the_page_are_valid_specs() -> None:
    for match in REGION.finditer(TEXT):
        if match["name"].startswith("fixture "):
            block = match["body"].removeprefix("```json\n").removesuffix("```\n")
            assert parse_ui_spec(block).version == "tumbleweed.ui/1"
            assert block == golden_text(match["name"].split(" ", 1)[1])


def test_the_fragment_is_a_real_part_of_its_golden_spec() -> None:
    tip = golden("tip_calculator")
    state = tip["state"]
    assert isinstance(state, list)
    entry = state[3]
    assert isinstance(entry, dict)

    assert render("fragment tip_calculator /state/3/computed").strip("`json\n") == json.dumps(
        entry["computed"], indent=2
    )
    assert entry["key"] == "tip_amount"


def test_the_limits_table_describes_every_limit_and_only_those() -> None:
    assert set(DESCRIPTIONS) == set(all_limits())
    listed = re.findall(r"^\| `(MAX_[A-Z_]+)` \|", render("limits"), re.MULTILINE)

    assert listed == list(all_limits())


def test_a_changed_limit_changes_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docgen, "all_limits", lambda: {**all_limits(), "MAX_SPACING": 65})

    assert "| `MAX_SPACING` | 65 |" in render("limits")
    assert regenerate(TEXT) != TEXT


# --- the tables of components -------------------------------------------------------------


def table_after(marker: str) -> list[list[str]]:
    """The cells of the rows (without the header) of the table that follows `<!-- marker -->`."""
    start = TEXT.index(f"<!-- verified: {marker} -->\n") + len(f"<!-- verified: {marker} -->\n")
    lines = []
    for line in TEXT[start:].splitlines():
        if not line.startswith("|"):
            break
        lines.append(line)
    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]
    return rows[2:]  # the header and the rule


def names(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def test_the_bindings_table_matches_what_the_cross_reference_checks_do() -> None:
    rows = {names(row[0])[0]: row[1:] for row in table_after("bindings")}

    assert set(rows) == set(BOUND)
    for component, (_, kinds, writes, bounded) in BOUND.items():
        binds, two_way, needs = rows[component]
        assert set(names(binds)) == kinds, component
        expected_two_way = (
            "if `editable`" if component == "text_view" else "yes" if writes else "no"
        )
        assert two_way == expected_two_way, component
        assert needs == ("yes" if bounded else "no"), component


def test_the_components_that_bind_nothing_are_the_ones_the_page_says() -> None:
    sentence = re.search(r"(`box`, [^.]*bind nothing)", TEXT)
    assert sentence is not None

    assert set(names(sentence.group(1))) == set(UNBOUND)


def test_the_events_table_matches_the_components() -> None:
    rows = {names(row[0])[0]: names(row[1]) for row in table_after("events")}

    assert rows == {component: list(events) for component, events in EVENTS.items()}


def test_every_relative_link_on_the_page_goes_somewhere() -> None:
    for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", TEXT):
        if target.startswith("http"):
            continue
        assert (Path(DOC).parent / target).resolve().exists(), target


def test_every_anchor_link_on_the_page_has_a_heading() -> None:
    def slug(heading: str) -> str:
        return re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")

    headings = {slug(h) for h in re.findall(r"^#+ (.+)$", TEXT, re.MULTILINE)}
    for anchor in re.findall(r"\]\(#([^)]+)\)", TEXT):
        assert anchor in headings, anchor
