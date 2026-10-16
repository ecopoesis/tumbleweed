"""Authored text, data text, safe integers and finite floats."""

import math
import time
import unicodedata

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
DATA_TYPES = {"DataString": DATA_STRING, "ColumnName": COLUMN_NAME}

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
        (COLUMN_NAME, MAX_CELL_LENGTH, 1),
        (DATA_STRING, MAX_STRING_LENGTH, 0),
    ],
    ids=["Title", "Line", "Block", "Option", "ColumnName", "DataString"],
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


# --- one rule for every number (RFC 8785) --------------------------------------------------

# Integers that a double holds exactly, and some that it does not (above 2**53 the doubles are
# the multiples of 2, then 4, ...).
EXACT_INTS = [0, 1, -1, 2**53 - 1, 2**53, -(2**53), 2**53 + 2, 10**16, 10**20, 2**100, 10**22]
INEXACT_INTS = [2**53 + 1, -(2**53) - 1, 2**53 + 3, 10**16 + 1, 10**23 + 1, 3 * 2**100 + 1]


class TestExactDouble:
    @pytest.mark.parametrize("value", EXACT_INTS, ids=lambda v: str(v)[:12])
    def test_an_int_a_double_holds_comes_back_as_that_float(self, value: int) -> None:
        number = text.exact_double(value)

        assert type(number) is float
        assert int(number) == value

    @pytest.mark.parametrize("value", INEXACT_INTS, ids=lambda v: str(v)[:12])
    def test_an_int_no_double_equals_is_refused_not_rounded(self, value: int) -> None:
        with pytest.raises(InvalidValueError, match="no double is equal"):
            text.exact_double(value)

    @pytest.mark.parametrize(
        "value",
        [pytest.param(10**400, id="10**400"), pytest.param(-(10**5000), id="-10**5000")],
    )
    def test_an_int_too_large_for_any_double_is_refused_with_a_short_message(
        self, value: int
    ) -> None:
        with pytest.raises(InvalidValueError, match="too large for a double") as raised:
            text.exact_double(value)

        assert len(str(raised.value)) < 100


class TestFloatPositionsTakeExactIntegers:
    """`FiniteFloat`: an integer is a float if some double equals it, in any size."""

    @pytest.mark.parametrize("value", EXACT_INTS, ids=lambda v: str(v)[:12])
    def test_accepts_and_stores_a_float(self, value: int) -> None:
        number = FINITE_FLOAT.validate_python(value)

        assert type(number) is float
        assert int(number) == value
        assert FINITE_FLOAT.validate_json(str(value)) == number

    @pytest.mark.parametrize("value", INEXACT_INTS, ids=lambda v: str(v)[:12])
    def test_refuses_what_it_would_have_to_round(self, value: int) -> None:
        with pytest.raises(ValidationError, match="no double is equal"):
            FINITE_FLOAT.validate_python(value)
        with pytest.raises(ValidationError, match="no double is equal"):
            FINITE_FLOAT.validate_json(str(value))

    def test_a_bool_is_not_a_number(self) -> None:
        for bad in (True, False):
            with pytest.raises(ValidationError):
                FINITE_FLOAT.validate_python(bad)

    def test_the_positive_variant_applies_the_same_rule(self) -> None:
        assert POSITIVE_FINITE_FLOAT.validate_python(10**20) == 1e20
        with pytest.raises(ValidationError, match="no double is equal"):
            POSITIVE_FINITE_FLOAT.validate_python(2**53 + 1)
        with pytest.raises(ValidationError, match="greater than 0"):
            POSITIVE_FINITE_FLOAT.validate_python(0)


class TestIntPositionsStayWithinSafeIntegers:
    @pytest.mark.parametrize("value", [2**53, 10**16, 2**60])
    def test_an_int_slot_refuses_what_a_float_slot_takes(self, value: int) -> None:
        assert FINITE_FLOAT.validate_python(value) == float(value)
        with pytest.raises(ValidationError):
            SAFE_INT.validate_python(value)


