"""`excerpt`: a short, safe description of untrusted input, whatever its type."""

import pytest

from tumbleweed.core.errors import excerpt


class Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("the repr of this object raises")


class HugeRepr:
    def __repr__(self) -> str:
        return "x" * 10_000_000


class LyingStr(str):
    __slots__ = ()

    def __repr__(self) -> str:
        return "'harmless'"

    def __getitem__(self, key: object) -> str:
        return "lies"


class LyingInt(int):
    __slots__ = ()

    def __repr__(self) -> str:
        return "42"


class LyingFloat(float):
    __slots__ = ()

    def __repr__(self) -> str:
        return "1.0"


class TestStrings:
    def test_a_short_string_is_its_repr(self) -> None:
        assert excerpt("abc") == "'abc'"

    def test_a_long_string_is_cut_and_its_length_given(self) -> None:
        result = excerpt("x" * 5000)

        assert result == repr("x" * 64) + "... (5000 chars)"

    @pytest.mark.parametrize("char", ["\x1b", "\r", "\n", "\u202e", "\u2028", "\x00", "\ud800"])
    def test_control_format_and_separator_characters_come_out_escaped(self, char: str) -> None:
        result = excerpt(f"a{char}b")

        assert char not in result
        assert result.isprintable()

    def test_a_str_subclass_is_not_asked_to_describe_itself(self) -> None:
        assert excerpt(LyingStr("real")) == "<LyingStr>"


class TestOtherValues:
    @pytest.mark.parametrize(("value", "shown"), [(True, "True"), (False, "False"), (None, "None")])
    def test_bool_and_none_are_shown_as_themselves(self, value: object, shown: str) -> None:
        assert excerpt(value) == shown

    @pytest.mark.parametrize(
        "value",
        [1.5, -0.25, 0.0, -0.0, 1e300, 5e-324, 1.7976931348623157e308, 1 / 3, 1e22, 2.0**60],
    )
    def test_a_float_is_shown_as_itself_because_a_repr_of_it_is_short_and_safe(
        self, value: float
    ) -> None:
        assert excerpt(value) == repr(value)
        assert len(excerpt(value)) <= 24

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_floats_are_shown_too(self, value: float) -> None:
        assert excerpt(value) == repr(value)

    def test_a_float_subclass_is_not_asked_to_describe_itself(self) -> None:
        assert excerpt(LyingFloat(2.5)) == "<LyingFloat>"

    @pytest.mark.parametrize("value", [0, 5, -7, 2**63, -(2**63), 2**64 - 1])
    def test_a_short_int_is_shown(self, value: int) -> None:
        assert excerpt(value) == repr(value)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(2**64, id="2**64"),
            pytest.param(-(2**64), id="-2**64"),
            pytest.param(10**5000, id="10**5000"),
            pytest.param(-(10**5000), id="-10**5000"),
        ],
    )
    def test_a_long_int_is_described_by_its_size_never_printed(self, value: int) -> None:
        result = excerpt(value)

        assert result == f"an int of {abs(value).bit_length()} bits"
        assert len(result) < 40

    def test_an_int_subclass_is_not_asked_to_describe_itself(self) -> None:
        assert excerpt(LyingInt(1)) == "<LyingInt>"

    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            ([1, 2], "<list>"),
            ({"a": 1}, "<dict>"),
            ((1,), "<tuple>"),
            (b"abc", "<bytes>"),
            (object(), "<object>"),
        ],
    )
    def test_anything_else_is_its_type_name_only(self, value: object, shown: str) -> None:
        assert excerpt(value) == shown

    def test_a_raising_repr_is_never_called(self) -> None:
        assert excerpt(Hostile()) == "<Hostile>"

    def test_a_huge_repr_is_never_called(self) -> None:
        assert excerpt(HugeRepr()) == "<HugeRepr>"

    def test_a_deep_list_is_not_walked(self) -> None:
        deep: list[object] = []
        for _ in range(100_000):
            deep = [deep]

        assert excerpt(deep) == "<list>"

    def test_a_very_long_type_name_is_cut(self) -> None:
        huge = type("T" * 5000, (), {})

        assert len(excerpt(huge())) <= 70
