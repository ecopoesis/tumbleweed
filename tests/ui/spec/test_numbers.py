"""One rule for numbers, so that a spec still validates after RFC 8785 canonicalisation.

The tool store (S23) hashes the canonical JSON of what it saves, and keeps those bytes. JCS writes
every number as a double in ECMAScript's shortest form, so `1e16` becomes `10000000000000000` and
`5.0` becomes `5`: a spec that was valid when written must be valid when read back, with the same
meaning. These tests canonicalise specs full of numbers near the edges (around 2**53, 1e16, 1e20,
subnormals) and check that, for every kind of state.
"""

import json
import math
from typing import TYPE_CHECKING

import pytest

from tests.ui.spec import jcs
from tests.ui.spec.jcs import canonicalize, double_from_bits, es_number
from tests.ui.spec.support import STATE_KINDS, Json, build, lit_int
from tumbleweed.ui.spec.errors import SpecError
from tumbleweed.ui.spec.expr import FloatLit
from tumbleweed.ui.spec.limits import MAX_SAFE_INT
from tumbleweed.ui.spec.parse import parse_ui_spec, validate_ui_spec
from tumbleweed.ui.spec.spec import UiSpec
from tumbleweed.ui.spec.state import FloatState

if TYPE_CHECKING:
    from tumbleweed.core.types import JsonValue

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


class TestTheCanonicaliserIsRight:
    """The helper is itself checked against the RFC, or the round trips below prove nothing."""

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

    def test_objects_are_sorted_by_utf16_code_units_and_text_is_escaped(self) -> None:
        text = canonicalize(
            {"b": 1, "a": [True, None, 'x\ny"\u0001'], "\U0001f600": 2, "\uffff": 3}
        )

        assert text == '{"a":[true,null,"x\\ny\\"\\u0001"],"b":1,"\U0001f600":2,"\uffff":3}'

    def test_an_integer_that_is_no_double_is_refused_here_as_the_real_thing_would_round_it(
        self,
    ) -> None:
        assert canonicalize(2**53) == "9007199254740992"
        with pytest.raises(ValueError, match="not an IEEE-754 double"):
            canonicalize(2**53 + 1)
        with pytest.raises(ValueError, match="no NaN or infinity"):
            canonicalize(math.nan)


# Numbers near the edges, as Python values: ints that are exactly doubles, and floats.
FLOATS: list[float | int] = [
    0,
    1,
    -1,
    0.1,
    1 / 3,
    -0.0,
    2**53 - 1,
    2**53,
    -(2**53),
    2**53 + 2,
    10**16,
    1e16,
    10**20,
    1e20,
    10**21,
    1e22,
    2**100,
    1e300,
    1.7976931348623157e308,
    5e-324,
    2.2250738585072014e-308,
    1e-7,
    123456789.123456789,
]
INTS: list[int] = [0, 1, -1, MAX_SAFE_INT, -MAX_SAFE_INT, 2**52, 12345678901234]


def roundtrip(data: Json) -> tuple[UiSpec, UiSpec, str]:
    """A spec, the spec read back from its canonical JSON, and that canonical JSON."""
    spec = validate_ui_spec(data)
    canonical = canonicalize(json.loads(spec.model_dump_json()))
    return spec, parse_ui_spec(canonical), canonical


def assert_survives(data: Json) -> None:
    spec, again, canonical = roundtrip(data)

    assert again == spec  # the same meaning, value for value
    # And canonicalisation is idempotent: what is saved is what would be saved again.
    assert canonicalize(json.loads(again.model_dump_json())) == canonical


class TestEveryKindOfStateSurvivesCanonicalisation:
    def test_floats(self) -> None:
        state: list[Json] = [
            {"kind": "float", "key": f"f{i}", "default": value} for i, value in enumerate(FLOATS)
        ]

        assert_survives(build(state=state))

    def test_float_bounds_and_step(self) -> None:
        state: list[Json] = [
            {"kind": "float", "key": "a", "default": 2**53, "minimum": -(2**53), "maximum": 10**20},
            {"kind": "float", "key": "b", "default": 1e16, "minimum": 1e16, "maximum": 1e20},
            {"kind": "float", "key": "c", "default": 0.5, "step": 5e-324},
            {"kind": "float", "key": "d", "default": 0.5, "step": 10**16},
        ]

        assert_survives(build(state=state))

    def test_ints(self) -> None:
        state: list[Json] = [
            {"kind": "int", "key": f"i{i}", "default": value} for i, value in enumerate(INTS)
        ]
        state.append(
            {
                "kind": "int",
                "key": "bounded",
                "default": 0,
                "minimum": -MAX_SAFE_INT,
                "maximum": MAX_SAFE_INT,
                "step": 2**40,
            }
        )

        assert_survives(build(state=state))

    def test_lists_of_floats_and_of_ints(self) -> None:
        floats: list[JsonValue] = [*FLOATS]
        ints: list[JsonValue] = [*INTS]
        state: list[Json] = [
            {"kind": "list", "key": "f", "item_type": "float", "default": floats},
            {"kind": "list", "key": "i", "item_type": "int", "default": ints},
        ]

        assert_survives(build(state=state))

    def test_tables(self) -> None:
        columns: list[JsonValue] = [
            {"name": "f", "type": "float"},
            {"name": "i", "type": "int"},
            {"name": "s", "type": "string"},
            {"name": "b", "type": "bool"},
        ]
        rows: list[JsonValue] = [
            [value, INTS[n % len(INTS)], "t", n % 2 == 0] for n, value in enumerate(FLOATS)
        ]
        state: list[Json] = [
            {"kind": "table", "key": "t", "default": {"columns": columns, "rows": rows}}
        ]

        assert_survives(build(state=state))

    def test_json_values_which_is_where_canonical_json_changes_the_type_of_a_number(self) -> None:
        # `1e16` is written `10000000000000000`, which reads back as an integer.
        value: JsonValue = {
            "numbers": [*FLOATS],
            "nested": [[1e16, 10**16], {"x": 2**53, "y": 5e-324}],
        }
        state: list[Json] = [{"kind": "json", "key": "j", "default": value}]

        assert_survives(build(state=state))

    def test_the_other_kinds_have_no_numbers_to_lose(self) -> None:
        state: list[Json] = [
            {"kind": "bool", "key": "b", "default": True},
            {"kind": "string", "key": "s", "default": "1e16 and 5.0 are text here"},
            {"kind": "enum", "key": "e", "options": ["1e16", "5.0"], "default": "5.0"},
            {"kind": "image", "key": "i", "default": {"path": "out/1e16.png"}},
            {"kind": "file", "key": "f", "default": {"path": "out/5.0.csv"}},
        ]

        assert_survives(build(state=state))

    def test_every_kind_of_state_is_covered_by_a_test_above(self) -> None:
        covered = {
            "float",
            "int",
            "list",
            "table",
            "json",
            "bool",
            "string",
            "enum",
            "image",
            "file",
        }

        assert covered == set(STATE_KINDS)


