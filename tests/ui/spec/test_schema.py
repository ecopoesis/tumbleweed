"""The committed JSON Schema matches the models, and says what the models enforce."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ui.spec.support import (
    COMPONENT_KINDS,
    EXPR_KINDS,
    HANDLER_KINDS,
    STATE_KINDS,
)
from tumbleweed.core.types import JsonValue
from tumbleweed.ui.spec import limits, schema
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
        for name, definition in definitions().items():
            if definition.get("type") == "object":
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
