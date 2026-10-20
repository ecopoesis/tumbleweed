"""The section of the system prompt that documents UI specs, generated from the schema and the docs.

The model has to write `tumbleweed.ui/1` specs that validate, so the system prompt carries the
reference. It is not typed in a second time: `build_section` makes it from

- the JSON Schema of the spec (`tumbleweed.ui.spec.schema`, generated from the models): one line
  per kind of component, state entry, handler and expression, with each field's type and bounds;
- the prose of `docs/ui-spec.md`, which carries what the schema cannot say (names, text and
  numbers, what each component binds and fires, the expression types, handlers, what is checked
  across the spec, the limits) and the tip calculator, a whole worked example. Sections are taken
  by their number; the generated regions of that page (kinds, limits, the example) come with them,
  already checked against the code by `tests/ui/spec/test_docs.py`.

The result is committed as `prompts/ui_spec_v1.md`, which the prompt loader appends to the system
prompt (`tumbleweed.agent.prompts`). A test fails when the file no longer matches what this module
makes from the current docs and models, so a change to either cannot leave the prompt behind:

    uv run python -m tumbleweed.agent.spec_prompt --write

and `--check` (the default) says whether the committed file is current. Run it from the repository
root.
"""

import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from tumbleweed.ui.spec.schema import ui_spec_schema

DOC_FILE: Final = Path("docs") / "ui-spec.md"
OUTPUT_FILE: Final = Path("src") / "tumbleweed" / "agent" / "prompts" / "ui_spec_v1.md"
SECTIONS: Final = (2, 3, 4, 5, 7, 8, 9, 10, 11)  # of the docs page, by number
_UNIONS: Final = (
    ("Components", "Component"),
    ("State entries", "StateDecl"),
    ("Handlers", "Handler"),
    ("Expressions", "Expr"),
)
_SECTION: Final = re.compile(r"^## (\d+)\. (.+)$", re.MULTILINE)
_COMMENT: Final = re.compile(r"<!--.*?-->\n?", re.DOTALL)
_LIMIT_VALUE: Final = re.compile(r"<!-- limit: [A-Z_]+ -->(.*?)<!-- /limit -->")
_LINK: Final = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_NAME_PATTERN: Final = "^(?:[a-z][a-z0-9_]"
_PLAIN_TYPES: Final = frozenset({"array", "integer", "number", "string"})


