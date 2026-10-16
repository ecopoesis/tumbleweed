"""Authored text, data text, safe integers and finite floats."""

import math

import pytest
from pydantic import ConfigDict, TypeAdapter, ValidationError

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.ui.spec import text
from tumbleweed.ui.spec.limits import (
    MAX_CELL_LENGTH,
    MAX_LINE_LENGTH,
    MAX_OPTION_LENGTH,
    MAX_SAFE_INT,
    MAX_STRING_LENGTH,
    MAX_TEXT_LENGTH,
    MAX_TITLE_LENGTH,
)

STRICT = ConfigDict(strict=True)

TITLE = TypeAdapter(text.Title, config=STRICT)
LINE = TypeAdapter(text.Line, config=STRICT)
BLOCK = TypeAdapter(text.Block, config=STRICT)
OPTION = TypeAdapter(text.Option, config=STRICT)
CELL = TypeAdapter(text.Cell, config=STRICT)
COLUMN_NAME = TypeAdapter(text.ColumnName, config=STRICT)
DATA_STRING = TypeAdapter(text.DataString, config=STRICT)
SAFE_INT = TypeAdapter(text.SafeInt, config=STRICT)
POSITIVE_SAFE_INT = TypeAdapter(text.PositiveSafeInt, config=STRICT)
FINITE_FLOAT = TypeAdapter(text.FiniteFloat, config=STRICT)
POSITIVE_FINITE_FLOAT = TypeAdapter(text.PositiveFiniteFloat, config=STRICT)

LINE_TYPES = {"Title": TITLE, "Line": LINE, "Option": OPTION}
AUTHORED_TYPES = {**LINE_TYPES, "Block": BLOCK}
DATA_TYPES = {"Cell": CELL, "DataString": DATA_STRING, "ColumnName": COLUMN_NAME}

# What no authored text may hold, and why. A lone surrogate is here too: it is refused by
# pydantic itself before our validator sees it, so it is tested separately below.
FORBIDDEN_IN_AUTHORED = {
    "NUL": "\0",
    "ESC": "\x1b",
    "C1 control CSI": "\x9b",
    "NEL": "\x85",
    "DEL": "\x7f",
    "line separator": "\u2028",
    "paragraph separator": "\u2029",
    "right-to-left override": "\u202e",
    "bidi isolate": "\u2066",
    "zero-width space": "\u200b",
    "byte-order mark": "\ufeff",
    "soft hyphen": "\xad",
    "tag character": "\U000e0041",
}
# Real text that must stay possible: zero-width joiner and non-joiner, emoji, scripts.
ALLOWED_IN_AUTHORED = [
    "plain",
    "naïve café",
    "日本語のラベル",
    "مرحبا بالعالم",
    "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",  # Persian, with a zero-width non-joiner
    "👨\u200d👩\u200d👧",  # a family, with zero-width joiners
    " leading and trailing ",
]
LONE_SURROGATES = {"high": "\ud800", "low": "\udfff", "inside": "x\udc80y"}


class LyingStr(str):
    __slots__ = ()


@pytest.mark.parametrize("check", AUTHORED_TYPES.values(), ids=AUTHORED_TYPES)
class TestAuthoredText:
    @pytest.mark.parametrize("value", ALLOWED_IN_AUTHORED)
    def test_accepts_real_text(self, check: TypeAdapter[str], value: str) -> None:
        assert check.validate_python(value) == value

    @pytest.mark.parametrize("value", FORBIDDEN_IN_AUTHORED.values(), ids=FORBIDDEN_IN_AUTHORED)
    def test_refuses_what_makes_text_look_like_something_else(
        self, check: TypeAdapter[str], value: str
    ) -> None:
        with pytest.raises(ValidationError, match="must not contain"):
            check.validate_python(f"a{value}b")

    @pytest.mark.parametrize("value", LONE_SURROGATES.values(), ids=LONE_SURROGATES)
    def test_refuses_a_lone_surrogate_which_cannot_be_written_as_json(
        self, check: TypeAdapter[str], value: str
    ) -> None:
        with pytest.raises(ValidationError):
            check.validate_python(value)

    @pytest.mark.parametrize("bad", [None, 5, b"x", ["x"]])
    def test_takes_a_str(self, check: TypeAdapter[str], bad: object) -> None:
        with pytest.raises(ValidationError):
            check.validate_python(bad)

    def test_a_str_subclass_is_replaced_by_the_str_it_holds(self, check: TypeAdapter[str]) -> None:
        # The validators see, and the value keeps, the real text, not what a subclass claims.
        result = check.validate_python(LyingStr("real text"))

        assert type(result) is str
        assert result == "real text"


@pytest.mark.parametrize("check", LINE_TYPES.values(), ids=LINE_TYPES)
def test_a_line_has_no_newline_tab_or_carriage_return(check: TypeAdapter[str]) -> None:
    for control in ("\n", "\t", "\r"):
        with pytest.raises(ValidationError, match="must not contain"):
            check.validate_python(f"a{control}b")


def test_a_block_may_have_newlines_and_tabs_but_not_carriage_returns() -> None:
    assert BLOCK.validate_python("a\nb\tc") == "a\nb\tc"
    with pytest.raises(ValidationError, match="must not contain"):
        BLOCK.validate_python("a\r\nb")


