"""The committed JSON Schema matches the models, and says what the models enforce."""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import ClassVar

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel

from tests.ui.spec.support import (
    COMPONENT_KINDS,
    EXPR_KINDS,
    HANDLER_KINDS,
    MINIMAL_STATE,
    STATE_KINDS,
    build,
    golden,
    minimal,
)
from tumbleweed.core.types import JsonValue
from tumbleweed.ui.spec import limits, schema
from tumbleweed.ui.spec.errors import SpecError
from tumbleweed.ui.spec.parse import parse_ui_spec
from tumbleweed.ui.spec.schema import SCHEMA_FILE, main, schema_text, ui_spec_schema

ROOT = Path(__file__).resolve().parents[3]
COMMITTED = ROOT / SCHEMA_FILE
REGENERATE = "uv run python -m tumbleweed.ui.spec.schema --write"


def definitions() -> dict[str, dict[str, JsonValue]]:
    defs = ui_spec_schema()["$defs"]
    assert isinstance(defs, dict)
    result: dict[str, dict[str, JsonValue]] = {}
    for name, definition in defs.items():
        assert isinstance(definition, dict)
        result[name] = definition
    return result


def prop(model: str, name: str, *, follow: bool = True) -> dict[str, JsonValue]:
    """A property of a model; a `$ref` to a shared definition is followed unless `follow` is off."""
    defs = definitions()
    properties = defs[model]["properties"]
    assert isinstance(properties, dict)
    found = properties[name]
    assert isinstance(found, dict)
    target = found.get("$ref")
    if follow and isinstance(target, str):
        shared = defs[target.removeprefix("#/$defs/")]
        return {**shared, **{k: v for k, v in found.items() if k != "$ref"}}
    return found


class TestDrift:
    def test_the_committed_schema_is_what_the_models_produce(self) -> None:
        assert COMMITTED.read_text(encoding="utf-8") == schema_text(), (
            f"schemas/ui-spec.v1.json is out of date: run `{REGENERATE}` and commit it"
        )

    def test_it_is_where_the_other_tools_expect_it(self) -> None:
        assert Path("schemas") / "ui-spec.v1.json" == SCHEMA_FILE
        assert COMMITTED.is_file()

    def test_regenerating_is_deterministic(self) -> None:
        assert schema_text() == schema_text()
        assert schema_text().endswith("}\n")

    def test_it_is_valid_json_with_a_stable_identity(self) -> None:
        loaded = json.loads(COMMITTED.read_text(encoding="utf-8"))

        assert loaded["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert loaded["$id"] == "urn:tumbleweed:ui-spec:v1"
        assert loaded["title"] == "tumbleweed.ui/1"
        assert loaded["properties"]["version"]["const"] == "tumbleweed.ui/1"
        assert set(loaded["required"]) == {"version", "title", "root"}
        assert loaded["additionalProperties"] is False


class TestCommandLine:
    def test_check_passes_on_the_repository(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(ROOT)

        assert main([]) == 0
        assert main(["--check"]) == 0

    def test_write_then_check_in_a_scratch_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "schemas").mkdir()
        monkeypatch.chdir(tmp_path)

        assert main(["--check"]) == 1  # nothing written yet
        assert "missing or out of date" in capsys.readouterr().err
        assert main(["--write"]) == 0
        assert (tmp_path / SCHEMA_FILE).read_text(encoding="utf-8") == schema_text()
        assert main(["--check"]) == 0

    def test_check_fails_and_says_so_when_the_file_is_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "schemas").mkdir()
        shutil.copy(COMMITTED, tmp_path / SCHEMA_FILE)
        monkeypatch.chdir(tmp_path)
        stale = (
            (tmp_path / SCHEMA_FILE)
            .read_text(encoding="utf-8")
            .replace('"maxItems": 8', '"maxItems": 9')
        )
        (tmp_path / SCHEMA_FILE).write_text(stale, encoding="utf-8")

        assert main(["--check"]) == 1
        assert "out of date: run with --write" in capsys.readouterr().err

    def test_bad_usage_and_the_wrong_directory_are_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)

        assert main(["--wat"]) == 2
        assert main(["--check", "--write"]) == 2
        assert main(["--write"]) == 2  # no schemas/ here
        assert "run this from the repository root" in capsys.readouterr().err
        assert not (tmp_path / "schemas").exists()


