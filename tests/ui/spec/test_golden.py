"""The golden specs: a tip calculator, a photo adjuster and a CSV viewer.

They are the examples in `docs/ui-spec.md`, the fixtures the renderer steps (S10, S11, S12) draw
and the photo adjuster is S20's end-to-end tool. Each is valid, round-trips, and is checked for
the features it is there to show; mutations of each show that the cross-reference rules apply
to it.
"""

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from tests.ui.spec.support import (
    COMPONENT_KINDS,
    EXPR_KINDS,
    GOLDEN_DIR,
    Json,
    accepted,
    golden,
    golden_text,
    mentions,
    refused,
)
from tumbleweed.core.types import JsonValue
from tumbleweed.ui.spec.components import Box, Label
from tumbleweed.ui.spec.names import HandlerName, StateKey
from tumbleweed.ui.spec.parse import parse_ui_spec
from tumbleweed.ui.spec.spec import UiSpec

NAMES = ["tip_calculator", "photo_adjuster", "csv_viewer"]


def test_the_golden_specs_are_exactly_these() -> None:
    assert sorted(path.stem for path in GOLDEN_DIR.glob("*.json")) == sorted(NAMES)


def kinds_used(node: JsonValue) -> set[str]:
    """Every `kind` tag anywhere in a piece of spec JSON."""
    found: set[str] = set()
    if isinstance(node, dict):
        kind = node.get("kind")
        if isinstance(kind, str):
            found.add(kind)
        for value in node.values():
            found |= kinds_used(value)
    elif isinstance(node, list):
        for item in node:
            found |= kinds_used(item)
    return found


def contains(big: JsonValue, small: JsonValue) -> bool:
    """Whether `big` has everything `small` has, with the same values (`5` equals `5.0`)."""
    if isinstance(small, dict):
        return isinstance(big, dict) and all(
            key in big and contains(big[key], value) for key, value in small.items()
        )
    if isinstance(small, list):
        return (
            isinstance(big, list)
            and len(big) == len(small)
            and all(contains(b, x) for b, x in zip(big, small, strict=True))
        )
    return big == small and type(big) in {type(small), int, float}


@pytest.mark.parametrize("name", NAMES)
class TestEveryGoldenSpec:
    def test_is_valid_through_both_entry_points(self, name: str) -> None:
        from tumbleweed.ui.spec.parse import validate_ui_spec

        assert parse_ui_spec(golden_text(name)) == validate_ui_spec(golden(name))

    def test_round_trips_to_the_same_spec_and_the_same_bytes(self, name: str) -> None:
        spec = parse_ui_spec(golden_text(name))

        dumped = spec.model_dump_json()

        assert UiSpec.model_validate_json(dumped) == spec
        assert UiSpec.model_validate_json(dumped).model_dump_json() == dumped
        assert parse_ui_spec(dumped) == spec

    def test_the_dump_writes_out_every_default_and_keeps_everything_that_was_written(
        self, name: str
    ) -> None:
        # The dump writes every field, so two spellings of one spec (a default written or left
        # out) give the same bytes: the form a tool store can normalise to before hashing (S23).
        spec = parse_ui_spec(golden_text(name))
        written = golden(name)
        explicit = json.loads(spec.model_dump_json())

        assert contains(explicit, written)
        assert explicit != written  # the dump added defaults the golden leaves out
        assert parse_ui_spec(json.dumps(explicit)).model_dump_json() == spec.model_dump_json()

    def test_is_stored_as_two_space_json_with_a_trailing_newline(self, name: str) -> None:
        text = golden_text(name)

        assert text.endswith("}\n")
        assert not text.endswith("\n\n")
        assert "\t" not in text

    def test_holds_only_defaults_not_user_state(self, name: str) -> None:
        # ARCHITECTURE section 5: a tool package carries state defaults, never current values.
        spec = parse_ui_spec(golden_text(name))

        assert all("default" in type(decl).model_fields for decl in spec.state)

    def test_uses_no_key_the_spec_does_not_declare(self, name: str) -> None:
        spec = parse_ui_spec(golden_text(name))
        keys = {str(decl.key) for decl in spec.state}

        def binds(node: JsonValue) -> set[str]:
            found: set[str] = set()
            if isinstance(node, dict):
                for field in ("bind", "key", "target"):
                    value = node.get(field)
                    if isinstance(value, str):
                        found.add(value)
                for value in node.values():
                    found |= binds(value)
            elif isinstance(node, list):
                for item in node:
                    found |= binds(item)
            return found

        assert binds(golden(name)["root"]) <= keys