def test_the_validators_themselves_refuse_a_lone_surrogate() -> None:
    # Pydantic stops one first when the field has a length bound; the functions stay safe when
    # they are called without that.
    with pytest.raises(InvalidValueError, match="control, format, separator or surrogate"):
        text.check_line("a\ud800")
    with pytest.raises(InvalidValueError, match="NUL or a lone surrogate"):
        text.check_data_text("a\ud800")


@pytest.mark.parametrize(
    ("check", "limit", "minimum"),
    [
        (TITLE, MAX_TITLE_LENGTH, 1),
        (LINE, MAX_LINE_LENGTH, 0),
        (BLOCK, MAX_TEXT_LENGTH, 0),
        (OPTION, MAX_OPTION_LENGTH, 1),
        (CELL, MAX_CELL_LENGTH, 0),
        (COLUMN_NAME, MAX_CELL_LENGTH, 1),
        (DATA_STRING, MAX_STRING_LENGTH, 0),
    ],
    ids=["Title", "Line", "Block", "Option", "Cell", "ColumnName", "DataString"],
)
def test_lengths_are_counted_in_characters_and_bounded(
    check: TypeAdapter[str], limit: int, minimum: int
) -> None:
    assert check.validate_python("é" * limit) == "é" * limit  # characters, not bytes
    with pytest.raises(ValidationError, match="at most"):
        check.validate_python("é" * (limit + 1))
    if minimum:
        with pytest.raises(ValidationError, match="at least"):
            check.validate_python("")
    else:
        assert check.validate_python("") == ""


@pytest.mark.parametrize("check", DATA_TYPES.values(), ids=DATA_TYPES)
class TestDataText:
    @pytest.mark.parametrize(
        "value",
        [
            "\x1b[31mred",
            "a\u202eb",
            "\ufeffcolumn",  # a CSV's byte-order mark
            "line\r\nbreak",
            "tab\there",
            "\u2028",
        ],
        ids=["escape", "bidi override", "BOM", "CRLF", "tab", "line separator"],
    )
    def test_carries_whatever_a_file_or_a_user_holds(
        self, check: TypeAdapter[str], value: str
    ) -> None:
        assert check.validate_python(value) == value

    def test_refuses_nul(self, check: TypeAdapter[str]) -> None:
        with pytest.raises(ValidationError, match="NUL or a lone surrogate"):
            check.validate_python("a\0b")

    @pytest.mark.parametrize("value", LONE_SURROGATES.values(), ids=LONE_SURROGATES)
    def test_refuses_a_lone_surrogate(self, check: TypeAdapter[str], value: str) -> None:
        with pytest.raises(ValidationError):
            check.validate_python(value)


class TestSafeInt:
    @pytest.mark.parametrize("value", [0, 1, -1, MAX_SAFE_INT, -MAX_SAFE_INT])
    def test_accepts_integers_exact_in_canonical_json(self, value: int) -> None:
        assert SAFE_INT.validate_python(value) == value

    @pytest.mark.parametrize("value", [MAX_SAFE_INT + 1, -MAX_SAFE_INT - 1, 2**63, 10**30])
    def test_refuses_integers_a_double_cannot_hold(self, value: int) -> None:
        with pytest.raises(ValidationError):
            SAFE_INT.validate_python(value)

    @pytest.mark.parametrize("bad", [5.0, "5", True, False, None, b"5"])
    def test_refuses_what_is_not_an_int(self, bad: object) -> None:
        with pytest.raises(ValidationError):
            SAFE_INT.validate_python(bad)

    def test_json_5_point_0_is_not_an_int(self) -> None:
        with pytest.raises(ValidationError):
            SAFE_INT.validate_json("5.0")

    def test_positive_refuses_zero(self) -> None:
        with pytest.raises(ValidationError):
            POSITIVE_SAFE_INT.validate_python(0)
        assert POSITIVE_SAFE_INT.validate_python(1) == 1


class TestFiniteFloat:
    @pytest.mark.parametrize("value", [0.0, -0.5, 1e300, 5])
    def test_accepts_finite_numbers_and_ints(self, value: float) -> None:
        assert FINITE_FLOAT.validate_python(value) == value

    def test_an_int_becomes_a_float(self) -> None:
        assert type(FINITE_FLOAT.validate_python(5)) is float

    @pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
    def test_refuses_non_finite_python_floats(self, value: float) -> None:
        with pytest.raises(ValidationError, match="finite"):
            FINITE_FLOAT.validate_python(value)

    @pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e999"])
    def test_refuses_non_finite_json(self, raw: str) -> None:
        with pytest.raises(ValidationError):
            FINITE_FLOAT.validate_json(raw)

    @pytest.mark.parametrize("bad", ["5", True, None])
    def test_refuses_what_is_not_a_number(self, bad: object) -> None:
        with pytest.raises(ValidationError):
            FINITE_FLOAT.validate_python(bad)

    @pytest.mark.parametrize("value", [0.0, -1.0])
    def test_positive_refuses_zero_and_negative(self, value: float) -> None:
        with pytest.raises(ValidationError):
            POSITIVE_FINITE_FLOAT.validate_python(value)


class TestAsTuple:
    def test_converts_exactly_a_list(self) -> None:
        assert text.as_tuple([1, 2]) == (1, 2)
        assert text.as_tuple((1, 2)) == (1, 2)

    def test_leaves_everything_else_for_the_validator_to_refuse(self) -> None:
        class Sub(list[int]):
            pass

        subclass = Sub([1])
        for value in ({1}, "ab", None, subclass):
            assert text.as_tuple(value) is value