def _object(value: JsonValue | None) -> Mapping[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError("the schema does not have the shape this generator reads")
    return value


def _range(low: JsonValue | None, high: JsonValue | None) -> str:
    if low is None and high is None:
        return ""
    return f"({'' if low is None else low}..{'' if high is None else high})"


def _type(node: Mapping[str, JsonValue], definitions: Mapping[str, JsonValue]) -> str:
    """A compact name for the type a schema node describes, with its bounds.

    A named definition that is a plain value (a list, a number, an enum) is written out where it
    is used; one that is an object or a union is named, and has a line of its own.
    """
    reference = node.get("$ref")
    if isinstance(reference, str):
        name = reference.rsplit("/", 1)[-1]
        target = _object(definitions[name])
        if target.get("type") in _PLAIN_TYPES or "enum" in target:
            return _type(target, definitions)
        return name
    if "const" in node:
        return json.dumps(node["const"])
    options = node.get("enum")
    if isinstance(options, list):
        return "|".join(json.dumps(option) for option in options)
    alternatives = node.get("anyOf")
    if isinstance(alternatives, list):
        return "|".join(_type(_object(alternative), definitions) for alternative in alternatives)
    kind = node.get("type")
    pattern = node.get("pattern")
    match kind:
        case "string" if isinstance(pattern, str) and pattern.startswith(_NAME_PATTERN):
            return "name"
        case "string":
            return "string" + _range(node.get("minLength"), node.get("maxLength"))
        case "integer" | "number":
            name = "int" if kind == "integer" else "number"
            low = node.get("minimum", node.get("exclusiveMinimum"))
            if "exclusiveMinimum" in node and "minimum" not in node:
                return f"{name}(>{low}..{node.get('maximum', '')})"
            return name + _range(low, node.get("maximum"))
        case "boolean":
            return "bool"
        case "null":
            return "null"
        case "array":
            items = node.get("items")
            inner = _type(_object(items), definitions) if items is not None else "value"
            return f"[{inner}]" + _range(node.get("minItems"), node.get("maxItems"))
        case "object":
            return "object"
        case _:
            return "value"


def _fields(definition: Mapping[str, JsonValue], definitions: Mapping[str, JsonValue]) -> str:
    properties = _object(definition.get("properties"))
    required = definition.get("required")
    needed = set(required) if isinstance(required, list) else set()
    parts = [
        f"{name}{'' if name in needed else '?'}: {_type(_object(node), definitions)}"
        for name, node in properties.items()
        if name != "kind"
    ]
    return ", ".join(parts)


def shapes(schema: Mapping[str, JsonValue]) -> str:
    """One line per shape of the spec, from the JSON Schema: fields, types and bounds."""
    definitions = _object(schema.get("$defs"))
    lines = [
        (
            "Every object is closed: a field not listed is an error. `name` is a lowercase "
            "snake_case name of at most 64 characters. Ranges are (min..max)."
        ),
        "",
        f"spec: {_fields(schema, definitions)}",
    ]
    listed: set[str] = set()
    for title, union in _UNIONS:
        mapping = _object(_object(_object(definitions[union]).get("discriminator")).get("mapping"))
        lines += ["", f"{title}, by `kind`:"]
        for kind, reference in mapping.items():
            if not isinstance(reference, str):
                raise ValueError("the schema does not have the shape this generator reads")
            name = reference.rsplit("/", 1)[-1]
            listed.add(name)
            lines.append(f"- {kind}: {_fields(_object(definitions[name]), definitions)}")
        listed.add(union)
    others = [
        name
        for name, definition in definitions.items()
        if name not in listed and "properties" in _object(definition)
    ]
    if others:
        lines += ["", "Other shapes:"]
        lines += [
            f"- {name}: {_fields(_object(definitions[name]), definitions)}" for name in others
        ]
    return "\n".join(lines)


def _sections(doc: str) -> dict[int, tuple[str, str]]:
    found = list(_SECTION.finditer(doc))
    result: dict[int, tuple[str, str]] = {}
    for index, match in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(doc)
        result[int(match.group(1))] = (match.group(2), doc[match.end() : end])
    return result


def _plain(body: str) -> str:
    """The body of a docs section for the model: no comment markers, no links, no blank runs."""
    body = _LIMIT_VALUE.sub(r"\1", body)
    body = _COMMENT.sub("", body)
    body = _LINK.sub(r"\1", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def build_section(doc: str, schema: Mapping[str, JsonValue]) -> str:
    """The prompt's spec reference, made from the docs page `doc` and the JSON Schema `schema`."""
    sections = _sections(doc)
    missing = [number for number in SECTIONS if number not in sections]
    if missing:
        raise ValueError(f"docs/ui-spec.md has no section {missing}: the generator is stale")
    parts = [
        "# UI spec reference (tumbleweed.ui/1)",
        "",
        (
            "show_ui takes a spec in this format and validates it before anything is shown. This "
            "reference is generated from the format's schema and its documentation."
        ),
        "",
        "## Shapes",
        "",
        shapes(schema),
    ]
    for number in SECTIONS:
        title, body = sections[number]
        parts += ["", f"## {number}. {title}", "", _plain(body)]
    return "\n".join(parts) + "\n"


def section_text(root: Path) -> str:
    """The section as it should be committed, from the docs and the models under `root`."""
    return build_section((root / DOC_FILE).read_text(encoding="utf-8"), ui_spec_schema())


def main(argv: Sequence[str]) -> int:
    """`--check` (default) reports drift; `--write` regenerates the file. Run from the repo root."""
    mode = argv[0] if argv else "--check"
    if mode not in {"--check", "--write"} or len(argv) > 1:
        sys.stderr.write("usage: python -m tumbleweed.agent.spec_prompt [--check | --write]\n")
        return 2
    if not DOC_FILE.is_file():
        sys.stderr.write(f"{DOC_FILE} not found: run this from the repository root\n")
        return 2
    text = section_text(Path())
    if mode == "--write":
        OUTPUT_FILE.write_text(text, encoding="utf-8")
        return 0
    if not OUTPUT_FILE.is_file() or OUTPUT_FILE.read_text(encoding="utf-8") != text:
        sys.stderr.write(f"{OUTPUT_FILE} is missing or out of date: run with --write\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
