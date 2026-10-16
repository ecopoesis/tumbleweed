"""One rule for numbers, so that a spec still validates after RFC 8785 canonicalisation.

The tool store (S23) hashes the canonical JSON of what it saves, and keeps those bytes. JCS writes
every number as a double in ECMAScript's shortest form, so `1e16` becomes `10000000000000000` and
`5.0` becomes `5`: a spec that was valid when written must be valid when read back, with the same
meaning. These tests canonicalise specs full of numbers near the edges (around 2**53, 1e16, 1e20,
subnormals) and check that, for every kind of state.
"""

import json
import random

import pytest

from tests.ui.spec.support import STATE_KINDS, Json, build, lit_int
from tumbleweed.core.jcs import canonicalize, es_number
from tumbleweed.core.types import JsonValue
from tumbleweed.ui.spec.errors import SpecError
from tumbleweed.ui.spec.expr import FloatLit
from tumbleweed.ui.spec.limits import MAX_SAFE_INT
from tumbleweed.ui.spec.parse import parse_ui_spec, validate_ui_spec
from tumbleweed.ui.spec.spec import UiSpec
from tumbleweed.ui.spec.state import FloatState

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


def float_and_json_positions(value: JsonValue) -> dict[str, Json]:
    """One state entry for each place a number may be that takes an integer-valued double."""
    columns: list[JsonValue] = [{"name": "f", "type": "float"}]
    rows: list[JsonValue] = [[value]]
    items: list[JsonValue] = [value]
    nested: JsonValue = {"a": [{"b": value}]}
    return {
        "float default": {"kind": "float", "key": "x", "default": value},
        "float minimum": {"kind": "float", "key": "x", "default": value, "minimum": value},
        "float maximum": {"kind": "float", "key": "x", "default": 0.0, "maximum": value},
        "float step": {"kind": "float", "key": "x", "default": 0.0, "step": value},
        "float literal": {
            "kind": "float",
            "key": "x",
            "default": 0.0,
            "computed": {"kind": "float", "value": value},
        },
        "table float cell": {
            "kind": "table",
            "key": "x",
            "default": {"columns": columns, "rows": rows},
        },
        "list float item": {"kind": "list", "key": "x", "item_type": "float", "default": items},
        "json default": {"kind": "json", "key": "x", "default": value},
        "nested json": {"kind": "json", "key": "x", "default": nested},
    }


POSITIONS = sorted(float_and_json_positions(0))


class TestTheOneRuleForNumbers:
    """Where a number is written as an integer: it stands for a double, or it is refused."""

    @pytest.mark.parametrize(
        "value",
        [
            2**53 + 1,
            10**16 + 1,
            -(2**53) - 1,
            2**60 + 1,
            1152921504606847001,
            10**21 - 1,
            10**23 + 1,
            10**400,
        ],
        ids=lambda v: f"{v.bit_length()}-bits",
    )
    @pytest.mark.parametrize("position", POSITIONS)
    def test_an_integer_no_double_stands_for_is_refused_in_every_position(
        self, position: str, value: int
    ) -> None:
        entry = float_and_json_positions(value)[position]

        with pytest.raises(SpecError, match=r"no double is equal|too large for a double"):
            validate_ui_spec(build(state=[entry]))

    @pytest.mark.parametrize(
        "value",
        [
            2**53,
            10**16,
            10**20,
            2**100,
            1152921504606847000,  # 2**60 as canonical JSON writes it
            295147905179352830000,  # 2**68, the number of RFC 8785 appendix B
            123456789012345680000,
        ],
        ids=lambda v: f"{v.bit_length()}-bits",
    )
    @pytest.mark.parametrize("position", POSITIONS)
    def test_the_same_kinds_of_integer_are_fine_where_they_stand_for_a_double(
        self, position: str, value: int
    ) -> None:
        validate_ui_spec(build(state=[float_and_json_positions(value)[position]]))

    def test_nothing_is_silently_rounded(self) -> None:
        spec = validate_ui_spec(build(state=[{"kind": "float", "key": "x", "default": 2**53 + 2}]))

        assert getattr(spec.state[0], "default") == float(2**53 + 2)  # noqa: B009
        with pytest.raises(SpecError, match="no double is equal"):
            validate_ui_spec(build(state=[{"kind": "float", "key": "x", "default": 2**53 + 1}]))

    def test_an_integer_that_is_the_spelling_of_a_double_is_stored_as_that_double(self) -> None:
        spec = validate_ui_spec(
            build(state=[{"kind": "float", "key": "x", "default": 1152921504606847000}])
        )

        default = getattr(spec.state[0], "default")  # noqa: B009
        assert type(default) is float
        assert default == float(2**60)
        assert int(default) == 2**60  # the double, not the digits that were written


