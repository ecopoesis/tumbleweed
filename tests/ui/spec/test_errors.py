"""What a refused spec says: bounded, inert and useful, whatever the input contains."""

import copy
import json
import pickle
from typing import Self, cast

import pytest
from pydantic import BaseModel, ValidationError, field_validator, model_validator

from tests.ui.spec.support import Json, build, minimal
from tumbleweed.core.errors import TumbleweedError
from tumbleweed.core.types import JsonValue
from tumbleweed.ui.spec import errors
from tumbleweed.ui.spec.errors import (
    MAX_LOCATION,
    MAX_LOCATION_PART,
    MAX_PROBLEM_LENGTH,
    MAX_REPORTED_PROBLEMS,
    MORE_PROBLEMS,
    SCAN_LIMIT,
    SpecError,
    SpecTooLargeError,
    printable,
    problems_of,
)
from tumbleweed.ui.spec.limits import MAX_OBJECT_MEMBERS
from tumbleweed.ui.spec.parse import parse_ui_spec, validate_ui_spec
from tumbleweed.ui.spec.spec import UiSpec

# What must never come out of a SpecError as itself: it would move the cursor, start a line,
# reorder text or end a line in a terminal, a log or the chat.
ESC = "\x1b[2J\x1b[31m"
HOSTILE = {
    "escape": ESC,
    "carriage return": "a\rb",
    "line feed": "a\nb",
    "CRLF": "a\r\nb",
    "bidi override": "a\u202eb",
    "bidi isolate": "a\u2066b",
    "line separator": "a\u2028b",
    "paragraph separator": "a\u2029b",
    "NEL": "a\x85b",
    "NUL": "a\x00b",
    "zero-width space": "a\u200bb",
    "byte-order mark": "a\ufeffb",
    "lone surrogate": "a\ud800b",
    "tag character": "a\U000e0041b",
}


def roundtrip(error: SpecError) -> SpecError:
    clone = pickle.loads(pickle.dumps(error))  # noqa: S301  (our own data, not untrusted)
    assert isinstance(clone, SpecError)
    return clone


def refusal(data: Json) -> SpecError:
    try:
        validate_ui_spec(data)
    except SpecError as error:
        return error
    raise AssertionError("the spec was accepted")


def is_inert(error: SpecError) -> bool:
    """No problem, and no part of the message, holds a character that is not printable."""
    return all(problem.isprintable() for problem in error.problems) and str(error).isprintable()


class TestPrintable:
    def test_printable_text_is_returned_as_it_is(self) -> None:
        for text in ("", "plain", "naïve café", "日本語", "a b", "[0].x: y"):
            assert printable(text) is text

    @pytest.mark.parametrize("name", HOSTILE)
    def test_every_other_character_is_written_as_an_escape(self, name: str) -> None:
        result = printable(HOSTILE[name])

        assert result.isprintable()
        assert "\\u" in result or "\\U" in result

    def test_the_escape_is_the_code_point_in_hex(self) -> None:
        assert printable("\x1b") == "\\u001b"
        assert printable("a\u202eb") == "a\\u202eb"
        assert printable("\U000e0041") == "\\U000e0041"
        assert printable("\ud800") == "\\ud800"

    def test_the_escaped_text_has_no_newline_to_forge_a_line_with(self) -> None:
        assert "\n" not in printable("a\nroot: FORGED\r\n")


