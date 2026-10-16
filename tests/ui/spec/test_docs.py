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
from tests.ui.spec.docgen import (
    DESCRIPTIONS,
    DOC,
    INLINE,
    REGION,
    numbers,
    regenerate,
    render,
)
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
    links = [
        target
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", TEXT)
        if not target.startswith("http")
    ]

    assert len(links) >= 5  # the pattern really finds the page's links
    for target in links:
        assert (Path(DOC).parent / target).resolve().exists(), target


def test_every_anchor_link_on_the_page_has_a_heading() -> None:
    def slug(heading: str) -> str:
        return re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")

    headings = {slug(h) for h in re.findall(r"^#+ (.+)$", TEXT, re.MULTILINE)}
    anchors = re.findall(r"\]\(#([^)]+)\)", TEXT)

    assert len(anchors) >= 3
    for anchor in anchors:
        assert anchor in headings, anchor


# --- the numbers in the prose -------------------------------------------------------------


def prose(text: str) -> str:
    """The sentences of the page: no generated parts, code, links, headings, or inline numbers."""
    text = REGION.sub("", text)
    text = INLINE.sub("", text)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"\[[^\]]*\]\([^)]*\)", "", text)  # links, with their text
    text = re.sub(r"`[^`\n]*`", "", text)  # code spans
    text = re.sub(r"^#+ .*$", "", text, flags=re.MULTILINE)  # headings
    return re.sub(r"\b(?:sections?|RFC|I|S)\s?\d+\b", "", text)  # section 4, RFC 8785, I9, S10


def bare_numbers(text: str) -> list[int]:
    found = re.findall(r"(?<![\w.*+-])\d[\d,]*(?![\w])", prose(text))
    return [int(token.replace(",", "")) for token in found if token.replace(",", "")]


def test_the_marked_numbers_are_the_values_of_the_limits() -> None:
    marked = {match["name"]: match["value"] for match in INLINE.finditer(TEXT)}

    assert len(marked) >= 20  # the page quotes most of its limits
    for name, value in marked.items():
        assert value == f"{numbers()[name]:,}", name


def test_every_limit_of_the_code_is_quoted_somewhere_on_the_page() -> None:
    # In the generated table, or in the prose as a marked number.
    in_table = {name for name in all_limits() if f"| `{name}` |" in TEXT}

    assert in_table == set(all_limits())


def test_no_number_in_the_prose_is_a_limit_that_is_not_marked() -> None:
    values = set(numbers().values())
    assert len(bare_numbers(TEXT)) >= 5  # the scan does see numbers in the prose
    bare = [number for number in bare_numbers(TEXT) if number in values]

    assert bare == [], (
        f"these numbers in docs/ui-spec.md are limits: write them between `limit` comments: {bare}"
    )


def test_the_scan_finds_a_bare_limit_and_leaves_the_rest() -> None:
    sample = (
        "# 65,536 in a heading\n"
        "A string is at most 65,536 characters (section 16, RFC 8785, I9).\n"
        "<!-- limit: MAX_STRING_LENGTH -->65,536<!-- /limit --> is marked, `65,536` is code,\n"
        "[65,536](link) is a link, and 2**53 - 1 and 5e0 and v65 are not limits.\n"
    )

    assert bare_numbers(sample) == [65536, 2, 1]


def test_an_unknown_limit_in_the_prose_is_an_error() -> None:
    with pytest.raises(ValueError, match="unknown limit"):
        regenerate("<!-- limit: MAX_NOTHING -->1<!-- /limit -->\n")


def test_a_changed_limit_changes_the_prose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docgen, "all_limits", lambda: {**all_limits(), "MAX_NAME_LENGTH": 99})

    changed = regenerate(TEXT)

    assert changed != TEXT
    assert "<!-- limit: MAX_NAME_LENGTH -->99<!-- /limit -->" in changed
    assert "<!-- limit: MAX_NAME_LENGTH -->64<!-- /limit -->" not in changed


def section(heading: str) -> str:
    """The text under a heading, up to the next heading of the same or a higher level, with its
    line breaks and runs of blanks made single spaces (so a needle may span a line)."""
    level = heading.split(" ", 1)[0]
    start = TEXT.index(f"\n{heading}\n") + 1
    rest = TEXT[start + len(heading) :]
    end = re.search(rf"\n#{{1,{len(level)}}} ", rest)
    return " ".join((rest[: end.start()] if end else rest).split())


class TestTheRulesForTheCodeThatOpensAReference:
    """The spec accepts `-rf.png`, `~root/x.png` and `%2e%2e/x.png`: the page must say how
    they are opened, because the spec cannot make them safe (R2-F9; S11 tests the names)."""

    TEXT_OF_THE_SECTION = section("## 6. Value types and ports")

    @pytest.mark.parametrize(
        "needle",
        [
            "dirfd",
            "O_NOFOLLOW",
            "expanduser",
            "URI builder",
            "percent decoder",
            "`--`",
            "`./`",
            "-rf.png",
            "~root/x.png",
            "%2e%2e/x.png",
        ],
    )
    def test_the_rule_is_on_the_page(self, needle: str) -> None:
        assert needle in self.TEXT_OF_THE_SECTION

    def test_the_names_are_valid_references(self) -> None:
        for name in ("-rf.png", "~root/x.png", "%2e%2e/x.png"):
            assert parse_ref(name) == name


def parse_ref(path: str) -> str:
    from tumbleweed.ui.spec.values import ImageRef

    return str(ImageRef.model_validate({"path": path}).path)


class TestTheEvaluationRules:
    EVALUATION = section("### Evaluation")

    def test_a_write_that_grows_a_state_over_its_budget_is_not_applied(self) -> None:
        assert "MAX_STATE_TEXT_BYTES" in self.EVALUATION
        assert "makes that text grow is not applied: the entry keeps its" in self.EVALUATION
        assert "shrinks the text, or leaves it the same size, is applied" in self.EVALUATION

    def test_the_state_counts_computed_entries(self) -> None:
        assert "computed ones included" in self.EVALUATION

    def test_the_budget_section_says_current_is_required_and_whole(self) -> None:
        state = section("## 5. State")

        assert "`current` is required" in state
        assert "computed entries included" in state


def test_validate_ui_spec_is_documented_to_refuse_not_convert() -> None:
    how = section("## 1. How a spec is accepted")

    for needle in ("tuple", "enum member", "key that is not a string", "never converted"):
        assert needle in how


def test_the_page_no_longer_has_a_limit_on_how_many_positions_a_step_makes() -> None:
    assert "MAX_WIDGET_STEPS" not in TEXT
    assert "how many positions the step makes is up to the author" in section("## 8. Components")