class TestTipCalculator:
    """Computed state only: no handlers, no sandbox, no agent."""

    def spec(self) -> UiSpec:
        return parse_ui_spec(golden_text("tip_calculator"))

    def test_has_no_handlers_so_it_works_with_no_agent_round_trip(self) -> None:
        assert self.spec().handlers == ()

    def test_inputs_are_bounded_numbers_and_the_rest_is_computed(self) -> None:
        spec = self.spec()
        computed = [d for d in spec.state if getattr(d, "computed", None) is not None]
        plain = [d for d in spec.state if getattr(d, "computed", None) is None]

        assert [str(d.key) for d in plain] == ["bill", "tip_percent", "people"]
        assert all(d.kind in {"int", "float"} for d in plain)
        assert len(computed) == 8

    def test_computed_state_is_evaluated_in_dependency_order(self) -> None:
        order = [str(key) for key in self.spec().computed_order()]

        assert order.index("tip_amount") < order.index("total") < order.index("per_person")
        assert order.index("shared") < order.index("per_person_text")
        assert order.index("per_person") < order.index("per_person_text")
        assert order.index("total") < order.index("total_text")
        assert set(order) == {
            "tip_amount",
            "total",
            "per_person",
            "shared",
            "tip_text",
            "total_text",
            "per_person_text",
            "verdict",
        }

    def test_shows_the_expression_features(self) -> None:
        used = kinds_used(golden("tip_calculator"))

        assert {"add", "mul", "div", "round", "if", "concat", "format_number", "ref"} <= used
        assert {"gt", "ge", "lt", "int", "string"} <= used

    def test_binds_computed_entries_one_way_to_labels(self) -> None:
        root = self.spec().root
        assert isinstance(root, Box)
        labels = [child for child in root.children[-4:] if isinstance(child, Label)]

        assert [str(label.bind) for label in labels] == [
            "tip_text",
            "total_text",
            "per_person_text",
            "verdict",
        ]


class TestPhotoAdjuster:
    """Sandbox handlers (run in S20), a `set` handler, an image result."""

    def spec(self) -> UiSpec:
        return parse_ui_spec(golden_text("photo_adjuster"))

    def test_the_sandbox_handler_declares_everything_s20_needs(self) -> None:
        handler = self.spec().handlers_by_name()[HandlerName("adjust")]

        assert handler.kind == "sandbox"
        assert (str(handler.module), str(handler.function)) == ("photo", "adjust")
        assert [str(k) for k in handler.inputs] == [
            "photo_path",
            "brightness",
            "contrast",
            "grayscale",
        ]
        assert [str(k) for k in handler.outputs] == ["result", "status"]
        assert handler.debounce_ms == 250

    def test_the_output_is_an_image_the_renderer_resolves_by_workspace_reference(self) -> None:
        spec = self.spec()

        assert spec.declarations()[StateKey("result")].kind == "image"
        assert spec.declarations()[StateKey("result")].default is None

    def test_the_input_photo_is_text_not_a_reference_to_the_host_file_system(self) -> None:
        # The renderer never opens it: the sandbox reads it, if the user granted it (S19, S20).
        assert self.spec().declarations()[StateKey("photo_path")].kind == "string"

    def test_sliders_and_the_switch_drive_the_sandbox_handler(self) -> None:
        data = golden("photo_adjuster")
        used = kinds_used(data["root"])

        assert {"slider", "switch", "entry", "button", "image", "grid"} <= used
        assert json.dumps(data["root"]).count('"on_change": "adjust"') == 3

    def test_has_a_set_handler_too(self) -> None:
        reset = self.spec().handlers_by_name()[HandlerName("reset")]

        assert reset.kind == "set"
        assert [str(a.target) for a in reset.assignments] == ["brightness", "contrast", "grayscale"]


class TestCsvViewer:
    def spec(self) -> UiSpec:
        return parse_ui_spec(golden_text("csv_viewer"))

    def test_the_table_comes_from_a_sandbox_handler_and_has_no_fixed_columns(self) -> None:
        spec = self.spec()
        table = spec.declarations()[StateKey("data")]

        assert table.kind == "table"
        assert table.default.columns == ()  # the headers are in the file
        handler = spec.handlers_by_name()[HandlerName("load")]
        assert handler.kind == "sandbox"
        assert [str(k) for k in handler.outputs] == ["data", "raw_head", "status"]

    def test_shows_tabs_a_dropdown_and_a_read_only_text_view(self) -> None:
        used = kinds_used(golden("csv_viewer")["root"])

        assert {"tabs", "table", "text_view", "dropdown", "switch", "entry"} <= used


