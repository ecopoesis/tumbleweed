"""`tumbleweed.core.jcs` against RFC 8785, and its refusals.

The tool store (S23) hashes what this writes, and the UI spec's number rules are proved against
it (`tests/ui/spec/test_numbers.py`), so it is itself checked against the RFC's own vectors.
"""

import json
import math
import struct
import sys

import pytest

from tumbleweed.core.jcs import canonicalize, double_of_int, es_number


def double_from_bits(bits: int) -> float:
    """The double with this IEEE-754 bit pattern (the way RFC 8785 appendix B lists them)."""
    (value,) = struct.unpack(">d", struct.pack(">Q", bits))
    assert isinstance(value, float)
    return value


# RFC 8785 appendix B: IEEE-754 bit pattern, and the text JCS writes.
RFC_8785_NUMBERS = [
    (0x0000000000000000, "0"),
    (0x8000000000000000, "0"),
    (0x0000000000000001, "5e-324"),
    (0x8000000000000001, "-5e-324"),
    (0x7FEFFFFFFFFFFFFF, "1.7976931348623157e+308"),
    (0xFFEFFFFFFFFFFFFF, "-1.7976931348623157e+308"),
    (0x4340000000000000, "9007199254740992"),
    (0xC340000000000000, "-9007199254740992"),
    (0x4430000000000000, "295147905179352830000"),
    (0x44B52D02C7E14AF5, "9.999999999999997e+22"),
    (0x44B52D02C7E14AF6, "1e+23"),
    (0x44B52D02C7E14AF7, "1.0000000000000001e+23"),
    (0x444B1AE4D6E2EF4E, "999999999999999700000"),
    (0x444B1AE4D6E2EF4F, "999999999999999900000"),
    (0x444B1AE4D6E2EF50, "1e+21"),
    (0x3EB0C6F7A0B5ED8C, "9.999999999999997e-7"),
    (0x3EB0C6F7A0B5ED8D, "0.000001"),
]


class TestEsNumber:
    @pytest.mark.parametrize(("bits", "text"), RFC_8785_NUMBERS, ids=lambda v: str(v)[:22])
    def test_numbers_as_in_rfc_8785_appendix_b(self, bits: int, text: str) -> None:
        assert es_number(double_from_bits(bits)) == text

    def test_some_more_numbers(self) -> None:
        assert es_number(1e16) == "10000000000000000"
        assert es_number(1e20) == "100000000000000000000"
        assert es_number(1e21) == "1e+21"
        assert es_number(5.0) == "5"
        assert es_number(0.5) == "0.5"
        assert es_number(123.456) == "123.456"
        assert es_number(1e-6) == "0.000001"
        assert es_number(1e-7) == "1e-7"
        assert es_number(-1.5e-10) == "-1.5e-10"
        assert es_number(float(2**60)) == "1152921504606847000"
        assert es_number(float(2**68)) == "295147905179352830000"

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_json_has_no_nan_or_infinity(self, bad: float) -> None:
        with pytest.raises(ValueError, match="no NaN or infinity"):
            es_number(bad)


class TestDoubleOfInt:
    """An integer stands for a double if it is that double, or how JCS spells the nearest."""

    @pytest.mark.parametrize("value", [0, 1, -1, 2**53 - 1, 2**53, -(2**53), 2**53 + 2, 2**100])
    def test_an_integer_that_is_exactly_a_double(self, value: int) -> None:
        assert double_of_int(value) == value
        assert type(double_of_int(value)) is float

    @pytest.mark.parametrize(
        ("spelling", "double"),
        [
            (1152921504606847000, float(2**60)),
            (295147905179352830000, float(2**68)),
            (-1152921504606847000, -float(2**60)),
            (123456789012345680000, 1.2345678901234568e20),
            (999999999999999700000, 999999999999999700000.0),
        ],
    )
    def test_an_integer_that_is_how_jcs_spells_a_double(self, spelling: int, double: float) -> None:
        assert double_of_int(spelling) == double
        assert int(double) != spelling  # not exact: only the spelling of the double
        assert es_number(double) == str(spelling)

    @pytest.mark.parametrize(
        "value",
        [
            2**53 + 1,
            -(2**53) - 1,
            2**53 + 3,
            2**60 + 1,
            10**16 + 1,
            1152921504606847001,
            10**21 - 1,
            10**23 + 1,
            3 * 2**100 + 1,
            10**400,
            -(10**5000),
        ],
        ids=lambda v: f"{v.bit_length()}-bits",
    )
    def test_any_other_integer_stands_for_none(self, value: int) -> None:
        assert double_of_int(value) is None


