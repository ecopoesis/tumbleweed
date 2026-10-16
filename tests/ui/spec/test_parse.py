"""The entry points: JSON text or parsed JSON in, a UiSpec or a bounded SpecError out."""

import json
import math
import time
from collections.abc import Iterator
from decimal import Decimal
from typing import TYPE_CHECKING, ClassVar

import pytest

from tests.ui.spec.support import Json, build, golden, golden_text, minimal
from tumbleweed.ui.spec.errors import (
    MAX_PROBLEM_LENGTH,
    MAX_REPORTED_PROBLEMS,
    SpecError,
    SpecTooLargeError,
)
from tumbleweed.ui.spec.limits import MAX_SPEC_BYTES
from tumbleweed.ui.spec.parse import parse_ui_spec, validate_ui_spec
from tumbleweed.ui.spec.spec import UiSpec

if TYPE_CHECKING:
    from tumbleweed.core.types import JsonValue

MINIMAL_TEXT = json.dumps(minimal())


def padded(text: str, size: int) -> str:
    """`text` (JSON) followed by whitespace, so that it is exactly `size` bytes."""
    return text + " " * (size - len(text.encode()))


class TestParse:
    def test_parses_text_and_bytes_to_the_same_spec(self) -> None:
        from_text = parse_ui_spec(MINIMAL_TEXT)
        from_bytes = parse_ui_spec(MINIMAL_TEXT.encode())

        assert from_text == from_bytes
        assert isinstance(from_text, UiSpec)

    def test_every_golden_spec_parses_from_text_and_from_parsed_json(self) -> None:
        for name in ("tip_calculator", "photo_adjuster", "csv_viewer"):
            assert parse_ui_spec(golden_text(name)) == validate_ui_spec(golden(name))

    @pytest.mark.parametrize("bad", [None, 5, {"a": 1}, ["x"], bytearray(b"{}"), memoryview(b"{}")])
    def test_takes_exactly_str_or_bytes(self, bad: object) -> None:
        with pytest.raises(SpecError, match="expected str or bytes"):
            parse_ui_spec(bad)  # type: ignore[arg-type]  # the point of the test

    def test_a_bytes_subclass_is_not_bytes(self) -> None:
        class Lying(bytes):
            __slots__ = ()

            def decode(self, *args: object, **kwargs: object) -> str:
                return MINIMAL_TEXT

        with pytest.raises(SpecError, match="expected str or bytes"):
            parse_ui_spec(Lying(b"not json at all"))

    def test_a_str_subclass_is_not_a_str(self) -> None:
        class Lying(str):
            __slots__ = ()

            def encode(self, *args: object, **kwargs: object) -> bytes:
                return MINIMAL_TEXT.encode()

        with pytest.raises(SpecError, match="expected str or bytes"):
            parse_ui_spec(Lying("not json at all"))


class TestSize:
    def test_exactly_the_limit_is_accepted_as_text_and_as_bytes(self) -> None:
        text = padded(MINIMAL_TEXT, MAX_SPEC_BYTES)
        assert len(text.encode()) == MAX_SPEC_BYTES

        assert parse_ui_spec(text).title == "Test"
        assert parse_ui_spec(text.encode()).title == "Test"

    def test_one_byte_more_is_refused_before_parsing(self) -> None:
        text = padded(MINIMAL_TEXT, MAX_SPEC_BYTES + 1)

        with pytest.raises(SpecTooLargeError, match="over 262144 bytes"):
            parse_ui_spec(text)
        with pytest.raises(SpecTooLargeError, match="over 262144 bytes"):
            parse_ui_spec(text.encode())

    def test_the_limit_is_in_bytes_not_characters(self) -> None:
        # Each "é" is two bytes: the same number of characters is over the limit in bytes.
        data = minimal(title="é")
        text = padded(json.dumps(data, ensure_ascii=False), MAX_SPEC_BYTES)
        assert parse_ui_spec(text).title == "é"
        over = text + " "  # the same count of characters, one byte over

        assert len(text) == MAX_SPEC_BYTES - 1  # the é is two bytes
        assert len(over) == MAX_SPEC_BYTES
        assert len(over.encode()) == MAX_SPEC_BYTES + 1
        with pytest.raises(SpecTooLargeError):
            parse_ui_spec(over)

    def test_a_size_error_is_a_spec_error_with_a_short_message(self) -> None:
        with pytest.raises(SpecError) as raised:
            parse_ui_spec(b" " * (10 * MAX_SPEC_BYTES))

        assert len(str(raised.value)) < 100


