"""The JSON Schema of `tumbleweed.ui/1`, generated from the models and committed.

`schemas/ui-spec.v1.json` is what other tools and the model's system prompt (S12) read; a drift
test fails when it no longer matches the models. After a change to the models, regenerate it:

    uv run python -m tumbleweed.ui.spec.schema --write

and `--check` (the default) says whether the committed file is current.

**The schema is looser than the models, and is not a validator.** It describes shape, types and
numeric and length bounds, and anything it rejects the models reject, but JSON Schema cannot
express the cross-reference rules (names that exist, expression types, cycles), the character
rules of text and workspace paths, or the size budgets. Those are in `docs/ui-spec.md` and
enforced by the models. It is a guide for writing a spec. Untrusted text, from the model, a
file or another process, must go through `parse_ui_spec` (and parsed JSON through
`validate_ui_spec`), never be checked against this schema alone.

**A tagged union is dispatched on its `kind`.** pydantic describes `Expr`, `StateDecl`,
`Component` and `Handler` as a `oneOf` of every kind plus a `discriminator`, which is an OpenAPI
keyword that generic validators (the `jsonschema` library, editors) ignore: they then try every
branch at every level, and a nest of 16 `not` nodes costs 25**16 steps. Here each is rewritten
as `if` `kind` is `x` `then` the schema of `x`, with the set of kinds in an `enum`, which a
validator checks in a single pass; the `discriminator` stays for tools that read it.
"""

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter

from tumbleweed.core.types import JsonValue
from tumbleweed.ui.spec.spec import SPEC_VERSION, UiSpec

SCHEMA_FILE: Final = Path("schemas") / "ui-spec.v1.json"  # relative to the repository root
_JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])


def _dispatch_on_kind(schema: dict[str, JsonValue]) -> None:
    """Rewrite each `oneOf` + `discriminator` definition to `if`/`then` on the `kind`."""
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)  # noqa: S101  (pydantic always writes $defs here)
    for definition in definitions.values():
        if not isinstance(definition, dict) or "discriminator" not in definition:
            continue
        discriminator = definition["discriminator"]
        assert isinstance(discriminator, dict)  # noqa: S101
        mapping = discriminator["mapping"]
        assert isinstance(mapping, dict)  # noqa: S101
        definition.pop("oneOf")
        definition["type"] = "object"
        definition["required"] = ["kind"]
        definition["properties"] = {"kind": {"enum": list(mapping)}}
        definition["allOf"] = [
            {
                "if": {"properties": {"kind": {"const": kind}}, "required": ["kind"]},
                "then": {"$ref": reference},
            }
            for kind, reference in mapping.items()
        ]


def ui_spec_schema() -> dict[str, JsonValue]:
    """The JSON Schema (validation mode) of a spec."""
    schema = _JSON_OBJECT.validate_python(UiSpec.model_json_schema(mode="validation"))
    _dispatch_on_kind(schema)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:tumbleweed:ui-spec:v1"
    schema["title"] = SPEC_VERSION
    return schema


def schema_text() -> str:
    """The schema as committed: two-space indent, a trailing newline."""
    return json.dumps(ui_spec_schema(), indent=2, ensure_ascii=False) + "\n"


def main(argv: Sequence[str]) -> int:
    """`--check` (default) reports drift; `--write` regenerates the file. Run from the repo root."""
    mode = argv[0] if argv else "--check"
    if mode not in {"--check", "--write"} or len(argv) > 1:
        sys.stderr.write("usage: python -m tumbleweed.ui.spec.schema [--check | --write]\n")
        return 2
    if not SCHEMA_FILE.parent.is_dir():
        sys.stderr.write(f"{SCHEMA_FILE.parent} not found: run this from the repository root\n")
        return 2
    text = schema_text()
    if mode == "--write":
        SCHEMA_FILE.write_text(text, encoding="utf-8")
        return 0
    if not SCHEMA_FILE.is_file() or SCHEMA_FILE.read_text(encoding="utf-8") != text:
        sys.stderr.write(f"{SCHEMA_FILE} is missing or out of date: run with --write\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
