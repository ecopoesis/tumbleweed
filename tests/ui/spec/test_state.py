"""State declarations and the one function that decides whether a value fits one."""

import unicodedata

import pytest
from pydantic import TypeAdapter, ValidationError

from tests.ui.spec.support import MINIMAL_STATE as MINIMAL
from tests.ui.spec.support import STATE_KINDS, Json, lit_int
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import JsonValue
from tumbleweed.ui.spec.limits import (
    DEFAULT_LIST_MAX_ITEMS,
    DEFAULT_STRING_MAX_LENGTH,
    DEFAULT_TABLE_MAX_ROWS,
    MAX_ENUM_OPTIONS,
    MAX_LIST_ITEMS,
    MAX_OPTION_LENGTH,
    MAX_SAFE_INT,
    MAX_STRING_LENGTH,
    MAX_TABLE_ROWS,
)
from tumbleweed.ui.spec.state import (
    EnumState,
    FileState,
    FloatState,
    ImageState,
    IntState,
    JsonState,
    ListState,
    StateDecl,
    StringState,
    TableState,
    check_value,
    port_type_of,
)
from tumbleweed.ui.spec.values import FileRef, ImageRef, Table

DECL = TypeAdapter(StateDecl)
COLUMN_A: list[JsonValue] = [{"name": "a", "type": "int"}]


def decl(kind: str, **fields: JsonValue) -> StateDecl:
    return DECL.validate_python({"kind": kind, "key": "k", **fields})


def accepts(kind: str, **fields: JsonValue) -> None:
    """The declaration is valid (a ValidationError fails the test)."""
    decl(kind, **fields)


def refuse(kind: str, match: str, **fields: JsonValue) -> None:
    with pytest.raises(ValidationError, match=match):
        decl(kind, **fields)


def test_the_minimal_declarations_cover_every_kind_of_state() -> None:
    assert set(MINIMAL) == set(STATE_KINDS)
    assert len(STATE_KINDS) == 10


@pytest.mark.parametrize("kind", MINIMAL)
class TestEveryKind:
    def test_a_minimal_declaration_is_valid_and_has_a_default(self, kind: str) -> None:
        parsed = decl(kind, **MINIMAL[kind])

        assert type(parsed) is STATE_KINDS[kind]
        assert parsed.kind == kind
        assert "default" in type(parsed).model_fields

    def test_survives_a_json_round_trip_with_its_defaults_written_out(self, kind: str) -> None:
        parsed = decl(kind, **MINIMAL[kind])

        again = DECL.validate_json(DECL.dump_json(parsed))

        assert again == parsed
        assert DECL.dump_json(again) == DECL.dump_json(parsed)

    def test_has_a_valid_key_and_no_extras_and_is_immutable(self, kind: str) -> None:
        with pytest.raises(ValidationError, match="key"):
            DECL.validate_python({"kind": kind, **MINIMAL[kind], "key": "Bad Key"})
        with pytest.raises(ValidationError, match="Field required"):
            DECL.validate_python({"kind": kind, **MINIMAL[kind]})
        with pytest.raises(ValidationError, match="Extra inputs"):
            decl(kind, **MINIMAL[kind], extra=1)
        parsed = decl(kind, **MINIMAL[kind])
        with pytest.raises(ValidationError, match="frozen"):
            setattr(parsed, "key", "other")  # noqa: B010  (the assignment is the point)

    def test_the_default_passes_check_value(self, kind: str) -> None:
        parsed = decl(kind, **MINIMAL[kind])

        value = check_value(parsed, getattr(parsed, "default"))  # noqa: B009  (every kind has one)

        # A list default is held as a tuple and checked as one.
        assert value == getattr(parsed, "default")  # noqa: B009


COMPUTABLE = ["bool", "int", "float", "string"]