class TestWhatTheTextMustBe:
    @pytest.mark.parametrize(
        "text",
        ["", "   ", "{", "{}}", "[]", "null", "5", '"x"', "{'a': 1}", "{,}", '{"a":1,}', "tru"],
    )
    def test_must_be_a_json_object_that_is_a_spec(self, text: str) -> None:
        with pytest.raises(SpecError):
            parse_ui_spec(text)

    def test_invalid_utf8_is_refused(self) -> None:
        with pytest.raises(SpecError, match="not valid UTF-8"):
            parse_ui_spec(b'{"title": "\xff"}')

    def test_a_byte_order_mark_is_refused(self) -> None:
        with pytest.raises(SpecError):
            parse_ui_spec(b"\xef\xbb\xbf" + MINIMAL_TEXT.encode())
        with pytest.raises(SpecError):
            parse_ui_spec("\ufeff" + MINIMAL_TEXT)

    def test_utf16_is_not_json_here(self) -> None:
        with pytest.raises(SpecError):
            parse_ui_spec(MINIMAL_TEXT.encode("utf-16"))

    def test_text_with_a_lone_surrogate_is_refused(self) -> None:
        with pytest.raises(SpecError, match="lone surrogate"):
            parse_ui_spec(MINIMAL_TEXT.replace("Test", "\ud800"))
        escaped = MINIMAL_TEXT.replace("Test", "\\ud800")
        with pytest.raises(SpecError):
            parse_ui_spec(escaped)

    @pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
    def test_json_has_no_nan_or_infinity(self, constant: str) -> None:
        text = (
            '{"version":"tumbleweed.ui/1","title":"T","root":{"kind":"box","spacing":'
            + constant
            + "}}"
        )

        with pytest.raises(SpecError, match="not valid JSON"):
            parse_ui_spec(text)

    def test_an_exponent_too_big_for_a_float_is_refused_by_the_models(self) -> None:
        entry: Json = {"kind": "float", "key": "x", "default": 0}
        text = json.dumps(build(state=[entry])).replace('"default": 0', '"default": 1e999')

        with pytest.raises(SpecError, match="finite"):
            parse_ui_spec(text)


class TestRepeatedKeys:
    """Parsers keep the last value of a repeated key and drop the rest without a word."""

    def test_a_repeated_top_level_key_is_refused(self) -> None:
        text = MINIMAL_TEXT[:-1] + ', "title": "Other"}'

        with pytest.raises(SpecError, match="an object repeats the key 'title'"):
            parse_ui_spec(text)

    def test_a_repeated_key_deep_inside_is_refused(self) -> None:
        text = (
            '{"version":"tumbleweed.ui/1","title":"T",'
            '"root":{"kind":"box","children":[{"kind":"label","text":"a","text":"b"}]}}'
        )

        with pytest.raises(SpecError, match="repeats the key 'text'"):
            parse_ui_spec(text)

    def test_a_repeated_key_in_an_expression_is_refused(self) -> None:
        text = (
            '{"version":"tumbleweed.ui/1","title":"T","state":[{"kind":"int","key":"x",'
            '"default":0,"computed":{"kind":"int","kind":"float","value":1}}],"root":{"kind":"box"}}'
        )

        with pytest.raises(SpecError, match="repeats the key 'kind'"):
            parse_ui_spec(text)

    def test_the_same_key_in_different_objects_is_fine(self) -> None:
        assert parse_ui_spec(golden_text("csv_viewer")).title == "CSV viewer"

    def test_the_message_does_not_echo_a_long_key(self) -> None:
        key = "k" * 10_000
        text = f'{{"{key}": 1, "{key}": 2}}'

        with pytest.raises(SpecError) as raised:
            parse_ui_spec(text)

        assert len(str(raised.value)) < 200