class TestAsAModule:
    """`python -m tumbleweed.ui.spec.schema` is the command the docs and the test failures name."""

    @staticmethod
    def run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603  (argv list, no shell; our own interpreter)
            [sys.executable, "-I", "-m", "tumbleweed.ui.spec.schema", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def test_check_passes_in_the_repository(self) -> None:
        result = self.run(ROOT, "--check")

        assert (result.returncode, result.stderr) == (0, "")

    def test_write_then_check_in_a_scratch_directory(self, tmp_path: Path) -> None:
        (tmp_path / "schemas").mkdir()

        assert self.run(tmp_path, "--check").returncode == 1
        assert self.run(tmp_path, "--write").returncode == 0
        assert (tmp_path / SCHEMA_FILE).read_text(encoding="utf-8") == schema_text()
        assert self.run(tmp_path, "--check").returncode == 0

    def test_usage_errors_exit_with_two(self, tmp_path: Path) -> None:
        assert self.run(tmp_path, "--wat").returncode == 2


class TestTheSchemaDescribesTheModels:
    def test_every_kind_of_every_union_has_a_definition_with_its_tag(self) -> None:
        defs = definitions()
        unions = {
            "Expr": EXPR_KINDS,
            "StateDecl": STATE_KINDS,
            "Component": COMPONENT_KINDS,
            "Handler": HANDLER_KINDS,
        }

        for union, kinds in unions.items():
            discriminator = defs[union]["discriminator"]
            assert isinstance(discriminator, dict)
            assert discriminator["propertyName"] == "kind"
            mapping = discriminator["mapping"]
            assert isinstance(mapping, dict)
            assert set(mapping) == set(kinds), union
            for kind, cls in kinds.items():
                assert mapping[kind] == f"#/$defs/{cls.__name__}"
                properties = defs[cls.__name__]["properties"]
                assert isinstance(properties, dict)
                assert properties["kind"] == {"const": kind, "title": "Kind", "type": "string"}
                assert "kind" in (defs[cls.__name__]["required"] or [])  # type: ignore[operator]  # a list

    def test_every_model_is_closed(self) -> None:
        tagged_unions = {"Expr", "StateDecl", "Component", "Handler"}
        for name, definition in definitions().items():
            if definition.get("type") == "object" and name not in tagged_unions:
                assert definition["additionalProperties"] is False, name

    def test_arrays_are_bounded_with_item_keywords_not_string_keywords(self) -> None:
        # `max_length` on a tuple must come out as `maxItems`. If the validators and the
        # constraint are annotated in the wrong order it comes out as `maxLength`, which a JSON
        # Schema validator ignores on an array.
        def walk(node: JsonValue, path: str) -> None:
            if isinstance(node, dict):
                if node.get("type") == "array":
                    assert "maxLength" not in node, path
                    assert "minLength" not in node, path
                for key, value in node.items():
                    walk(value, f"{path}/{key}")
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, f"{path}/{index}")

        walk(ui_spec_schema(), "#")

    def test_the_limits_are_in_the_schema(self) -> None:
        assert prop("Min", "args")["maxItems"] == limits.MAX_EXPR_ARGS
        assert prop("Min", "args")["minItems"] == 2
        assert prop("Box", "children")["maxItems"] == limits.MAX_CONTAINER_CHILDREN
        assert prop("Tabs", "pages")["maxItems"] == limits.MAX_CONTAINER_CHILDREN
        assert prop("Tabs", "pages")["minItems"] == 1
        assert prop("Table", "rows")["maxItems"] == limits.MAX_TABLE_ROWS
        assert prop("Table", "columns")["maxItems"] == limits.MAX_TABLE_COLUMNS
        assert prop("EnumState", "options")["maxItems"] == limits.MAX_ENUM_OPTIONS
        assert prop("SandboxHandler", "outputs")["maxItems"] == limits.MAX_HANDLER_ITEMS
        assert prop("SandboxHandler", "inputs")["maxItems"] == limits.MAX_HANDLER_ITEMS
        assert prop("SetHandler", "assignments")["maxItems"] == limits.MAX_HANDLER_ITEMS
        assert prop("SandboxHandler", "debounce_ms")["maximum"] == limits.MAX_DEBOUNCE_MS
        assert prop("FormatNumber", "decimals")["maximum"] == limits.MAX_DECIMALS
        assert prop("Grid", "columns")["maximum"] == limits.MAX_GRID_COLUMNS
        assert prop("StringState", "max_length")["maximum"] == limits.MAX_STRING_LENGTH
        assert prop("ListState", "max_items")["maximum"] == limits.MAX_LIST_ITEMS
        assert prop("TableState", "max_rows")["maximum"] == limits.MAX_TABLE_ROWS
        assert prop("IntState", "default")["maximum"] == limits.MAX_SAFE_INT
        assert prop("IntState", "default")["minimum"] == -limits.MAX_SAFE_INT
        assert prop("StrLit", "value")["maxLength"] == limits.MAX_TEXT_LENGTH
        assert prop("Button", "label")["maxLength"] == limits.MAX_LINE_LENGTH

    def test_the_title_is_bounded(self) -> None:
        properties = ui_spec_schema()["properties"]
        assert isinstance(properties, dict)
        title = properties["title"]
        assert isinstance(title, dict)

        assert title["maxLength"] == limits.MAX_TITLE_LENGTH
        assert title["minLength"] == 1

    @pytest.mark.parametrize(
        ("model", "field"),
        [
            ("Ref", "key"),
            ("IntState", "key"),
            ("SetAssignment", "target"),
            ("Entry", "bind"),
            ("SandboxHandler", "module"),
            ("SandboxHandler", "function"),
            ("SetHandler", "name"),
            ("Button", "on_click"),
        ],
    )
    def test_names_carry_the_pattern_of_the_name_types(self, model: str, field: str) -> None:
        found = prop(model, field)
        pattern = "^(?:[a-z][a-z0-9_]{0,63})$"

        assert found.get("pattern") == pattern or pattern in json.dumps(found)

    @pytest.mark.parametrize("model", ["ImageRef", "FileRef"])
    def test_a_workspace_ref_is_an_object_with_just_a_path(self, model: str) -> None:
        assert definitions()[model]["required"] == ["path"]
        assert prop(model, "path")["type"] == "string"

    @pytest.mark.parametrize(
        ("model", "field"), [("IntState", "computed"), ("SetAssignment", "value"), ("Add", "left")]
    )
    def test_where_an_expression_goes_the_schema_says_an_expression_not_a_string(
        self, model: str, field: str
    ) -> None:
        found = json.dumps(prop(model, field, follow=False))

        assert "#/$defs/Expr" in found
        assert '"type": "string"' not in found

    def test_the_module_exposes_what_the_system_prompt_needs(self) -> None:
        assert schema.ui_spec_schema() == json.loads(schema_text())