class TestComputed:
    @pytest.mark.parametrize("kind", COMPUTABLE)
    def test_scalar_kinds_may_be_computed(self, kind: str) -> None:
        parsed = decl(kind, **MINIMAL[kind], computed=lit_int(1))

        assert getattr(parsed, "computed") is not None  # noqa: B009

    @pytest.mark.parametrize("kind", [k for k in MINIMAL if k not in COMPUTABLE])
    def test_the_other_kinds_cannot_be(self, kind: str) -> None:
        refuse(kind, "Extra inputs are not permitted", **MINIMAL[kind], computed=lit_int(1))

    def test_not_computed_by_default(self) -> None:
        assert getattr(decl("int", default=1), "computed") is None  # noqa: B009

    def test_the_expression_is_a_tree_never_text(self) -> None:
        refuse("int", "valid dictionary or object", default=1, computed="1 + 1")


class TestInt:
    def test_bounds_and_step(self) -> None:
        parsed = decl("int", default=5, minimum=0, maximum=10, step=5)

        assert isinstance(parsed, IntState)
        assert (parsed.minimum, parsed.maximum, parsed.step) == (0, 10, 5)

    def test_unbounded_by_default_with_step_one(self) -> None:
        parsed = decl("int", default=5)

        assert isinstance(parsed, IntState)
        assert (parsed.minimum, parsed.maximum, parsed.step) == (None, None, 1)

    def test_the_default_must_be_within_the_bounds_inclusive(self) -> None:
        accepts("int", default=0, minimum=0, maximum=10)
        accepts("int", default=10, minimum=0, maximum=10)
        refuse("int", "must be at least 0, got -1", default=-1, minimum=0)
        refuse("int", "must be at most 10, got 11", default=11, maximum=10)

    def test_minimum_may_not_exceed_maximum(self) -> None:
        accepts("int", default=5, minimum=5, maximum=5)
        refuse("int", "minimum 6 is above maximum 5", default=5, minimum=6, maximum=5)

    @pytest.mark.parametrize("field", ["default", "minimum", "maximum", "step"])
    @pytest.mark.parametrize("bad", [5.0, "5", True])
    def test_an_int_is_an_int(self, field: str, bad: JsonValue) -> None:
        fields: dict[str, JsonValue] = {"default": 5}
        fields[field] = bad
        with pytest.raises(ValidationError):
            decl("int", **fields)

    def test_null_means_no_bound(self) -> None:
        accepts("int", default=5, minimum=None, maximum=None)
        with pytest.raises(ValidationError):
            decl("int", default=None)

    @pytest.mark.parametrize("step", [0, -1])
    def test_the_step_is_positive(self, step: int) -> None:
        refuse("int", "greater than or equal to 1", default=1, step=step)

    @pytest.mark.parametrize(
        ("field", "fits", "over"),
        [
            ("default", {"default": MAX_SAFE_INT}, MAX_SAFE_INT + 1),
            ("minimum", {"default": 1, "minimum": -MAX_SAFE_INT}, -MAX_SAFE_INT - 1),
            ("maximum", {"default": 1, "maximum": MAX_SAFE_INT}, MAX_SAFE_INT + 1),
            ("step", {"default": 1, "step": MAX_SAFE_INT}, MAX_SAFE_INT + 1),
        ],
    )
    def test_integers_stay_within_what_canonical_json_keeps_exact(
        self, field: str, fits: dict[str, JsonValue], over: int
    ) -> None:
        accepts("int", **fits)
        for too_big in (over, 10**30):
            with pytest.raises(ValidationError):
                decl("int", **{**fits, field: too_big})