class TestHostileShapes:
    def test_deeply_nested_arrays_are_refused_without_a_crash(self) -> None:
        with pytest.raises(SpecError):
            parse_ui_spec("[" * 100_000 + "]" * 100_000)

    def test_deeply_nested_objects_are_refused_without_a_crash(self) -> None:
        with pytest.raises(SpecError):
            parse_ui_spec('{"a":' * 100_000 + "1" + "}" * 100_000)

    def test_a_deep_but_legal_json_nesting_is_refused_by_the_depth_limits_not_a_crash(self) -> None:
        node = '{"kind":"box","children":[' * 150 + '{"kind":"separator"}' + "]}" * 150
        text = '{"version":"tumbleweed.ui/1","title":"T","root":' + node + "}"

        with pytest.raises(SpecError):
            parse_ui_spec(text)

    def test_a_huge_string_is_refused_by_the_size_limit_first(self) -> None:
        with pytest.raises(SpecTooLargeError):
            parse_ui_spec('{"title": "' + "x" * (2 * MAX_SPEC_BYTES) + '"}')


class TestErrorsAreBounded:
    def test_many_problems_are_cut_to_a_report_of_bounded_size(self) -> None:
        fields: dict[str, JsonValue] = {f"unknown_{i}": 0 for i in range(2000)}
        text = json.dumps(build({"kind": "box", **fields}))

        with pytest.raises(SpecError) as raised:
            parse_ui_spec(text)

        problems = raised.value.problems
        assert len(problems) == MAX_REPORTED_PROBLEMS + 1
        assert problems[-1] == "... and more problems"
        assert all(len(problem) <= MAX_PROBLEM_LENGTH for problem in problems)
        assert len(str(raised.value)) < (MAX_REPORTED_PROBLEMS + 1) * (MAX_PROBLEM_LENGTH + 2)

    def test_a_problem_names_where_and_what_and_quotes_only_a_short_excerpt(self) -> None:
        secret = "sk-ant-api03-" + "s3cr3t" * 200
        data = minimal(root={"kind": "label", "text": 5, "bind": secret})

        with pytest.raises(SpecError) as raised:
            parse_ui_spec(json.dumps(data))

        message = str(raised.value)
        assert "root.label.text" in message or "root" in message
        assert secret[:100] not in message  # at most the first 64 characters are quoted
        assert "s3cr3t" * 20 not in message

    def test_an_excerpt_of_a_bad_value_is_short(self) -> None:
        data = minimal(title="a\u202e" + "x" * 5000)

        with pytest.raises(SpecError) as raised:
            parse_ui_spec(json.dumps(data))

        assert len(str(raised.value)) < 400

    def test_a_long_message_is_cut_with_an_ellipsis(self) -> None:
        error = SpecError(["x" * 5000])

        assert len(error.problems[0]) == MAX_PROBLEM_LENGTH
        assert error.problems[0].endswith("...")

    def test_a_cut_report_says_there_is_more_and_exactly_at_the_limit_it_does_not(self) -> None:
        over = SpecError([f"p{i}" for i in range(MAX_REPORTED_PROBLEMS + 1)])
        exactly = SpecError([f"p{i}" for i in range(MAX_REPORTED_PROBLEMS)])

        assert len(over.problems) == MAX_REPORTED_PROBLEMS + 1
        assert over.problems[-1] == "... and more problems"
        assert over.problems[:-1] == tuple(f"p{i}" for i in range(MAX_REPORTED_PROBLEMS))
        assert exactly.problems == tuple(f"p{i}" for i in range(MAX_REPORTED_PROBLEMS))

    def test_few_problems_are_all_shown_with_no_count(self) -> None:
        error = SpecError(["a", "b"])

        assert error.problems == ("a", "b")
        assert str(error) == "a; b"

    def test_a_spec_error_is_a_tumbleweed_error(self) -> None:
        from tumbleweed.core.errors import TumbleweedError

        assert issubclass(SpecError, TumbleweedError)
        assert issubclass(SpecTooLargeError, SpecError)


def loose(**fields: object) -> dict[object, object]:
    """A minimal spec as a dict that may hold things JSON cannot (the point of some tests)."""
    data: dict[object, object] = dict(minimal().items())
    data.update(fields)
    return data


def base36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while True:
        number, digit = divmod(number, 36)
        result = digits[digit] + result
        if number == 0:
            return result