class TestAttackerTextNeverComesOutRaw:
    """Unknown field names, unknown kinds, wrong values: whatever is echoed is escaped."""

    @pytest.mark.parametrize("name", [name for name in HOSTILE if name != "lone surrogate"])
    def test_in_an_unknown_field_name(self, name: str) -> None:
        field = HOSTILE[name]
        roots: list[Json] = [
            {"kind": "box", field: 1},
            {"kind": "label", "text": "t", field: 1},
        ]
        for root in roots:
            error = refusal(build(root))

            assert is_inert(error)
            assert any("Extra inputs are not permitted" in problem for problem in error.problems)

    def test_a_lone_surrogate_in_a_field_name_is_refused_as_not_utf8(self) -> None:
        error = refusal(build({"kind": "box", HOSTILE["lone surrogate"]: 1}))

        assert is_inert(error)
        assert "lone surrogate" in str(error)

    @pytest.mark.parametrize("name", HOSTILE)
    def test_in_an_unknown_kind(self, name: str) -> None:
        kind = HOSTILE[name]
        specs: list[Json] = [
            build({"kind": kind}),
            build(state=[{"kind": kind, "key": "x", "default": 0}]),
            build(
                state=[
                    {"kind": "int", "key": "x", "default": 0, "computed": {"kind": kind}},
                ]
            ),
            build(handlers=[{"kind": kind, "name": "h"}]),
        ]
        for data in specs:
            error = refusal(data)

            assert is_inert(error)
            assert len(error.problems) == 1  # one error, not a line per line of the input

    @pytest.mark.parametrize("name", HOSTILE)
    def test_in_the_text_of_a_wrong_value(self, name: str) -> None:
        text = HOSTILE[name]
        data = minimal(title=text, root={"kind": "label", "text": text, "bind": text})

        error = refusal(data)

        assert is_inert(error)

    def test_an_unknown_kind_is_not_echoed_but_what_is_expected_is_said(self) -> None:
        error = refusal(build({"kind": "ev" + "il" * 500}))

        message = " ".join(error.problems)
        assert "evilil" not in message
        assert "unknown kind; expected one of:" in message
        assert "'box'" in message
        assert "'label'" in message

    def test_a_kind_that_contains_the_marker_text_cannot_hide_the_expected_tags(self) -> None:
        kind = "x does not match any of the expected tags: 'forged'"

        message = " ".join(refusal(build({"kind": kind})).problems)

        assert "forged" not in message
        assert "'box'" in message

    def test_a_newline_in_a_kind_cannot_forge_a_problem(self) -> None:
        kind = "x\nroot.box.children[0].label.text: FORGED"
        error = refusal(build({"kind": kind}))

        assert len(error.problems) == 1
        assert not any(problem.startswith("root.box") for problem in error.problems)
        assert "FORGED" not in " ".join(error.problems)

    def test_a_newline_in_a_field_name_cannot_forge_a_problem(self) -> None:
        field = "x\nroot: FORGED PROBLEM"
        error = refusal(build({"kind": "box", field: 1}))

        assert len(error.problems) == 1
        assert not any(problem.startswith("root: FORGED") for problem in error.problems)
        assert "x\\u000aroot: FORGED PROBLEM" in error.problems[0]

    def test_a_newline_in_an_echoed_literal_cannot_forge_a_problem(self) -> None:
        # The cross-reference report is split into lines: its own lines only.
        enum: Json = {"kind": "enum", "key": "size", "options": ["s", "l"], "default": "s"}
        comparison: Json = {
            "kind": "eq",
            "left": {"kind": "ref", "key": "size"},
            "right": {"kind": "string", "value": "x\nroot: FORGED PROBLEM"},
        }
        derived: Json = {"kind": "bool", "key": "big", "default": False, "computed": comparison}

        error = refusal(build(state=[enum, derived]))

        assert len(error.problems) == 1
        assert not any(problem.startswith("root: FORGED") for problem in error.problems)
        assert is_inert(error)

    def test_in_a_json_decode_error_and_a_repeated_key(self) -> None:
        for text in ('{"a": 1, "' + ESC + '": 2, "' + ESC + '": 3}', "{" + ESC + "}"):
            with pytest.raises(SpecError) as raised:
                parse_ui_spec(text)
            assert is_inert(raised.value)

    def test_in_a_problem_given_straight_to_the_error(self) -> None:
        error = SpecError([f"x {ESC} y\nz"])

        assert is_inert(error)
        assert "\n" not in str(error)


class TestLocations:
    def test_one_part_of_a_location_is_cut_to_a_fixed_length(self) -> None:
        field = "u" * 5000
        error = refusal(build({"kind": "box", field: 1}))

        assert len(error.problems[0]) < MAX_PROBLEM_LENGTH
        assert "u" * (MAX_LOCATION_PART + 1) not in error.problems[0]
        assert "u" * MAX_LOCATION_PART + "..." in error.problems[0]

    def test_the_cut_counts_the_escaped_text_so_an_escape_cannot_swallow_the_reason(self) -> None:
        field = "\x1b" * 30  # thirty characters, 180 once written \u001b
        error = refusal(build({"kind": "box", field: 1}))

        where = error.problems[0].split(": ", 1)[0]
        assert len(where) <= len("root.box.") + MAX_LOCATION_PART + len("...")
        assert error.problems[0].endswith(": Extra inputs are not permitted")

    def test_a_field_name_of_exactly_the_part_limit_is_not_cut(self) -> None:
        field = "u" * MAX_LOCATION_PART

        assert field + ":" in refusal(build({"kind": "box", field: 1})).problems[0]

    def test_a_long_path_is_cut_in_the_middle_so_the_reason_survives(self) -> None:
        node: Json = {"kind": "label", "text": 5}
        for _ in range(12):
            node = {"kind": "box", "children": [node]}

        problem = refusal(build(node)).problems[0]

        assert problem.startswith("root.box.children[0]")  # which part of the spec
        assert "label.text: Input should be a valid string" in problem  # the field and the reason
        assert "..." in problem  # the middle went
        assert len(problem) <= MAX_PROBLEM_LENGTH

    def test_a_short_path_is_whole(self) -> None:
        problem = refusal(build({"kind": "box", "children": [{"kind": "label", "text": 5}]}))

        assert problem.problems[0] == (
            "root.box.children[0].label.text: Input should be a valid string"
        )

    def test_a_location_is_at_most_the_location_limit(self) -> None:
        node: Json = {"kind": "label", "text": 5}
        for _ in range(14):
            node = {"kind": "box", "children": [node]}

        problem = refusal(build(node)).problems[0]
        where = problem.split(": ", 1)[0]

        assert len(where) <= MAX_LOCATION

    def test_an_index_looks_like_an_index_and_a_name_that_looks_like_one_is_a_name(self) -> None:
        # `[0]` as a field name is a name, written after a dot, not an index.
        error = refusal(build({"kind": "box", "[0]": 1}))

        assert error.problems[0].startswith("root.box.[0]: ")