class TestFloat:
    def test_bounds_and_step(self) -> None:
        parsed = decl("float", default=0.5, minimum=0, maximum=1, step=0.1)

        assert isinstance(parsed, FloatState)
        assert (parsed.minimum, parsed.maximum, parsed.step) == (0.0, 1.0, 0.1)

    def test_an_int_is_accepted_and_becomes_a_float(self) -> None:
        parsed = decl("float", default=2, minimum=0, maximum=10)

        assert isinstance(parsed, FloatState)
        assert type(parsed.default) is float
        assert type(parsed.minimum) is float

    def test_the_default_must_be_within_the_bounds_inclusive(self) -> None:
        accepts("float", default=0.0, minimum=0, maximum=1)
        accepts("float", default=1.0, minimum=0, maximum=1)
        refuse("float", "must be at least 0.0, got -0.1", default=-0.1, minimum=0)
        refuse("float", "must be at most 1.0, got 1.1", default=1.1, maximum=1)
        refuse("float", "minimum 2.0 is above maximum 1.0", default=1, minimum=2, maximum=1)

    @pytest.mark.parametrize("field", ["default", "minimum", "maximum", "step"])
    @pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e999"])
    def test_numbers_are_finite(self, field: str, raw: str) -> None:
        fields = {"default": "0", "minimum": "0", "maximum": "1", "step": "0.1"}
        fields[field] = raw
        members = ",".join(f'"{name}":{value}' for name, value in fields.items())

        with pytest.raises(ValidationError):
            DECL.validate_json('{"kind":"float","key":"k",' + members + "}")

    @pytest.mark.parametrize("step", [0, -0.5])
    def test_the_step_is_positive(self, step: float) -> None:
        refuse("float", "greater than 0", default=1, step=step)

    @pytest.mark.parametrize("bad", ["1.5", True, None])
    def test_a_float_is_a_number(self, bad: JsonValue) -> None:
        with pytest.raises(ValidationError):
            decl("float", default=bad)


class TestString:
    def test_the_length_limit_defaults_and_is_bounded(self) -> None:
        parsed = decl("string", default="")

        assert isinstance(parsed, StringState)
        assert parsed.max_length == DEFAULT_STRING_MAX_LENGTH
        accepts("string", default="", max_length=MAX_STRING_LENGTH)
        refuse(
            "string", "less than or equal to 65536", default="", max_length=MAX_STRING_LENGTH + 1
        )
        refuse("string", "greater than or equal to 1", default="", max_length=0)

    def test_the_default_may_not_exceed_the_declared_length(self) -> None:
        accepts("string", default="abc", max_length=3)
        refuse("string", "4 characters, the limit is 3", default="abcd", max_length=3)

    def test_the_default_is_data_text(self) -> None:
        accepts("string", default="\x1b[31m\ufeff\r\n\u202e")
        refuse("string", "NUL", default="a\0b")

    def test_a_longer_default_needs_a_longer_declared_limit(self) -> None:
        long = "x" * (DEFAULT_STRING_MAX_LENGTH + 1)

        refuse("string", "limit is 1000", default=long)
        accepts("string", default=long, max_length=5000)


class TestEnum:
    def test_options_and_a_default_among_them(self) -> None:
        parsed = decl("enum", options=["small", "large"], default="large")

        assert isinstance(parsed, EnumState)
        assert parsed.options == ("small", "large")

    def test_the_default_must_be_an_option(self) -> None:
        refuse("enum", "one of 2 options", options=["a", "b"], default="c")
        refuse("enum", "one of 2 options", options=["a", "b"], default="A")

    def test_options_are_distinct(self) -> None:
        refuse("enum", "distinct", options=["a", "a"], default="a")

    def test_options_that_differ_only_in_unicode_composition_are_the_same_option(self) -> None:
        composed = unicodedata.normalize("NFC", "é")
        decomposed = unicodedata.normalize("NFD", composed)
        assert composed != decomposed

        refuse("enum", "distinct", options=[composed, decomposed], default=composed)

    def test_the_count_is_one_to_the_limit(self) -> None:
        many: list[JsonValue] = [f"o{i}" for i in range(MAX_ENUM_OPTIONS)]

        accepts("enum", options=["only"], default="only")
        accepts("enum", options=many, default="o0")
        refuse("enum", "at most 64", options=[*many, "extra"], default="o0")
        refuse("enum", "at least 1", options=[], default="a")

    def test_an_option_is_a_non_empty_authored_line_of_bounded_length(self) -> None:
        longest = "x" * MAX_OPTION_LENGTH
        accepts("enum", options=[longest], default=longest)
        for bad in ("", "x" * (MAX_OPTION_LENGTH + 1), "a\nb", "a\0b", "a\u202eb"):
            with pytest.raises(ValidationError):
                decl("enum", options=[bad], default=bad)


