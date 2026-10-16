"""Value types: image and file refs, tables, JSON data, port types."""

import json
import unicodedata

import pytest
from pydantic import TypeAdapter, ValidationError

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import JsonValue, WorkspaceRelPath
from tumbleweed.ui.spec import values
from tumbleweed.ui.spec.limits import (
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_SAFE_INT,
    MAX_STRING_LENGTH,
    MAX_TABLE_CELLS,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
)
from tumbleweed.ui.spec.values import FileRef, ImageRef, JsonData, Table

# Paths that must never be a reference, with the rule that refuses each (from WorkspaceRelPath
# in `tumbleweed.core.types`, or from the reference types on top of it).
NOT_RELATIVE_WORKSPACE_PATHS = {
    "empty": "",
    "absolute": "/etc/passwd.png",
    "parent": "../secret.png",
    "parent inside": "a/../../secret.png",
    "only parent": "..",
    "dot": "./a.png",
    "dot inside": "a/./b.png",
    "empty component": "a//b.png",
    "trailing slash": "a/b.png/",
    "NUL": "a\0.png",
    "newline": "a\nb.png",
    "bidi override": "a\u202e.png",
    "CSI": "a\x9b.png",
    "http URL": "http://example.com/a.png",
    "https URL, one slash": "https:/example.com/a.png",
    "file URL": "file:///etc/passwd.png",
    "file URL, one slash": "file:/etc/passwd.png",
    "data URI": "data:image/png;base64,iVBORw0KGgo.png",
    "drive letter": "C:\\Users\\a.png",
    "scheme in first component": "x:y/a.png",
    "component over NAME_MAX": "a" * 256 + ".png",
    "path over PATH_MAX": "/".join(["a" * 200] * 21) + ".png",
}
# A colon is fine after the first component: `a/b:c.png` is a file called `b:c.png`.
COLON_BEYOND_THE_FIRST_COMPONENT = "photos/2026-10-16 12:30.png"


class TestRefsAreWorkspaceRelativeAndNothingElse:
    @pytest.mark.parametrize("ref", [FileRef, ImageRef])
    @pytest.mark.parametrize(
        "path", NOT_RELATIVE_WORKSPACE_PATHS.values(), ids=NOT_RELATIVE_WORKSPACE_PATHS
    )
    def test_refuses_paths_that_are_not_plain_workspace_paths(
        self, ref: type[FileRef | ImageRef], path: str
    ) -> None:
        with pytest.raises(ValidationError):
            ref.model_validate({"path": path})
        with pytest.raises(ValidationError):
            ref.model_validate_json(json.dumps({"path": path}))

    @pytest.mark.parametrize(
        "path", ["a.png", "out/adjusted.PNG", "a/b/c.jpg", "x.jpeg", "p.webp", "é/ü.png"]
    )
    def test_accepts_relative_paths_to_raster_images(self, path: str) -> None:
        assert str(ImageRef.model_validate({"path": path}).path) == path

    def test_a_colon_after_the_first_component_is_a_file_name(self) -> None:
        assert str(FileRef.model_validate({"path": "a/b:c"}).path) == "a/b:c"
        assert (
            str(ImageRef.model_validate({"path": COLON_BEYOND_THE_FIRST_COMPONENT}).path)
            == COLON_BEYOND_THE_FIRST_COMPONENT
        )

    @pytest.mark.parametrize("path", ["a.svg", "a.SVG", "a.gif", "a.bmp", "a.tiff", "a.ico", "a"])
    def test_an_image_is_png_jpeg_or_webp_by_extension_never_svg(self, path: str) -> None:
        with pytest.raises(ValidationError, match="raster formats only, never SVG"):
            ImageRef.model_validate({"path": path})

    def test_a_file_may_have_any_extension(self) -> None:
        for path in ("data.csv", "notes", "a.svg", "archive.tar.gz"):
            assert str(FileRef.model_validate({"path": path}).path) == path

    @pytest.mark.parametrize("ref", [FileRef, ImageRef])
    @pytest.mark.parametrize(
        "bad", ["a.png", None, 5, ["a.png"], {"file": "a.png"}, {"path": "a.png", "extra": 1}, {}]
    )
    def test_a_ref_is_an_object_with_exactly_a_path(
        self, ref: type[FileRef | ImageRef], bad: object
    ) -> None:
        with pytest.raises(ValidationError):
            ref.model_validate(bad)

    @pytest.mark.parametrize("ref", [FileRef, ImageRef])
    def test_the_path_is_a_workspace_rel_path_and_dumps_to_its_text(
        self, ref: type[FileRef | ImageRef]
    ) -> None:
        parsed = ref.model_validate_json('{"path": "a/b.png"}')

        assert type(parsed.path) is WorkspaceRelPath
        assert parsed.model_dump(mode="json") == {"path": "a/b.png"}
        assert ref(path=WorkspaceRelPath("a/b.png")) == parsed

    @pytest.mark.parametrize("ref", [FileRef, ImageRef])
    def test_refs_are_immutable(self, ref: type[FileRef | ImageRef]) -> None:
        parsed = ref.model_validate({"path": "a.png"})

        with pytest.raises(ValidationError, match="frozen"):
            parsed.path = WorkspaceRelPath("b.png")  # type: ignore[misc]  # the point of the test

    def test_an_image_ref_is_not_a_file_ref(self) -> None:
        image: object = ImageRef.model_validate({"path": "a.png"})
        file: object = FileRef.model_validate({"path": "a.png"})

        assert image != file


