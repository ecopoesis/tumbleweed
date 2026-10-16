"""Value types: image and file refs, tables, JSON data, port types."""

import json
import tracemalloc
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
    MAX_VALUE_TEXT_BYTES,
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
    "upper-case scheme": "HTTP:/example.com/a.png",
    "scheme with plus and dot": "git+ssh.v2:/host/a.png",
    "javascript": "javascript:alert(1).png",
    "mailto": "mailto:a@example.com.png",
    "scheme after blanks": " https:/example.com/a.png",
    "single-letter drive with a slash": "c:/Windows/a.png",
    "backslash": "a\\b.png",
    "backslash traversal": "..\\..\\secret.png",
    "backslash at the start": "\\server\\share\\a.png",
    "backslash after a colon": "report 12:30\\a.png",
    "component over NAME_MAX": "a" * 256 + ".png",
    "path over PATH_MAX": "/".join(["a" * 200] * 21) + ".png",
}
# A colon is a file name character unless it ends what looks like a URL scheme or a drive letter
# (a letter, then letters, digits, + . -, then the colon) at the start of the first component.
COLONS_THAT_ARE_JUST_FILE_NAMES = [
    "photos/2026-10-16 12:30.png",  # after the first component
    "report 12:30.png",  # a space before the colon: no scheme starts like that
    "2024-01-01T10:30:00.png",  # starts with a digit
    "12:30.png",
    "a b:c.png",
    "my_photo:1.png",  # an underscore is not a scheme character
    "caf\u00e9:1.png",  # nor is a letter outside ASCII
    ".hidden:1.png",
]