def doubles_between_2_53_and_1e21(count: int) -> list[float]:
    """Random doubles from every binade of [2**53, 1e21), plus the doubles of the reviewers."""
    rng = random.Random(8785)  # noqa: S311 -- a reproducible sample, nothing secret
    found = [
        float(2**60),
        float(2**68),
        1.2345678901234568e20,
        float(2**53),
        999999999999999700000.0,
    ]
    while len(found) < count:
        exponent = rng.randrange(53, 70)  # 2**69 is 5.9e20; 2**70 is already over 1e21
        value = float((2**52 + rng.getrandbits(52)) * 2 ** (exponent - 52))
        if 2**53 <= value < 1e21:
            found.append(value)
    return found


DOUBLES = doubles_between_2_53_and_1e21(100)


class TestDoublesAbove2To53SurviveCanonicalisation:
    """JCS writes these as their shortest digits padded with zeros (`2**60` as
    `1152921504606847000`), which is no longer exactly the double: a spec holding one must still
    validate when it is read back from its canonical form, in every number position (R2-B1)."""

    def test_most_of_them_change_their_digits_which_is_why_this_is_tested(self) -> None:
        changed = [value for value in DOUBLES if int(es_number(value)) != int(value)]

        assert len(changed) > len(DOUBLES) // 2
        assert es_number(float(2**60)) == "1152921504606847000"
        assert es_number(float(2**68)) == "295147905179352830000"
        assert es_number(1.2345678901234568e20) == "123456789012345680000"

    def test_the_sample_is_in_range_and_varied(self) -> None:
        assert len(DOUBLES) == 100
        assert all(2**53 <= value < 1e21 for value in DOUBLES)
        assert len(set(DOUBLES)) == 100
        assert len({value.hex()[-6:] for value in DOUBLES}) > 50

    @pytest.mark.parametrize("position", POSITIONS)
    @pytest.mark.parametrize("form", ["float", "int"])
    def test_every_position_accepts_what_canonicalisation_writes(
        self, position: str, form: str
    ) -> None:
        for value in DOUBLES:
            written: JsonValue = value if form == "float" else int(value)
            entry = float_and_json_positions(written)[position]

            spec, again, canonical = roundtrip(build(state=[entry]))

            assert again == spec, (value.hex(), canonical)
            assert es_number(value) in canonical
            assert canonicalize(json.loads(again.model_dump_json())) == canonical

    def test_every_position_at_once_in_one_spec(self) -> None:
        state: list[Json] = []
        for number, (name, entry) in enumerate(float_and_json_positions(float(2**60)).items()):
            state.append({**entry, "key": f"k{number}"})
            assert name  # the names are the positions of the table above

        assert_survives(build(state=state))

    def test_the_digits_json_text_carries_are_read_as_the_nearest_double(self) -> None:
        text = (
            '{"version":"tumbleweed.ui/1","title":"t","root":{"kind":"box"},'
            '"state":[{"kind":"float","key":"x","default":295147905179352830000},'
            '{"kind":"json","key":"j","default":[295147905179352830000]}]}'
        )

        spec = parse_ui_spec(text)

        assert getattr(spec.state[0], "default") == float(2**68)  # noqa: B009
        assert getattr(spec.state[1], "default") == [float(2**68)]  # noqa: B009