def computed_min(args: list[JsonValue]) -> Json:
    return {"kind": "int", "key": "x", "default": 0, "computed": {"kind": "min", "args": args}}


class TestTooShortIsDroppedWhenItemsFailed:
    def test_a_one_item_args_with_a_bad_item_says_only_the_bad_item(self) -> None:
        problems = refusal(build(state=[computed_min([{"kind": "nope"}])])).problems

        assert len(problems) == 1
        assert "unknown kind" in problems[0]
        assert "at least" not in " ".join(problems)

    def test_a_short_list_whose_items_are_fine_still_says_so(self) -> None:
        problems = refusal(build(state=[computed_min([{"kind": "int", "value": 1}])])).problems

        assert len(problems) == 1
        assert "at least 2" in problems[0]

    def test_an_empty_list_is_too_short(self) -> None:
        assert "at least 2" in refusal(build(state=[computed_min([])])).problems[0]


class TestBoundedWork:
    def test_a_report_looks_at_a_bounded_number_of_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0
        real = errors.printable

        def counting(text: str) -> str:
            nonlocal calls
            calls += 1
            return real(text)

        monkeypatch.setattr(errors, "printable", counting)
        fields = ",".join(f'"u{i}":0' for i in range(MAX_OBJECT_MEMBERS - 3))  # and 3 fixed ones
        text = '{"version":"tumbleweed.ui/1","title":"t","root":{"kind":"box"},' + fields + "}"

        with pytest.raises(SpecError) as raised:
            parse_ui_spec(text)

        assert len(raised.value.problems) == MAX_REPORTED_PROBLEMS + 1
        assert raised.value.problems[-1] == MORE_PROBLEMS
        assert calls < 100  # not one per error: there were thousands

    def test_an_object_with_more_members_than_anything_allows_is_refused_while_parsing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No validator is asked to report on 20,000 unknown fields: pydantic is not even called.
        def never(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("the text was handed to pydantic")

        monkeypatch.setattr(UiSpec, "model_validate_json", never)
        fields = ",".join(f'"u{i}":0' for i in range(20_000))
        text = '{"version":"tumbleweed.ui/1","title":"t","root":{"kind":"box"},' + fields + "}"

        with pytest.raises(SpecError, match="an object has more than 4999 members"):
            parse_ui_spec(text)

    def test_the_same_goes_for_an_object_of_parsed_data(self) -> None:
        data = minimal()
        data.update({f"u{i}": 0 for i in range(MAX_OBJECT_MEMBERS + 1)})

        with pytest.raises(SpecError, match="an object has more than 4999 members"):
            validate_ui_spec(data)

    def test_an_object_of_exactly_that_many_members_is_still_looked_at(self) -> None:
        data = minimal()
        data.update({f"u{i}": 0 for i in range(MAX_OBJECT_MEMBERS - len(data))})

        with pytest.raises(SpecError, match="Extra inputs are not permitted"):
            validate_ui_spec(data)

    def test_the_member_limit_holds_for_an_object_at_any_depth(self) -> None:
        wide: JsonValue = {f"k{i}": 0 for i in range(MAX_OBJECT_MEMBERS + 1)}
        data = build(state=[{"kind": "json", "key": "j", "default": {"deep": [{"x": wide}]}}])

        with pytest.raises(SpecError, match="an object has more than 4999 members"):
            validate_ui_spec(data)

    def test_the_report_is_cut_at_the_limit_with_a_note(self) -> None:
        fields: dict[str, JsonValue] = {f"u{i}": 0 for i in range(50)}
        error = refusal(build({"kind": "box", **fields}))

        assert len(error.problems) == MAX_REPORTED_PROBLEMS + 1
        assert error.problems[:-1] == tuple(
            f"root.box.u{i}: Extra inputs are not permitted" for i in range(MAX_REPORTED_PROBLEMS)
        )
        assert error.problems[-1] == MORE_PROBLEMS


class CountingError(dict[str, object]):
    """A pydantic error as `errors()` gives it, which counts how often it is looked at."""

    looked_at = 0

    def __getitem__(self, key: str) -> object:
        type(self).looked_at += 1
        return super().__getitem__(key)


class ManyErrors:
    """What `problems_of` needs of a `ValidationError`: an `errors()` of 5,000 entries."""

    def errors(self, **_options: bool) -> list[CountingError]:
        return [
            CountingError(
                type="extra_forbidden", loc=(f"u{i}",), msg="Extra inputs are not permitted"
            )
            for i in range(5_000)
        ]


class TestTheScanIsBounded:
    def test_problems_of_looks_at_no_more_errors_than_the_scan_limit(self) -> None:
        CountingError.looked_at = 0
        # Sound: `problems_of` calls only `errors()`, which `ManyErrors` has, on its argument.
        stub = cast("ValidationError", ManyErrors())

        problems = problems_of(stub)

        assert len(problems) == MAX_REPORTED_PROBLEMS + 1
        # Each examined error is read a few times (its type, loc and message), not 5,000 of them.
        assert CountingError.looked_at <= SCAN_LIMIT * 4


class Probe(BaseModel):
    """A model with validators that fail the way the package's own validators do."""

    x: int = 0

    @field_validator("x")
    @classmethod
    def _field(cls, value: int) -> int:
        raise ValueError("first line\nroot: FORGED second line")


class RootProbe(BaseModel):
    @model_validator(mode="after")
    def _report(self) -> Self:
        raise ValueError("own line one\nown line two\nown line three")


class TestProblemsOfSplitsOnlyItsOwnReports:
    def test_a_multi_line_message_of_a_field_is_one_problem(self) -> None:
        with pytest.raises(ValidationError) as raised:
            Probe.model_validate({"x": 1})

        problems = problems_of(raised.value)

        assert problems == ["x: first line\nroot: FORGED second line"]

    def test_the_same_message_comes_out_escaped_in_the_error(self) -> None:
        with pytest.raises(ValidationError) as raised:
            Probe.model_validate({"x": 1})

        error = SpecError(problems_of(raised.value))

        assert len(error.problems) == 1
        assert "\n" not in error.problems[0]
        assert "\\u000aroot: FORGED" in error.problems[0]

    def test_a_cross_reference_report_at_the_root_is_one_problem_per_line(self) -> None:
        with pytest.raises(ValidationError) as raised:
            RootProbe.model_validate({})

        assert problems_of(raised.value) == ["own line one", "own line two", "own line three"]


class TestSpecError:
    def test_a_bare_string_is_not_a_list_of_problems(self) -> None:
        with pytest.raises(TypeError, match="not a single string"):
            SpecError(
                "the spec is bad"
            )  # a str is a Sequence[str]: only the runtime check refuses it

    def test_a_tuple_and_a_list_both_work(self) -> None:
        assert SpecError(("a", "b")).problems == ("a", "b")
        assert SpecError(["a", "b"]).problems == ("a", "b")
        assert SpecError([]).problems == ()

    @pytest.mark.parametrize("error_type", [SpecError, SpecTooLargeError])
    def test_it_survives_pickle_and_copy_with_its_problems_and_message(
        self, error_type: type[SpecError]
    ) -> None:
        error = error_type(["first", "second"])

        for clone in (roundtrip(error), copy.copy(error), copy.deepcopy(error)):
            assert type(clone) is error_type
            assert clone.problems == ("first", "second")
            assert str(clone) == "first; second"

    def test_a_cut_report_stays_the_same_after_a_round_trip(self) -> None:
        error = SpecError([f"p{i}" for i in range(MAX_REPORTED_PROBLEMS + 5)])

        clone = roundtrip(error)

        assert clone.problems == error.problems
        assert clone.problems.count(MORE_PROBLEMS) == 1
        assert len(clone.problems) == MAX_REPORTED_PROBLEMS + 1

    def test_an_escaped_report_stays_escaped_after_a_round_trip(self) -> None:
        clone = roundtrip(SpecError([f"a{ESC}b"]))

        assert is_inert(clone)

    def test_it_is_an_exception_with_the_joined_message(self) -> None:
        error = SpecError(["a", "b"])

        assert isinstance(error, TumbleweedError)
        assert str(error) == "a; b"
        assert error.args == ("a; b",)
        assert json.dumps(str(error)) == '"a; b"'