# Names that an option parser, `expanduser`, a URL decoder, a glob or a shell would each treat as
# something other than a file name. They are valid references; opening is what must stay plain.
NAMES_THAT_ARE_SPECIAL_TO_SOMETHING_ELSE = [
    "-rf.png",  # an option to most programs
    "--help",
    "~root/x.png",  # `expanduser` turns it into root's home
    "~/x.png",
    "%2e%2e/x.png",  # a URL decoder turns it into `../x.png`
    "%2fetc%2fpasswd",
    "$HOME/x.png",
    "*.png",
    "a;b.png",
    "`id`.png",
    "a b.png",
    "x.png ",  # the trailing blank survives
    "dir/-rf.png",
]


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

    @pytest.mark.parametrize("path", COLONS_THAT_ARE_JUST_FILE_NAMES)
    def test_a_colon_that_ends_no_url_scheme_is_a_file_name(self, path: str) -> None:
        assert str(ImageRef.model_validate({"path": path}).path) == path
        assert str(FileRef.model_validate({"path": path}).path) == path
        assert str(FileRef.model_validate({"path": "a/b:c"}).path) == "a/b:c"

    def test_nothing_that_parses_as_a_url_gets_through(self) -> None:
        # The other direction of the test above: a URL has a scheme before its first slash.
        urls = [
            "http://h/a.png",
            "https:/h/a.png",
            "HTTPS:h/a.png",
            "ftp:a.png",
            "file:/etc/passwd",
            "data:image/png;base64,AAAA",
            "javascript:alert(1)",
            "x:a.png",
            "s3+x.y-z:bucket/a.png",
            "\t".join(["", "http:/h/a.png"]).replace("\t", " "),
        ]
        ours = 0
        for url in urls:
            with pytest.raises(ValidationError):
                FileRef.model_validate({"path": url})
            try:
                WorkspaceRelPath(url)
            except InvalidValueError:
                continue  # `//` and the like: WorkspaceRelPath refuses it already
            ours += 1
            with pytest.raises(ValidationError, match="URL or a drive"):
                FileRef.model_validate({"path": url})
        assert ours >= 6  # the rule is what stops most of these

    @pytest.mark.parametrize("path", NAMES_THAT_ARE_SPECIAL_TO_SOMETHING_ELSE)
    def test_a_name_that_means_something_to_a_shell_or_a_url_is_a_valid_file_name(
        self, path: str
    ) -> None:
        # On purpose: each is a legal Linux file name, so the check that refuses a URL or a
        # drive must not refuse these. What keeps them harmless is how a reference is opened (a
        # directory file descriptor, never `expanduser`, a URI or an argv without `--`), which
        # `docs/ui-spec.md` section 6 states and S11 tests with exactly these names.
        assert str(FileRef.model_validate({"path": path}).path) == path
        assert str(FileRef.model_validate_json(json.dumps({"path": path})).path) == path

    def test_a_refused_ref_is_refused_before_it_is_ever_a_path(self) -> None:
        for path in ("a\\b.png", "a/b\\c.png"):
            with pytest.raises(ValidationError, match="backslash"):
                FileRef.model_validate({"path": path})

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

    @pytest.mark.parametrize("value", [2**53 - 1, 2**53, 10**16, 10**20, 10**22, -(2**53), 2**100])
    def test_an_integer_is_valid_when_some_double_equals_it(self, value: int) -> None:
        assert JSON.validate_python([value, {"k": value}]) == [value, {"k": value}]

    @pytest.mark.parametrize("value", [2**53 + 1, -(2**53) - 1, 10**16 + 1, 10**23 + 1])
    def test_an_integer_no_double_equals_is_refused_not_rounded(self, value: int) -> None:
        with pytest.raises(ValidationError, match="no double is equal"):
            JSON.validate_python([value])

    def test_an_integer_too_large_for_any_double_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="too large for a double"):
            JSON.validate_python([10**400])

    @pytest.mark.parametrize(
        ("spelling", "double"),
        [
            (1152921504606847000, float(2**60)),
            (-1152921504606847000, -float(2**60)),
            (295147905179352830000, float(2**68)),
        ],
    )
    def test_an_integer_that_is_how_canonical_json_spells_a_double_is_that_double(
        self, spelling: int, double: float
    ) -> None:
        checked = values.check_json([spelling, {"k": spelling}])

        assert checked == [double, {"k": double}]
        assert isinstance(checked, list)
        assert type(checked[0]) is float
        assert JSON.validate_json(f"[{spelling}]") == [double]

    def test_an_integer_beyond_the_safe_range_is_stored_as_a_float_and_the_others_stay_ints(
        self,
    ) -> None:
        checked = values.check_json([2**53 - 1, -(2**53 - 1), 2**53, 2**60, 5, {"k": 2**70}])

        assert isinstance(checked, list)
        assert [type(item) for item in checked] == [int, int, float, float, int, dict]
        assert checked[:5] == [2**53 - 1, -(2**53 - 1), 2**53, 2**60, 5]
        assert checked[5] == {"k": float(2**70)}

    def test_a_value_without_integers_beyond_the_safe_range_is_returned_as_it_came(self) -> None:
        value: JsonValue = {"a": [1, 2.5, "x", None, True]}

        assert values.check_json(value) is value

    def test_floats_are_finite(self) -> None:
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
                values.check_json(bad)
        assert values.check_json({"a": [1, None, True, 1.5, "x"]}) == {
            "a": [1, None, True, 1.5, "x"]
        }


class TestObjectKeysAreStrings:
    """A key that is not a string is a refusal, never a crash (R2-F2)."""

    BAD_KEYS: tuple[object, ...] = (1, 1.5, True, None, b"k", (1, 2), frozenset({1}), object())

    @pytest.mark.parametrize("key", BAD_KEYS, ids=lambda k: type(k).__name__)
    def test_check_json_refuses_a_key_that_is_not_a_string(self, key: object) -> None:
        for value in ({key: 1}, [{key: 1}], {"a": {"b": [{key: 1}]}}):
            with pytest.raises(InvalidValueError, match="a key of a JSON object must be a string"):
                values.check_json(value)

    def test_a_str_subclass_is_not_a_string_key_either(self) -> None:
        class Key(str):
            __slots__ = ()

        with pytest.raises(InvalidValueError, match="must be a string"):
            values.check_json({Key("k"): 1})

    def test_the_message_does_not_echo_what_the_key_was(self) -> None:
        with pytest.raises(InvalidValueError) as raised:
            values.check_json({10**5000: 1})

        assert len(str(raised.value)) < 100

    @pytest.mark.parametrize("key", BAD_KEYS, ids=lambda k: type(k).__name__)
    def test_the_models_refuse_it_with_a_validation_error(self, key: object) -> None:
        with pytest.raises(ValidationError):
            JSON.validate_python({key: 1})