class TestImageAndFile:
    def test_the_default_is_none_or_a_ref(self) -> None:
        empty = decl("image")
        with_ref = decl("image", default={"path": "a/b.png"})
        file = decl("file", default={"path": "data.csv"})

        assert isinstance(empty, ImageState)
        assert empty.default is None
        assert isinstance(with_ref, ImageState)
        assert with_ref.default == ImageRef.model_validate({"path": "a/b.png"})
        assert isinstance(file, FileState)
        assert file.default == FileRef.model_validate({"path": "data.csv"})

    @pytest.mark.parametrize("kind", ["image", "file"])
    @pytest.mark.parametrize(
        "bad", ["a.png", "/etc/x.png", "../x.png", {"path": "/etc/x.png"}, {"path": "../x.png"}]
    )
    def test_the_default_is_a_validated_workspace_ref(self, kind: str, bad: JsonValue) -> None:
        with pytest.raises(ValidationError):
            decl(kind, default=bad)

    def test_an_image_default_is_a_raster_image(self) -> None:
        refuse("image", "never SVG", default={"path": "a.svg"})


class TestTable:
    def test_the_row_limit_defaults_and_is_bounded(self) -> None:
        parsed = decl("table")

        assert isinstance(parsed, TableState)
        assert parsed.max_rows == DEFAULT_TABLE_MAX_ROWS
        assert parsed.default == Table()
        accepts("table", max_rows=MAX_TABLE_ROWS)
        refuse("table", "less than or equal to 10000", max_rows=MAX_TABLE_ROWS + 1)
        refuse("table", "greater than or equal to 1", max_rows=0)

    def test_the_default_may_not_exceed_the_row_limit(self) -> None:
        three: list[JsonValue] = [[1], [2], [3]]

        accepts("table", max_rows=3, default={"columns": COLUMN_A, "rows": three})
        refuse(
            "table",
            "3 rows, the limit is 2",
            max_rows=2,
            default={"columns": COLUMN_A, "rows": three},
        )

    def test_the_default_is_a_well_formed_table(self) -> None:
        bad: list[JsonValue] = [["x"]]

        refuse("table", "is of type int", default={"columns": COLUMN_A, "rows": bad})


class TestList:
    def test_the_item_type_and_the_item_limit(self) -> None:
        parsed = decl("list", item_type="string")

        assert isinstance(parsed, ListState)
        assert parsed.max_items == DEFAULT_LIST_MAX_ITEMS
        assert parsed.default == ()
        accepts("list", item_type="int", max_items=MAX_LIST_ITEMS)
        refuse("list", "less than or equal to 1000", item_type="int", max_items=MAX_LIST_ITEMS + 1)
        refuse("list", "greater than or equal to 1", item_type="int", max_items=0)

    @pytest.mark.parametrize("item_type", ["date", "list", "table", "json", "enum", "Int", ""])
    def test_items_are_scalars_of_the_four_types_only(self, item_type: str) -> None:
        refuse("list", "Input should be", item_type=item_type)

    @pytest.mark.parametrize(
        ("item_type", "good", "bad"),
        [
            ("bool", [True, False], [1]),
            ("int", [1, -2], [1.5]),
            ("int", [1, -2], [True]),
            ("int", [1], [MAX_SAFE_INT + 1]),
            ("float", [1.5, 2], ["1.5"]),
            ("string", ["a", ""], [1]),
            ("string", ["a"], ["a\0b"]),
        ],
    )
    def test_the_default_holds_items_of_the_item_type(
        self, item_type: str, good: list[JsonValue], bad: list[JsonValue]
    ) -> None:
        accepts("list", item_type=item_type, default=good)
        with pytest.raises(ValidationError):
            decl("list", item_type=item_type, default=bad)

    def test_the_default_may_not_exceed_the_item_limit(self) -> None:
        accepts("list", item_type="int", max_items=2, default=[1, 2])
        refuse("list", "3 items, the limit is 2", item_type="int", max_items=2, default=[1, 2, 3])

    def test_a_list_default_is_held_as_a_tuple(self) -> None:
        parsed = decl("list", item_type="int", default=[1, 2])

        assert isinstance(parsed, ListState)
        assert parsed.default == (1, 2)
        assert isinstance(parsed.default, tuple)