def table(columns: list[tuple[str, str]], rows: list[list[object]]) -> Table:
    return Table.model_validate(
        {"columns": [{"name": name, "type": kind} for name, kind in columns], "rows": rows}
    )


class TestTable:
    def test_the_empty_table_is_valid(self) -> None:
        empty = Table()

        assert empty.columns == ()
        assert empty.rows == ()
        assert Table.model_validate({}) == empty

    def test_typed_columns_and_rows(self) -> None:
        parsed = table(
            [("name", "string"), ("age", "int"), ("score", "float"), ("ok", "bool")],
            [["Ann", 30, 1.5, True], ["Bob", 25, 2, False]],
        )

        assert [column.type for column in parsed.columns] == ["string", "int", "float", "bool"]
        assert parsed.rows == (("Ann", 30, 1.5, True), ("Bob", 25, 2, False))

    def test_json_text_round_trips_and_is_deterministic(self) -> None:
        text = '{"columns":[{"name":"a","type":"int"}],"rows":[[1],[2]]}'

        parsed = Table.model_validate_json(text)

        assert parsed.model_dump_json() == text
        assert Table.model_validate_json(parsed.model_dump_json()) == parsed

    @pytest.mark.parametrize(
        ("kind", "cell"),
        [
            ("string", 5),
            ("string", True),
            ("string", None),
            ("int", 1.5),
            ("int", "1"),
            ("int", True),
            ("int", None),
            ("float", "1.5"),
            ("float", True),
            ("float", None),
            ("bool", 1),
            ("bool", "true"),
            ("bool", None),
        ],
    )
    def test_a_cell_must_be_of_its_columns_type(self, kind: str, cell: object) -> None:
        with pytest.raises(ValidationError):
            table([("c", kind)], [[cell]])

    def test_an_int_cell_of_a_float_column_is_stored_as_a_float(self) -> None:
        mixed = table([("a", "float"), ("b", "int")], [[2, 3], [2.5, 4]])
        spelt_out = table([("a", "float"), ("b", "int")], [[2.0, 3], [2.5, 4]])

        assert [type(cell) for cell in mixed.rows[0]] == [float, int]
        assert mixed == spelt_out
        # Two spellings of one table are one set of bytes.
        assert mixed.model_dump_json() == spelt_out.model_dump_json()

    def test_an_int_is_a_float_but_not_the_other_way(self) -> None:
        table([("c", "float")], [[2]])
        with pytest.raises(ValidationError, match="is of type int"):
            table([("c", "int")], [[2.0]])

    def test_an_int_too_big_to_be_exact_is_not_an_int_cell(self) -> None:
        table([("c", "int")], [[MAX_SAFE_INT]])
        with pytest.raises(ValidationError, match="is of type int"):
            table([("c", "int")], [[MAX_SAFE_INT + 1]])

    def test_the_error_names_the_row_and_column_not_the_cell(self) -> None:
        with pytest.raises(ValidationError, match=r"row 1, column 'b' is of type int, got str"):
            table([("a", "int"), ("b", "int")], [[1, 2], [3, "secret " * 50]])

    @pytest.mark.parametrize("rows", [[[1, 2]], [[]], [[1], [1, 2]]])
    def test_a_row_has_one_cell_per_column(self, rows: list[list[object]]) -> None:
        with pytest.raises(ValidationError, match=r"has .* cells for 1 columns"):
            table([("a", "int")], rows)

    def test_a_table_without_columns_has_no_rows(self) -> None:
        with pytest.raises(ValidationError, match="no columns has no rows"):
            table([], [[]])

    def test_column_names_are_distinct_also_after_nfc(self) -> None:
        composed = unicodedata.normalize("NFC", "e\u0301")
        decomposed = unicodedata.normalize("NFD", composed)
        assert composed != decomposed
        with pytest.raises(ValidationError, match="distinct"):
            table([("a", "int"), ("a", "int")], [])
        with pytest.raises(ValidationError, match="distinct"):
            table([(composed, "int"), (decomposed, "int")], [])

    @pytest.mark.parametrize("name", ["", "a\0b", "\ud800"])
    def test_a_column_name_is_non_empty_data_text(self, name: str) -> None:
        with pytest.raises(ValidationError):
            table([(name, "int")], [])

    def test_a_cell_is_data_text(self) -> None:
        cell = "\ufeff tab\t, CRLF\r\n"
        assert table([("a", "string")], [[cell]]).rows == ((cell,),)
        with pytest.raises(ValidationError, match="NUL"):
            table([("a", "string")], [["a\0b"]])

    @pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e999"])
    def test_a_float_cell_is_finite(self, raw: str) -> None:
        text = f'{{"columns":[{{"name":"a","type":"float"}}],"rows":[[{raw}]]}}'
        with pytest.raises(ValidationError):
            Table.model_validate_json(text)

    def test_unknown_column_types_and_fields_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            table([("a", "date")], [])
        with pytest.raises(ValidationError):
            Table.model_validate({"columns": [{"name": "a", "type": "int", "width": 3}]})
        with pytest.raises(ValidationError):
            Table.model_validate({"rows": [], "extra": 1})

    def test_a_table_is_immutable_all_the_way_down(self) -> None:
        parsed = table([("a", "int")], [[1]])

        assert isinstance(parsed.columns, tuple)
        assert isinstance(parsed.rows, tuple)
        assert isinstance(parsed.rows[0], tuple)
        with pytest.raises(ValidationError, match="frozen"):
            parsed.rows = ()  # type: ignore[misc]  # the point of the test

    def test_limits_on_columns_rows_and_cells(self) -> None:
        columns = [(f"c{i}", "int") for i in range(MAX_TABLE_COLUMNS)]
        assert len(table(columns, [[0] * MAX_TABLE_COLUMNS]).columns) == MAX_TABLE_COLUMNS
        with pytest.raises(ValidationError):
            table([*columns, ("one_more", "int")], [])
        assert len(table([("a", "int")], [[0]] * MAX_TABLE_ROWS).rows) == MAX_TABLE_ROWS
        with pytest.raises(ValidationError):
            table([("a", "int")], [[0]] * (MAX_TABLE_ROWS + 1))
        wide = [("a", "int"), ("b", "int"), ("c", "int"), ("d", "int")]
        assert len(table(wide, [[0] * 4] * (MAX_TABLE_CELLS // 4)).rows) == MAX_TABLE_CELLS // 4
        with pytest.raises(ValidationError, match="more than 20000 cells"):
            table(wide[:3], [[0] * 3] * (MAX_TABLE_CELLS // 3 + 1))


JSON = TypeAdapter(JsonData)


def nested(depth: int) -> JsonValue:
    """A JSON value of exactly `depth` levels (a scalar is depth 1)."""
    value: JsonValue = 1
    for _ in range(depth - 1):
        value = [value]
    return value


class TestJsonData:
    @pytest.mark.parametrize(
        "value",
        [None, True, 0, -5, 1.5, "text", [], {}, [1, "a", None, [True]], {"a": {"b": [1, 2]}}],
    )
    def test_accepts_json(self, value: JsonValue) -> None:
        assert JSON.validate_python(value) == value

    def test_depth_is_bounded(self) -> None:
        assert JSON.validate_python(nested(MAX_JSON_DEPTH))
        with pytest.raises(ValidationError, match="nested deeper than 16"):
            JSON.validate_python(nested(MAX_JSON_DEPTH + 1))

    def test_a_dict_counts_as_a_level_like_a_list(self) -> None:
        value: JsonValue = 1
        for _ in range(MAX_JSON_DEPTH - 1):
            value = {"k": value}
        assert JSON.validate_python(value)
        with pytest.raises(ValidationError, match="nested deeper"):
            JSON.validate_python({"k": value})

    def test_node_count_is_bounded_and_counts_keys(self) -> None:
        # Each member of a dict is a key and a value: 2 nodes. The dict itself is 1.
        fits: JsonValue = {str(i): 0 for i in range((MAX_JSON_NODES - 1) // 2)}
        assert JSON.validate_python(fits)
        too_many: JsonValue = {str(i): 0 for i in range((MAX_JSON_NODES - 1) // 2 + 1)}
        with pytest.raises(ValidationError, match="more than 10000 nodes"):
            JSON.validate_python(too_many)
        assert JSON.validate_python([0] * (MAX_JSON_NODES - 1))
        with pytest.raises(ValidationError, match="more than 10000 nodes"):
            JSON.validate_python([0] * MAX_JSON_NODES)

    def test_a_very_deep_value_is_refused_without_recursing(self) -> None:
        with pytest.raises(ValidationError):
            JSON.validate_python(nested(100_000))

    def test_strings_and_keys_are_data_text_of_bounded_length(self) -> None:
        assert JSON.validate_python("x" * MAX_STRING_LENGTH)
        assert JSON.validate_python({"k" * MAX_STRING_LENGTH: 1})
        with pytest.raises(ValidationError, match="longer than 65536"):
            JSON.validate_python("x" * (MAX_STRING_LENGTH + 1))
        with pytest.raises(ValidationError, match="longer than 65536"):
            JSON.validate_python({"k" * (MAX_STRING_LENGTH + 1): 1})
        with pytest.raises(ValidationError, match="NUL"):
            JSON.validate_python(["a\0"])
        with pytest.raises(ValidationError, match="NUL"):
            JSON.validate_python({"a\0": 1})

    def test_integers_are_safe_and_floats_finite(self) -> None:
        assert JSON.validate_python([MAX_SAFE_INT, -MAX_SAFE_INT])
        for bad in (MAX_SAFE_INT + 1, -MAX_SAFE_INT - 1, 10**30):
            with pytest.raises(ValidationError, match="outside"):
                JSON.validate_python([bad])
        for bad_float in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(ValidationError, match="finite"):
                JSON.validate_python({"x": bad_float})

    def test_what_is_not_json_is_refused(self) -> None:
        for bad in ((1, 2), {1, 2}, b"x", object(), {1: "a"}):
            with pytest.raises(ValidationError):
                JSON.validate_python(bad)

    def test_the_check_itself_refuses_what_pydantic_would_have_stopped_first(self) -> None:
        class Sub(dict[str, JsonValue]):
            pass

        for bad in ((1, 2), {1, 2}, object(), Sub(a=1), [(1, 2)]):
            with pytest.raises(InvalidValueError, match="is not a JSON value"):
                values.check_json(bad)  # type: ignore[arg-type]  # the point of the test
        assert values.check_json({"a": [1, None, True, 1.5, "x"]}) == {
            "a": [1, None, True, 1.5, "x"]
        }


class TestPortTypes:
    def test_the_five_value_types_of_composition(self) -> None:
        assert values.PORT_TYPES == ("text", "json", "table", "image", "file")

    def test_a_port_type_adapter_accepts_only_those(self) -> None:
        adapter = TypeAdapter(values.PortType)

        for port in values.PORT_TYPES:
            assert adapter.validate_python(port) == port
        for bad in ("number", "bool", "Text", "", None):
            with pytest.raises(ValidationError):
                adapter.validate_python(bad)