class TestTheWalkStopsAtTheFirstLimitCrossed:
    def test_the_text_budget_is_checked_at_each_key_not_after_the_whole_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        looked_at = 0
        real = values._json_text  # noqa: SLF001 -- counting calls of the function under test

        def counting(text: str) -> int:
            nonlocal looked_at
            looked_at += 1
            return real(text)

        monkeypatch.setattr(values, "_json_text", counting)
        piece = 60_000
        keys = {f"{number:04d}" + "x" * piece: 0 for number in range(200)}  # 12 MB of keys

        with pytest.raises(InvalidValueError, match="bytes of text"):
            values.check_json(keys)

        # 18 keys fill the budget and the 19th crosses it: not all 200.
        assert looked_at == MAX_VALUE_TEXT_BYTES // (piece + 4) + 1

    def test_a_list_that_cannot_fit_is_not_unpacked(self) -> None:
        huge = [0] * 1_000_000
        tracemalloc.start()
        try:
            with pytest.raises(InvalidValueError, match="more than 10000 nodes"):
                values.check_json(huge)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        # Unpacking would put a million tuples on the stack: about 100 MB.
        assert peak < 1_000_000

    def test_an_object_that_cannot_fit_is_not_unpacked_either(self) -> None:
        huge = dict.fromkeys(map(str, range(300_000)), 0)
        tracemalloc.start()
        try:
            with pytest.raises(InvalidValueError, match="more than 10000 nodes"):
                values.check_json(huge)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert peak < 2_000_000


class TestNormalisationCollisions:
    """Names that are distinct as written but one after the tool store normalises text (S23)."""

    COMPOSED = unicodedata.normalize("NFC", "e\u0301")
    DECOMPOSED = unicodedata.normalize("NFD", COMPOSED)

    def test_the_two_spellings_really_differ(self) -> None:
        assert self.COMPOSED != self.DECOMPOSED
        assert unicodedata.normalize("NFC", self.DECOMPOSED) == self.COMPOSED

    def test_json_object_keys_must_be_distinct_after_nfc(self) -> None:
        with pytest.raises(ValidationError, match="two keys that are the same once normalised"):
            JSON.validate_python({self.COMPOSED: 1, self.DECOMPOSED: 2})
        with pytest.raises(ValidationError, match="two keys that are the same once normalised"):
            JSON.validate_python([{"outer": {self.COMPOSED: 1, self.DECOMPOSED: 2}}])

    def test_json_object_keys_must_be_distinct_after_line_feeds_too(self) -> None:
        with pytest.raises(ValidationError, match="two keys that are the same once normalised"):
            JSON.validate_python({"a\nb": 1, "a\r\nb": 2})
        with pytest.raises(ValidationError, match="two keys that are the same once normalised"):
            JSON.validate_python({"a\nb": 1, "a\rb": 2})

    def test_keys_that_stay_distinct_are_fine(self) -> None:
        assert JSON.validate_python({"a": 1, "A": 2, self.COMPOSED: 3, "e": 4})
        assert JSON.validate_python({"x": {"k": 1}, "y": {"k": 2}})  # one object's keys at a time

    def test_the_message_quotes_a_short_excerpt_of_the_key(self) -> None:
        key = "k" * 5000
        with pytest.raises(ValidationError) as raised:
            JSON.validate_python({key + "e\u0301": 1, key + "\u00e9": 2})

        assert len(str(raised.value)) < 800

    def test_table_column_names_must_be_distinct_after_nfc_and_line_feeds(self) -> None:
        for first, second in [
            (self.COMPOSED, self.DECOMPOSED),
            ("a\nb", "a\r\nb"),
            ("a\nb", "a\rb"),
            ("x" + self.DECOMPOSED + "\r\ny", "x" + self.COMPOSED + "\ny"),
        ]:
            with pytest.raises(ValidationError, match="column names must be distinct"):
                table([(first, "int"), (second, "int")], [])

    def test_columns_that_stay_distinct_are_fine(self) -> None:
        assert table([("a", "int"), ("A", "int"), ("a b", "int"), ("a\nb", "int")], []).columns