def test_together_the_golden_specs_exercise_most_of_the_vocabulary() -> None:
    components = set().union(*(kinds_used(golden(name)["root"]) for name in NAMES))
    states = set().union(*(kinds_used(golden(name)["state"]) for name in NAMES))

    # These kinds are covered by dedicated tests instead (every node kind in test_expr and
    # test_check, every component in test_spec); the goldens need not show all 18 widgets.
    assert {
        "box",
        "grid",
        "label",
        "heading",
        "slider",
        "spin",
        "image",
        "table",
        "tabs",
    } <= components
    assert {"bool", "int", "float", "string", "enum", "image", "table"} <= states
    assert components <= set(COMPONENT_KINDS)
    assert kinds_used(golden("tip_calculator")["state"]) - set(EXPR_KINDS) <= {
        "bool",
        "int",
        "float",
        "string",
    }


# --- mutations: the rules apply to the goldens -------------------------------------------


@dataclass(frozen=True)
class Mutation:
    spec: str
    what: str
    change: Callable[[Json], None]
    problem: str


def entry(data: Json, key: str) -> Json:
    entries = data["state"]
    assert isinstance(entries, list)
    for item in entries:
        assert isinstance(item, dict)
        if item["key"] == key:
            return item
    raise AssertionError(key)


def remove_state(data: Json, key: str) -> None:
    entries = data["state"]
    assert isinstance(entries, list)
    data["state"] = [e for e in entries if isinstance(e, dict) and e["key"] != key]


def replace_state(data: Json, key: str, new: Json) -> None:
    entries = data["state"]
    assert isinstance(entries, list)
    data["state"] = [new if isinstance(e, dict) and e["key"] == key else e for e in entries]


def set_default(key: str, value: JsonValue) -> Callable[[Json], None]:
    return lambda data: entry(data, key).update(default=value)


def set_computed(key: str, value: JsonValue) -> Callable[[Json], None]:
    return lambda data: entry(data, key).update(computed=value)


def set_root(root: Json) -> Callable[[Json], None]:
    return lambda data: data.update(root=root)


def drop_handlers(data: Json) -> None:
    data["handlers"] = []


MUTATIONS = [
    Mutation(
        "tip_calculator",
        "an input over its maximum",
        set_default("people", 21),
        "must be at most 20",
    ),
    Mutation(
        "tip_calculator",
        "an undeclared input",
        lambda d: remove_state(d, "bill"),
        "unknown state key",
    ),
    Mutation(
        "tip_calculator", "a cycle", set_computed("bill", {"kind": "ref", "key": "total"}), "cycle"
    ),
    Mutation(
        "tip_calculator",
        "a computed entry made two-way",
        set_root({"kind": "entry", "bind": "tip_text"}),
        "two-way",
    ),
    Mutation(
        "tip_calculator",
        "a string where an expression goes",
        set_computed("total", "bill + tip_amount"),
        "valid dictionary or object",
    ),
    Mutation(
        "photo_adjuster",
        "an undeclared sandbox output",
        lambda d: remove_state(d, "status"),
        "output status is not declared",
    ),
    Mutation(
        "photo_adjuster", "a handler that is not declared", drop_handlers, "unknown handler adjust"
    ),
    Mutation(
        "photo_adjuster",
        "an image default that is a URL",
        set_default("result", {"path": "https:/x/a.png"}),
        "must not look like a URL",
    ),
    Mutation(
        "photo_adjuster",
        "an image default that traverses",
        set_default("result", {"path": "../a.png"}),
        "empty, '.' or '..'",
    ),
    Mutation(
        "photo_adjuster",
        "an image default that is an SVG",
        set_default("result", {"path": "a.svg"}),
        "never SVG",
    ),
    Mutation(
        "csv_viewer",
        "a table bound to a string",
        lambda d: replace_state(d, "data", {"kind": "string", "key": "data", "default": ""}),
        "binds data, whose kind is string",
    ),
    Mutation(
        "csv_viewer",
        "a default that is not an option",
        set_default("delimiter", "pipe"),
        "one of 3 options",
    ),
]


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda m: f"{m.spec}: {m.what}")
def test_a_rule_broken_in_a_golden_spec_is_caught(mutation: Mutation) -> None:
    data = copy.deepcopy(golden(mutation.spec))
    mutation.change(data)

    assert mentions(refused(data), mutation.problem)


def test_the_unmutated_goldens_are_accepted() -> None:
    for name in NAMES:
        assert accepted(golden(name)).title