class TestScalarCell:
    """A table cell or list item: one validator, one message."""

    @pytest.mark.parametrize("value", [True, False, 0, -5, 2**53, 10**20, 1.5, -0.0, "", "text"])
    def test_accepts_a_bool_a_number_or_a_string(self, value: object) -> None:
        assert CELL.validate_python(value) == value
        assert type(CELL.validate_python(value)) is type(value)

    @pytest.mark.parametrize(
        "value",
        [
            None,
            [],
            {},
            (1,),
            b"x",
            object(),
            float("nan"),
            float("inf"),
            2**53 + 1,
            "a\0b",
            "\ud800",
        ],
        ids=lambda v: type(v).__name__ + ":" + repr(v)[:10],
    )
    def test_refuses_everything_else_with_one_error(self, value: object) -> None:
        with pytest.raises(ValidationError) as raised:
            CELL.validate_python(value)

        assert raised.value.error_count() == 1

    def test_the_message_says_what_is_accepted(self) -> None:
        with pytest.raises(ValidationError, match="expected a string, a number or a bool"):
            CELL.validate_python([])

    def test_a_string_is_at_most_a_thousand_characters(self) -> None:
        assert CELL.validate_python("x" * MAX_CELL_LENGTH)
        with pytest.raises(ValidationError, match="longer than 1000 characters"):
            CELL.validate_python("x" * (MAX_CELL_LENGTH + 1))

    def test_a_subclass_of_a_scalar_is_not_one(self) -> None:
        class Str(str):
            __slots__ = ()

        class Int(int):
            __slots__ = ()

        class Float(float):
            __slots__ = ()

        for impostor in (Str("x"), Int(1), Float(1.5)):
            with pytest.raises(ValidationError):
                CELL.validate_python(impostor)

    def test_the_json_schema_names_the_three_types(self) -> None:
        schema = CELL.json_schema()

        assert schema["anyOf"] == [
            {"type": "boolean"},
            {"type": "number"},
            {"type": "string", "maxLength": MAX_CELL_LENGTH},
        ]


# --- every format and control character, exhaustively -----------------------------------


def code_points(*categories: str) -> list[str]:
    return [
        char
        for number in range(0x110000)
        if unicodedata.category(char := chr(number)) in categories
    ]


FORMAT_CHARACTERS = code_points("Cf")
CONTROLS = code_points("Cc")
SEPARATORS = code_points("Zl", "Zp")
SURROGATES = code_points("Cs")


class TestEveryForbiddenCharacter:
    """Not a list of examples: every code point of each category, from the Unicode database."""

    def test_the_categories_are_what_they_were_taken_to_be(self) -> None:
        assert len(FORMAT_CHARACTERS) > 100
        assert len(CONTROLS) == 65
        assert SEPARATORS == ["\u2028", "\u2029"]
        assert len(SURROGATES) == 2048

    def test_only_the_two_joiners_of_the_format_characters_are_allowed_in_authored_text(
        self,
    ) -> None:
        accepted = [char for char in FORMAT_CHARACTERS if _accepts(LINE, char)]

        assert accepted == ["\u200c", "\u200d"]
        assert accepted == [char for char in FORMAT_CHARACTERS if _accepts(BLOCK, char)]

    def test_no_control_is_allowed_in_a_line_and_only_newline_and_tab_in_a_block(self) -> None:
        assert not [char for char in CONTROLS if _accepts(LINE, char)]
        assert [char for char in CONTROLS if _accepts(BLOCK, char)] == ["\t", "\n"]

    def test_no_separator_or_surrogate_is_allowed_in_authored_text(self) -> None:
        for adapter in (LINE, BLOCK, TITLE, OPTION):
            assert not [char for char in SEPARATORS + SURROGATES if _accepts(adapter, char)]

    def test_the_same_holds_in_the_middle_of_a_word(self) -> None:
        for char in FORMAT_CHARACTERS:
            assert _accepts(LINE, f"a{char}b") == (char in {"\u200c", "\u200d"})

    def test_data_text_refuses_exactly_nul_and_the_surrogates(self) -> None:
        refused = [
            char
            for category in (FORMAT_CHARACTERS, CONTROLS, SEPARATORS, SURROGATES)
            for char in category
            if not _accepts(DATA_STRING, char)
        ]

        assert refused == ["\x00", *SURROGATES]


def _accepts(adapter: TypeAdapter[str], text: str) -> bool:
    try:
        adapter.validate_python(text)
    except ValidationError:
        return False
    return True


# --- text helpers --------------------------------------------------------------------------


class TestTextBytes:
    @pytest.mark.parametrize(
        ("value", "size"),
        [("", 0), ("abc", 3), ("\u00e9", 2), ("\u20ac", 3), ("\U0001f600", 4), ("\ud800", 3)],
    )
    def test_is_the_length_in_utf8(self, value: str, size: int) -> None:
        assert text.text_bytes(value) == size


class TestCanonicalText:
    def test_is_nfc_with_line_feeds(self) -> None:
        assert text.canonical_text("e\u0301") == "\u00e9"
        assert text.canonical_text("a\r\nb\rc\nd") == "a\nb\nc\nd"

    def test_is_idempotent_and_leaves_plain_text_alone(self) -> None:
        for value in ("plain", "", "e\u0301\r\n", "\u00e9\n"):
            once = text.canonical_text(value)
            assert text.canonical_text(once) == once
        assert text.canonical_text("plain") == "plain"


class TestDataTextIsFast:
    def test_a_long_string_is_checked_in_c(self) -> None:
        started = time.perf_counter()
        for _ in range(200):
            text.check_data_text("x" * 65_536)
            text.check_data_text("\u00e9" * 65_536)
        elapsed = time.perf_counter() - started

        # 26 million characters. A Python loop over them takes tens of seconds.
        assert elapsed < 2.0