class TestJson:
    def test_the_default_is_null_or_any_json_within_the_limits(self) -> None:
        parsed = decl("json")

        assert isinstance(parsed, JsonState)
        assert parsed.default is None
        accepts("json", default={"a": [1, 2, {"b": None}]})
        deep: JsonValue = 1
        for _ in range(20):
            deep = [deep]
        refuse("json", "nested deeper than 16", default=deep)
        refuse("json", "outside", default=[2**60])


class TestPortTypes:
    def test_which_kinds_can_be_wired_and_as_what(self) -> None:
        ports = {kind: port_type_of(decl(kind, **MINIMAL[kind])) for kind in MINIMAL}

        assert ports == {
            "bool": None,
            "int": None,
            "float": None,
            "string": "text",
            "enum": None,
            "image": "image",
            "file": "file",
            "table": "table",
            "list": None,
            "json": "json",
        }


def value_of(kind: str, raw: object, **fields: JsonValue) -> object:
    return check_value(decl(kind, **{**MINIMAL[kind], **fields}), raw)


def refused(kind: str, raw: object, message: str = "", **fields: JsonValue) -> None:
    with pytest.raises(InvalidValueError, match=message):
        value_of(kind, raw, **fields)


class LyingInt(int):
    __slots__ = ()

    def __ge__(self, other: object) -> bool:
        return True

    __le__ = __ge__


class LyingStr(str):
    __slots__ = ()

    def __len__(self) -> int:
        return 0