class TestRepeatedKeysAreFoundInOnePass:
    """The check is linear: finding which key repeats must not be quadratic."""

    def test_a_big_object_whose_last_key_repeats_is_refused_fast(self) -> None:
        keys = [base36(i) for i in range(30_000)]
        members = ",".join(f'"{key}":0' for key in keys)
        text = "{" + members + f',"{keys[-1]}":0' + "}"
        assert len(text.encode()) <= MAX_SPEC_BYTES  # it gets past the size limit to the check

        started = time.perf_counter()
        with pytest.raises(SpecError, match="repeats the key") as raised:
            parse_ui_spec(text)
        elapsed = time.perf_counter() - started

        assert keys[-1] in str(raised.value)
        # The quadratic search for the repeated key took 6 to 9 seconds on this input.
        assert elapsed < 1.0

    def test_the_first_repeat_is_the_one_named(self) -> None:
        # Scanning left to right, `c` repeats (the fourth member) before `b` does (the fifth).
        with pytest.raises(SpecError, match="repeats the key 'c'"):
            parse_ui_spec('{"a": 1, "b": 2, "c": 3, "c": 4, "b": 5}')

    def test_distinct_keys_of_any_number_are_fine(self) -> None:
        keys = [base36(i) for i in range(30_000)]
        text = "{" + ",".join(f'"{key}":0' for key in keys) + "}"

        with pytest.raises(SpecError) as raised:  # not a spec, but not a repeated key either
            parse_ui_spec(text)

        assert "repeats the key" not in str(raised.value)


def spec_dict_of_size(size: int) -> Json:
    """A valid spec as parsed JSON whose JSON text is exactly `size` bytes."""

    def entry(key: str, length: int) -> Json:
        return {"kind": "string", "key": key, "default": "x" * length, "max_length": 65_536}

    state = [entry("a", 65_536), entry("b", 65_536), entry("c", 65_536), entry("d", 1)]
    data = build(state=state)
    missing = size - len(json.dumps(data, ensure_ascii=False).encode())
    assert 0 < missing + 1 <= 65_536
    state[3] = entry("d", 1 + missing)
    return build(state=state)


class TestParsedJsonTakesTheSameRoad:
    """`validate_ui_spec` writes the data out and parses it: one set of checks, one size limit."""

    def test_the_helper_builds_what_it_says(self) -> None:
        for size in (MAX_SPEC_BYTES, MAX_SPEC_BYTES + 1):
            assert len(json.dumps(spec_dict_of_size(size), ensure_ascii=False).encode()) == size

    def test_a_spec_of_exactly_the_limit_is_accepted(self) -> None:
        data = spec_dict_of_size(MAX_SPEC_BYTES)

        assert validate_ui_spec(data) == parse_ui_spec(json.dumps(data, ensure_ascii=False))

    def test_a_spec_one_byte_over_the_limit_is_refused(self) -> None:
        with pytest.raises(SpecTooLargeError, match="over 262144 bytes"):
            validate_ui_spec(spec_dict_of_size(MAX_SPEC_BYTES + 1))

    def test_a_twelve_megabyte_spec_is_refused(self) -> None:
        chunks: list[JsonValue] = ["x" * 60_000] * 200
        big = minimal(state=[{"kind": "json", "key": "k", "default": chunks}])

        with pytest.raises(SpecTooLargeError):
            validate_ui_spec(big)

    def test_writing_it_out_stops_at_the_limit_and_does_not_walk_the_rest(self) -> None:
        visited = 0

        class Endless(list[int]):
            def __iter__(self) -> Iterator[int]:
                nonlocal visited
                for _ in range(5_000_000):
                    visited += 1
                    yield 0

        with pytest.raises(SpecTooLargeError):
            validate_ui_spec(loose(state=Endless([0])))

        assert visited < 150_000  # 256 KiB of `0, ` and not five million

    def test_the_limit_counts_bytes_like_parse_ui_spec_does(self) -> None:
        # Written out as UTF-8, a character can be up to four bytes: the text is over the limit.
        data = {**minimal(), "title": "\U0001f600" * 80}
        data["state"] = [
            {"kind": "string", "key": "k", "default": "\U0001f600" * 1000, "max_length": 1000}
            for _ in range(70)
        ]

        with pytest.raises(SpecTooLargeError):
            validate_ui_spec(data)

    BAD_SPECS: ClassVar[dict[str, Json]] = {
        "an unknown field": minimal(extra=1),
        "a wrong type": minimal(title=5),
        "an unknown kind": build({"kind": "nope"}),
        "a bad binding": build({"kind": "label", "bind": "missing"}),
        "an int where a float is not exact": build(
            state=[{"kind": "float", "key": "x", "default": 2**53 + 1}]
        ),
        "a lone surrogate": minimal(title="a\ud800b"),
        "a control character": minimal(title="a\x1bb"),
    }

    @pytest.mark.parametrize("data", BAD_SPECS.values(), ids=BAD_SPECS)
    def test_what_parse_refuses_validate_refuses_with_the_same_problems(self, data: Json) -> None:
        with pytest.raises(SpecError) as parsed:
            parse_ui_spec(json.dumps(data, ensure_ascii=False))
        with pytest.raises(SpecError) as validated:
            validate_ui_spec(data)

        assert validated.value.problems == parsed.value.problems

    def test_what_parse_accepts_validate_accepts_to_the_same_spec(self) -> None:
        for name in ("tip_calculator", "photo_adjuster", "csv_viewer"):
            data = golden(name)

            assert validate_ui_spec(data) == parse_ui_spec(golden_text(name))

    NOT_JSON: ClassVar[dict[str, dict[object, object]]] = {
        "bytes": loose(title=b"t"),
        "a set": loose(state={1, 2}),
        "a model instance": loose(root=parse_ui_spec(MINIMAL_TEXT)),
        "NaN": loose(state=[{"kind": "float", "key": "x", "default": math.nan}]),
        "infinity": loose(state=[{"kind": "float", "key": "x", "default": math.inf}]),
        "a Decimal": loose(title=Decimal("1.5")),
        "a huge int": loose(title=10**5000),
        "a tuple key": {**loose(), (1, 2): 3},
    }

    @pytest.mark.parametrize("data", NOT_JSON.values(), ids=NOT_JSON)
    def test_data_that_is_not_json_is_a_spec_error(self, data: object) -> None:
        with pytest.raises(SpecError, match="not JSON data"):
            validate_ui_spec(data)

    def test_a_cycle_and_a_very_deep_structure_are_spec_errors(self) -> None:
        cyclic: dict[str, object] = {}
        cyclic["root"] = cyclic
        deep: object = []
        for _ in range(100_000):
            deep = [deep]

        with pytest.raises(SpecError, match="not JSON data"):
            validate_ui_spec(cyclic)
        with pytest.raises(SpecError, match="nested too deeply"):
            validate_ui_spec({**minimal(), "state": deep})

    def test_an_int_key_that_collides_with_a_str_key_is_a_repeated_key(self) -> None:
        with pytest.raises(SpecError, match="repeats the key '1'"):
            validate_ui_spec({1: "a", "1": "b"})

    def test_a_str_subclass_is_written_as_the_text_it_holds(self) -> None:
        class Lying(str):
            __slots__ = ()

            def __str__(self) -> str:
                return "lies"

        spec = validate_ui_spec({**minimal(), "title": Lying("truth")})

        assert spec.title == "truth"