class TestTaggedUnionsDispatchOnTheirKind:
    """pydantic writes `oneOf` + `discriminator`, which generic validators do not understand."""

    UNIONS: ClassVar[dict[str, dict[str, type[BaseModel]]]] = {
        "Expr": EXPR_KINDS,
        "StateDecl": STATE_KINDS,
        "Component": COMPONENT_KINDS,
        "Handler": HANDLER_KINDS,
    }

    @pytest.mark.parametrize("union", UNIONS)
    def test_the_definition_is_if_then_on_the_kind_not_one_of(self, union: str) -> None:
        definition = definitions()[union]
        kinds = self.UNIONS[union]

        assert "oneOf" not in definition
        assert "anyOf" not in definition
        assert definition["required"] == ["kind"]
        assert definition["properties"] == {"kind": {"enum": sorted(kinds)}}  # as pydantic sorts
        branches = definition["allOf"]
        assert isinstance(branches, list)
        assert len(branches) == len(kinds)
        for branch, kind in zip(branches, sorted(kinds), strict=True):
            cls = kinds[kind]
            assert branch == {
                "if": {"properties": {"kind": {"const": kind}}, "required": ["kind"]},
                "then": {"$ref": f"#/$defs/{cls.__name__}"},
            }

    def test_no_definition_still_has_a_oneof_or_an_anyof_of_models(self) -> None:
        # What remains are anyOf of a scalar and null, or of scalars: never of two or more refs
        # to models, which a generic validator would try one by one.
        def walk(node: JsonValue, path: str) -> None:
            if isinstance(node, dict):
                for keyword in ("oneOf", "anyOf"):
                    branches = node.get(keyword)
                    if isinstance(branches, list):
                        refs = [b for b in branches if isinstance(b, dict) and "$ref" in b]
                        assert len(refs) <= 1, f"{path}/{keyword} tries {len(refs)} models"
                for key, value in node.items():
                    walk(value, f"{path}/{key}")
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, f"{path}/{index}")

        walk(ui_spec_schema(), "#")