class TestExpressionLiteralsSurviveCanonicalisation:
    def test_float_and_int_literals(self) -> None:
        literals: list[Json] = [{"kind": "float", "value": value} for value in FLOATS]
        literals += [lit_int(value) for value in INTS]
        adds: list[Json] = [
            {"kind": "add", "left": left, "right": right}
            for left, right in zip(literals[::2], literals[1::2], strict=False)
        ]
        state: list[Json] = [
            {"kind": "float", "key": f"c{i}", "default": 0.0, "computed": expression}
            for i, expression in enumerate(adds)
        ]

        assert_survives(build(state=state))

    def test_a_float_literal_written_as_a_whole_number_is_still_a_float(self) -> None:
        # What canonical JSON makes of {"kind": "float", "value": 1e16}.
        spec = parse_ui_spec(
            '{"version":"tumbleweed.ui/1","title":"t","root":{"kind":"box"},'
            '"state":[{"computed":{"kind":"float","value":10000000000000000},"default":0,'
            '"key":"x","kind":"float"}]}'
        )

        entry = spec.state[0]
        assert isinstance(entry, FloatState)
        literal = entry.computed
        assert isinstance(literal, FloatLit)
        assert type(literal.value) is float
        assert literal.value == 1e16


class TestTheOneRuleForNumbers:
    """Where a number is written as an integer: exactly some double, or it is refused."""

    @pytest.mark.parametrize("value", [2**53 + 1, 10**16 + 1, -(2**53) - 1])
    def test_an_integer_no_double_equals_is_refused_in_every_float_or_json_position(
        self, value: int
    ) -> None:
        floats: list[JsonValue] = [value]
        columns: list[JsonValue] = [{"name": "f", "type": "float"}]
        rows: list[JsonValue] = [[value]]
        positions: list[Json] = [
            {"kind": "float", "key": "x", "default": value},
            {"kind": "float", "key": "x", "default": 0.0, "minimum": value},
            {
                "kind": "float",
                "key": "x",
                "default": 0.0,
                "computed": {"kind": "float", "value": value},
            },
            {"kind": "list", "key": "x", "item_type": "float", "default": floats},
            {"kind": "table", "key": "x", "default": {"columns": columns, "rows": rows}},
            {"kind": "json", "key": "x", "default": floats},
        ]

        for entry in positions:
            with pytest.raises(SpecError, match="no double is equal"):
                validate_ui_spec(build(state=[entry]))

    def test_the_same_integers_are_fine_where_a_double_holds_them(self) -> None:
        for value in (2**53, 10**16, 10**20):
            floats: list[JsonValue] = [value]
            columns: list[JsonValue] = [{"name": "f", "type": "float"}]
            rows: list[JsonValue] = [[value]]
            positions: list[Json] = [
                {"kind": "float", "key": "x", "default": value},
                {"kind": "list", "key": "x", "item_type": "float", "default": floats},
                {"kind": "table", "key": "x", "default": {"columns": columns, "rows": rows}},
                {"kind": "json", "key": "x", "default": floats},
            ]
            for entry in positions:
                validate_ui_spec(build(state=[entry]))

    def test_nothing_is_silently_rounded(self) -> None:
        spec = validate_ui_spec(build(state=[{"kind": "float", "key": "x", "default": 2**53 + 2}]))

        assert getattr(spec.state[0], "default") == float(2**53 + 2)  # noqa: B009
        with pytest.raises(SpecError, match="no double is equal"):
            validate_ui_spec(build(state=[{"kind": "float", "key": "x", "default": 2**53 + 1}]))

    def test_the_module_is_the_one_the_tests_use(self) -> None:
        assert jcs.canonicalize({"a": 1.0}) == '{"a":1}'