class TestValidateParsedJson:
    def test_accepts_dicts_and_lists_as_parsed_from_json(self) -> None:
        spec = validate_ui_spec(golden("tip_calculator"))

        assert spec.title == "Tip calculator"
        assert isinstance(spec.state, tuple)

    @pytest.mark.parametrize("bad", [None, [], "x", 5, ()])
    def test_refuses_what_is_not_an_object(self, bad: object) -> None:
        with pytest.raises(SpecError):
            validate_ui_spec(bad)

    def test_python_mode_is_as_strict_as_json(self) -> None:
        entry: Json = {"kind": "int", "key": "x", "default": 5.0}

        with pytest.raises(SpecError):
            validate_ui_spec(build(state=[entry]))
        with pytest.raises(SpecError):
            validate_ui_spec({**minimal(), "title": 5})

    def test_a_cyclic_object_is_refused_not_followed(self) -> None:
        cyclic: dict[str, object] = {"version": "tumbleweed.ui/1", "title": "T"}
        cyclic["root"] = cyclic

        with pytest.raises(SpecError):
            validate_ui_spec(cyclic)

    def test_absurd_python_values_are_refused_with_a_short_message(self) -> None:
        for data in (
            {"version": 10**5000},
            {
                "version": "tumbleweed.ui/1",
                "title": "T",
                "root": {"kind": "box", "spacing": 10**5000},
            },
            {"version": "x" * 1_000_000},
        ):
            with pytest.raises(SpecError) as raised:
                validate_ui_spec(data)
            assert len(str(raised.value)) < 4000

    def test_a_validation_error_never_escapes(self) -> None:
        from pydantic import ValidationError

        for data in (None, {}, {"version": 1}, build(root={"kind": "nope"})):
            try:
                validate_ui_spec(data)
            except SpecError:
                continue
            except ValidationError as error:  # pragma: no cover - the failure being tested for
                raise AssertionError("a pydantic error escaped") from error