def validator() -> Draft202012Validator:
    return Draft202012Validator(ui_spec_schema())


def chain(depth: int, wrap: str = "not") -> dict[str, JsonValue]:
    node: dict[str, JsonValue] = {"kind": "bool", "value": True}
    for _ in range(depth - 1):
        node = {"kind": wrap, "operand": node}
    return node


def with_expression(expression: dict[str, JsonValue]) -> dict[str, JsonValue]:
    entry: dict[str, JsonValue] = {
        "kind": "bool",
        "key": "b",
        "default": True,
        "computed": expression,
    }
    return build(state=[entry])


class TestAGenericValidatorCanUseTheSchema:
    """With `jsonschema` (a dev dependency): the schema is valid, quick and agrees on shape."""

    def test_it_is_a_valid_draft_2020_12_schema(self) -> None:
        Draft202012Validator.check_schema(ui_spec_schema())

    @pytest.mark.parametrize("name", ["tip_calculator", "photo_adjuster", "csv_viewer"])
    def test_the_golden_specs_pass(self, name: str) -> None:
        assert list(validator().iter_errors(golden(name))) == []

    def test_a_spec_of_every_kind_of_state_passes(self) -> None:
        for kind, fields in MINIMAL_STATE.items():
            entry: dict[str, JsonValue] = {"kind": kind, "key": "k", **fields}

            assert list(validator().iter_errors(build(state=[entry]))) == [], kind

    def test_a_nest_of_sixteen_nodes_is_validated_at_once(self) -> None:
        # With `oneOf` and an ignored `discriminator` each level tries all 25 kinds, and this
        # takes 25**15 steps: hours.
        started = time.perf_counter()
        errors = list(validator().iter_errors(with_expression(chain(16))))
        elapsed = time.perf_counter() - started

        assert errors == []
        assert elapsed < 1.0

    def test_a_wrong_kind_at_the_bottom_of_a_deep_nest_is_found_at_once_too(self) -> None:
        bad = chain(16)
        node = bad
        while node.get("operand") is not None:
            node = node["operand"]  # type: ignore[assignment]  # the nest is made of dicts
        node["kind"] = "nope"
        started = time.perf_counter()
        errors = list(validator().iter_errors(with_expression(bad)))
        elapsed = time.perf_counter() - started

        assert errors
        assert elapsed < 1.0

    @pytest.mark.parametrize(
        "data",
        [
            build({"kind": "nope"}),
            build({"kind": "box", "children": [{"kind": "nope"}]}),
            build(state=[{"kind": "nope", "key": "x", "default": 0}]),
            build(handlers=[{"kind": "nope", "name": "h"}]),
            with_expression({"kind": "nope"}),
            minimal(extra=1),
            build({"kind": "slider", "bind": "x", "extra": 1}),
            build(state=[{"kind": "int", "key": "x", "default": 1.5}]),
            build(state=[{"kind": "int", "key": "x", "default": 0, "computed": "1+1"}]),
        ],
        ids=[
            "unknown component",
            "unknown nested component",
            "unknown state kind",
            "unknown handler kind",
            "unknown expression kind",
            "extra top-level field",
            "extra field of a component",
            "float default of an int",
            "an expression written as a string",
        ],
    )
    def test_what_the_schema_can_see_is_wrong_is_refused(self, data: dict[str, JsonValue]) -> None:
        assert list(validator().iter_errors(data))

    def test_each_kind_dispatches_to_its_own_model_and_no_other(self) -> None:
        # A component with the fields of a button but the kind of a switch is not valid.
        wrong = build({"kind": "switch", "label": "Go", "on_click": "h"})

        assert list(validator().iter_errors(wrong))

    def test_the_schema_is_looser_than_the_models_where_it_says_it_is(self) -> None:
        # Documented in the module: JSON Schema cannot say what a workspace path may be.
        entry: dict[str, JsonValue] = {
            "kind": "image",
            "key": "i",
            "default": {"path": "/etc/x.png"},
        }
        data = build(state=[entry])

        assert list(validator().iter_errors(data)) == []
        with pytest.raises(SpecError):
            parse_ui_spec(json.dumps(data))