class TestCanonicalize:
    def test_the_serialization_example_of_rfc_8785_section_3_2_2(self) -> None:
        text = (
            '{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],'
            ' "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"/",'
            ' "literals": [null, true, false]}'
        )

        assert canonicalize(json.loads(text)) == (
            '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
            '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
        )

    def test_the_sorting_example_of_rfc_8785_section_3_2_3(self) -> None:
        data = {
            "€": "Euro Sign",
            "\r": "Carriage Return",
            "דּ": "Hebrew Letter Dalet With Dagesh",
            "1": "One",
            "\U0001f600": "Emoji: Grinning Face",
            "\u0080": "Control",
            "ö": "Latin Small Letter O With Diaeresis",
        }

        keys = list(json.loads(canonicalize(data)))

        assert keys == ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]

    def test_text_is_escaped_as_json_stringify_does(self) -> None:
        text = canonicalize('a"b\\c\b\f\n\r\t\x00\x1f\x7f/\u2028')

        assert text == '"a\\"b\\\\c\\b\\f\\n\\r\\t\\u0000\\u001f\x7f/\u2028"'

    def test_integers_are_written_as_the_doubles_they_stand_for(self) -> None:
        assert canonicalize([2**53, 2**60, 1152921504606847000, 295147905179352830000]) == (
            "[9007199254740992,1152921504606847000,1152921504606847000,295147905179352830000]"
        )
        assert canonicalize(5.0) == "5"
        assert canonicalize(1e21) == "1e+21"

    def test_an_integer_that_is_no_double_is_refused_not_rounded(self) -> None:
        for value in (2**53 + 1, 2**60 + 1, 10**400):
            with pytest.raises(ValueError, match="not an IEEE-754 double"):
                canonicalize(value)

    def test_literals_and_empty_containers(self) -> None:
        assert canonicalize([None, True, False, [], {}]) == "[null,true,false,[],{}]"

    @pytest.mark.parametrize(
        "bad",
        [
            math.nan,
            (1, 2),
            {1, 2},
            b"x",
            object(),
            {1: "a"},
            {None: "a"},
            {b"k": 1},
            "lone \ud800 surrogate",
            {"lone \udfff": 1},
        ],
        ids=repr,
    )
    def test_what_is_not_json_is_refused(self, bad: object) -> None:
        with pytest.raises(ValueError):  # noqa: PT011 -- every refusal is a ValueError
            canonicalize(bad)

    def test_subclasses_are_not_json_values(self) -> None:
        class Number(int):
            pass

        class Text(str):
            pass

        class Mapping(dict[str, int]):
            pass

        for bad in (Number(1), Text("x"), Mapping(a=1)):
            with pytest.raises(ValueError, match="not a JSON value"):
                canonicalize(bad)

    def test_nesting_is_bounded_so_a_hostile_value_cannot_exhaust_the_stack(self) -> None:
        deep: object = []
        for _ in range(100_000):
            deep = [deep]

        with pytest.raises(ValueError, match="nested deeper than 200"):
            canonicalize(deep)

    def test_a_cycle_is_refused_as_nesting(self) -> None:
        cycle: list[object] = []
        cycle.append(cycle)

        with pytest.raises(ValueError, match="nested deeper"):
            canonicalize(cycle)

    def test_the_deepest_value_allowed_is_written(self) -> None:
        value: object = 1
        for _ in range(199):
            value = [value]

        assert canonicalize(value) == "[" * 199 + "1" + "]" * 199

    def test_canonicalisation_is_idempotent_on_what_it_writes(self) -> None:
        data = {"b": [1.5, 2**60, "xé"], "a": {"z": None, "y": 1e21}}

        once = canonicalize(data)

        assert canonicalize(json.loads(once)) == once


class TestBudget:
    """#62: the walk is iterative and bounded, so a hostile value costs a bounded amount."""

    def test_the_walk_does_not_use_the_interpreters_stack(self) -> None:
        value: object = 1
        for _ in range(199):
            value = [value]
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(60)  # a recursive walk of 199 levels cannot run under this
        try:
            written = canonicalize(value)
        finally:
            sys.setrecursionlimit(limit)

        assert written == "[" * 199 + "1" + "]" * 199

    def test_a_deep_object_is_walked_without_recursion_too(self) -> None:
        value: object = None
        for _ in range(150):
            value = {"k": value}
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(60)
        try:
            written = canonicalize(value)
        finally:
            sys.setrecursionlimit(limit)

        assert written == '{"k":' * 150 + "null" + "}" * 150

    def test_the_node_budget_counts_containers_and_scalars(self) -> None:
        value = [1, 2, 3]  # the list and three numbers: four nodes

        assert canonicalize(value, max_nodes=4) == "[1,2,3]"
        with pytest.raises(ValueError, match="more than 3 values"):
            canonicalize(value, max_nodes=3)

    def test_object_keys_are_nodes(self) -> None:
        value = {"a": 1, "b": 2}  # the object, two keys and two values: five nodes

        assert canonicalize(value, max_nodes=5) == '{"a":1,"b":2}'
        with pytest.raises(ValueError, match="more than 4 values"):
            canonicalize(value, max_nodes=4)

    def test_a_wide_container_is_refused_before_it_is_walked(self) -> None:
        wide = [0] * 100_000

        with pytest.raises(ValueError, match="more than 10 values"):
            canonicalize(wide, max_nodes=10)
        with pytest.raises(ValueError, match="more than 10 values"):
            canonicalize(dict.fromkeys(map(str, range(100_000)), 0), max_nodes=10)

    def test_a_million_nested_empty_lists_are_refused_by_the_node_budget(self) -> None:
        many: list[list[object]] = [[] for _ in range(1_000_001)]

        with pytest.raises(ValueError, match="more than 1000000 values"):
            canonicalize(many)

    def test_the_byte_budget_counts_utf8_bytes_of_the_output(self) -> None:
        assert canonicalize("é" * 5, max_bytes=12) == '"ééééé"'  # 5 * 2 bytes + 2 quotes
        with pytest.raises(ValueError, match="more than 11 bytes of output"):
            canonicalize("é" * 5, max_bytes=11)

    def test_the_byte_budget_stops_the_output_of_many_small_values(self) -> None:
        with pytest.raises(ValueError, match="bytes of output"):
            canonicalize([1] * 1000, max_bytes=100)

    def test_the_defaults_are_generous_and_finite(self) -> None:
        from tumbleweed.core import jcs

        assert jcs.MAX_NODES == 1_000_000
        assert jcs.MAX_BYTES == 16 * 1024 * 1024
        assert canonicalize(list(range(1000))).startswith("[0,1,2")

    def test_a_lone_scalar_is_a_node_too(self) -> None:
        assert canonicalize(1, max_nodes=1) == "1"
        with pytest.raises(ValueError, match="more than 0 values"):
            canonicalize(1, max_nodes=0)