class TestACellIsOneCheck:
    """A cell is a string, a number or a bool: one error and one message, however it fails."""

    @pytest.mark.parametrize("cell", [None, [], {}, "a\0b", "x" * 1001, float("nan"), 2**53 + 1])
    def test_a_bad_cell_is_one_error_at_its_place(self, cell: object) -> None:
        rows: list[list[object]] = [[0], [1], [cell]]

        with pytest.raises(ValidationError) as raised:
            table([("a", "int")], rows)

        assert raised.value.error_count() == 1
        assert raised.value.errors()[0]["loc"] == ("rows", 2, 0)

    def test_the_message_says_what_a_cell_is(self) -> None:
        with pytest.raises(ValidationError, match="expected a string, a number or a bool"):
            table([("a", "int")], [[None]])

    def test_many_bad_cells_make_one_error_not_one_each(self) -> None:
        bad: list[list[object]] = [[None]] * 500

        with pytest.raises(ValidationError) as raised:
            table([("a", "int")], bad)

        assert raised.value.error_count() == 1

    def test_a_cell_has_no_union_noise(self) -> None:
        with pytest.raises(ValidationError) as raised:
            table([("a", "int")], [[[]]])

        text = str(raised.value)
        assert "valid boolean" not in text
        assert "valid integer" not in text
        assert "valid number" not in text


class TestCellNumbers:
    """Numbers in a table follow the one rule: ints within 2**53 - 1, floats any exact double."""

    def test_an_int_column_takes_safe_integers_only(self) -> None:
        assert table([("a", "int")], [[2**53 - 1], [-(2**53 - 1)]]).rows
        for bad in (2**53, 10**16, 10**20):
            with pytest.raises(ValidationError, match="beyond"):
                table([("a", "int")], [[bad]])
        with pytest.raises(ValidationError, match="no double is equal"):
            table([("a", "int")], [[2**53 + 1]])

    @pytest.mark.parametrize("value", [2**53, 10**16, 10**20, 2**100])
    def test_a_float_column_takes_an_integer_that_is_exactly_a_double(self, value: int) -> None:
        parsed = table([("a", "float")], [[value]])

        assert parsed.rows == ((float(value),),)
        assert type(parsed.rows[0][0]) is float

    @pytest.mark.parametrize(
        ("spelling", "double"),
        [(1152921504606847000, float(2**60)), (295147905179352830000, float(2**68))],
    )
    def test_a_float_column_takes_the_spelling_of_a_double_and_stores_the_double(
        self, spelling: int, double: float
    ) -> None:
        parsed = table([("a", "float")], [[spelling]])

        assert parsed.rows == ((double,),)
        assert type(parsed.rows[0][0]) is float
        with pytest.raises(ValidationError, match="beyond"):
            table([("a", "int")], [[spelling]])

    @pytest.mark.parametrize("value", [2**53 + 1, 10**16 + 1, 2**60 + 1, 10**400])
    def test_a_float_column_refuses_an_integer_it_would_round(self, value: int) -> None:
        with pytest.raises(ValidationError):
            table([("a", "float")], [[value]])

    def test_a_string_column_takes_no_number_however_exact(self) -> None:
        with pytest.raises(ValidationError, match="is of type string"):
            table([("a", "string")], [[10**16]])


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