class TestCheckValue:
    """`check_value` decides whether a value belongs to a declaration, for every kind."""

    def test_bool(self) -> None:
        assert value_of("bool", True) is True
        assert value_of("bool", False) is False
        for bad in (1, 0, "true", None, [True]):
            refused("bool", bad, "k is a bool")

    def test_int(self) -> None:
        assert value_of("int", 7) == 7
        assert value_of("int", -MAX_SAFE_INT) == -MAX_SAFE_INT
        for bad in (1.0, "1", True, False, None, LyingInt(1)):
            refused("int", bad, "k is an int")
        refused("int", MAX_SAFE_INT + 1, "within")
        refused("int", 10**5000, "within")

    def test_int_bounds_are_inclusive(self) -> None:
        for good in (0, 5, 10):
            assert value_of("int", good, minimum=0, maximum=10) == good
        refused("int", -1, "at least 0", minimum=0, maximum=10)
        refused("int", 11, "at most 10", minimum=0, maximum=10)

    def test_float_takes_a_float_or_an_int_and_returns_a_float(self) -> None:
        assert value_of("float", 1.5) == 1.5
        result = value_of("float", 2)
        assert result == 2.0
        assert type(result) is float
        for bad in ("1.5", True, None, float("nan"), float("inf"), float("-inf"), MAX_SAFE_INT + 1):
            refused("float", bad, "k is a finite number")

    def test_float_bounds_are_inclusive(self) -> None:
        for good in (0.0, 0.5, 1.0):
            assert value_of("float", good, default=0.5, minimum=0, maximum=1) == good
        refused("float", -0.0001, "at least", default=0.5, minimum=0, maximum=1)
        refused("float", 1.0001, "at most", default=0.5, minimum=0, maximum=1)

    def test_string(self) -> None:
        assert value_of("string", "") == ""
        assert value_of("string", "tab\there\ufeff\u202e") == "tab\there\ufeff\u202e"
        for bad in (1, None, b"x", ["x"], LyingStr("x" * 5000)):
            refused("string", bad, "k is a string")
        refused("string", "a\0b", "NUL")
        refused("string", "a\ud800", "lone surrogate")

    def test_string_length_is_in_characters_and_inclusive(self) -> None:
        assert value_of("string", "é" * 3, max_length=3, default="") == "é" * 3
        refused("string", "é" * 4, "4 characters, the limit is 3", max_length=3, default="")

    def test_enum(self) -> None:
        assert value_of("enum", "b") == "b"
        for bad in ("c", "A", "", 1, None, LyingStr("a")):
            refused("enum", bad, "one of 2 options")

    def test_image_and_file(self) -> None:
        image = ImageRef.model_validate({"path": "a.png"})

        assert value_of("image", None) is None
        assert value_of("image", {"path": "a.png"}) == image
        assert value_of("image", image) == image
        assert value_of("file", {"path": "a/b.csv"}) == FileRef.model_validate({"path": "a/b.csv"})
        bad_images: list[object] = ["a.png", {"path": "/x.png"}, {"path": "../x.png"}]
        bad_images += [{"path": "a.svg"}, {"path": "https://x/a.png"}, 5, {}]
        for bad in bad_images:
            refused("image", bad, "^k: ")
        bad_files: list[object] = ["a.csv", {"path": "/x"}, {"path": "../x"}, {}]
        bad_files.append({"path": "file:/etc/passwd"})
        for bad in bad_files:
            refused("file", bad, "^k: ")

    def test_an_image_value_is_not_a_file_value(self) -> None:
        refused("image", FileRef.model_validate({"path": "a.png"}), "^k: ")
        refused("file", ImageRef.model_validate({"path": "a.png"}), "^k: ")

    def test_table(self) -> None:
        raw: Json = {"columns": COLUMN_A, "rows": [[1], [2]]}

        result = value_of("table", raw)

        assert isinstance(result, Table)
        assert result.rows == ((1,), (2,))
        wrong_cell: Json = {"columns": COLUMN_A, "rows": [["x"]]}
        bad_tables: list[object] = [None, "x", [], wrong_cell]
        for bad in bad_tables:
            refused("table", bad, "^k: ")

    def test_table_rows_are_capped_by_the_declaration(self) -> None:
        rows: list[JsonValue] = [[i] for i in range(3)]
        raw: Json = {"columns": COLUMN_A, "rows": rows}

        assert isinstance(value_of("table", raw, max_rows=3), Table)
        refused("table", raw, "3 rows, the limit is 2", max_rows=2)

    def test_list(self) -> None:
        assert value_of("list", [1, 2]) == (1, 2)
        assert value_of("list", (1, 2)) == (1, 2)
        assert value_of("list", [1, 2.5], item_type="float") == (1.0, 2.5)
        assert value_of("list", []) == ()
        assert value_of("list", ["a"], item_type="string") == ("a",)
        assert value_of("list", [True], item_type="bool") == (True,)
        for bad in (None, "ab", {1, 2}, {"a": 1}, b"ab"):
            refused("list", bad, "k is a list")

    @pytest.mark.parametrize(
        ("item_type", "first", "bad"),
        [
            ("int", 0, 1.5),
            ("int", 0, True),
            ("bool", True, 1),
            ("string", "", 1),
            ("float", 0.0, "1"),
        ],
    )
    def test_a_list_item_must_be_exactly_the_item_type(
        self, item_type: str, first: object, bad: object
    ) -> None:
        refused("list", [first, bad], rf"k\[1\] must be a {item_type}", item_type=item_type)

    def test_list_is_capped_by_the_declaration_and_its_items_by_the_cell_limit(self) -> None:
        assert value_of("list", [1, 2], max_items=2) == (1, 2)
        refused("list", [1, 2, 3], "3 items, the limit is 2", max_items=2)
        refused("list", ["x" * 1001], r"k\[0\] must be a string", item_type="string")

    def test_a_list_subclass_is_not_a_list(self) -> None:
        class Sub(list[int]):
            pass

        refused("list", Sub([1]), "k is a list")

    def test_json(self) -> None:
        assert value_of("json", {"a": [1, None]}) == {"a": [1, None]}
        assert value_of("json", None) is None
        refused("json", [2**60], "^k: ")
        refused("json", (1, 2), "^k: ")
        refused("json", object(), "^k: ")

    def test_the_error_names_the_key_and_a_short_excerpt_of_a_long_value(self) -> None:
        with pytest.raises(InvalidValueError) as raised:
            value_of("int", "9" * 10_000)

        assert "k is an int" in str(raised.value)
        assert len(str(raised.value)) < 200

    def test_the_result_is_typed_not_the_input_object(self) -> None:
        raw = [1, 2]

        result = value_of("list", raw)

        assert result == (1, 2)
        assert not isinstance(result, list)
