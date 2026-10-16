"""The envelope and the checks that span it: names exist, types fit, bindings make sense."""

import json
from collections.abc import Callable, Iterator, Mapping
from typing import ClassVar

import pytest
from pydantic import ValidationError

from tests.ui.spec.support import (
    BOUND,
    COMPONENT_KINDS,
    EVENTS,
    HANDLER_KINDS,
    SEPARATOR,
    UNBOUND,
    UNBOUNDED_NUMBERS,
    Json,
    accepted,
    binary,
    build,
    lit_bool,
    lit_float,
    lit_int,
    lit_str,
    mentions,
    minimal,
    ref,
    refused,
)
from tumbleweed.core.types import JsonValue
from tumbleweed.ui.spec.errors import MAX_REPORTED_PROBLEMS, SpecError
from tumbleweed.ui.spec.limits import MAX_SAFE_INT
from tumbleweed.ui.spec.names import HandlerName, StateKey
from tumbleweed.ui.spec.parse import validate_ui_spec
from tumbleweed.ui.spec.spec import SPEC_VERSION, UiSpec
from tumbleweed.ui.spec.values import Table

# A declaration of each kind under the key `x`, valid as a binding target (numbers have bounds).
TARGETS: dict[str, Json] = {
    "bool": {"kind": "bool", "key": "x", "default": True},
    "int": {"kind": "int", "key": "x", "default": 5, "minimum": 0, "maximum": 10},
    "float": {"kind": "float", "key": "x", "default": 0.5, "minimum": 0, "maximum": 1},
    "string": {"kind": "string", "key": "x", "default": ""},
    "enum": {"kind": "enum", "key": "x", "options": ["a", "b"], "default": "a"},
    "image": {"kind": "image", "key": "x"},
    "file": {"kind": "file", "key": "x"},
    "table": {"kind": "table", "key": "x"},
    "list": {"kind": "list", "key": "x", "item_type": "int"},
    "json": {"kind": "json", "key": "x"},
}
X_INT = TARGETS["int"]
AGENT: Json = {"kind": "agent", "name": "h"}


class TestEnvelope:
    def test_the_smallest_spec(self) -> None:
        spec = accepted(minimal())

        assert spec.version == SPEC_VERSION == "tumbleweed.ui/1"
        assert spec.title == "Test"
        assert spec.state == ()
        assert spec.handlers == ()

    @pytest.mark.parametrize("version", ["tumbleweed.ui/2", "tumbleweed.ui/0", "ui/1", "", None, 1])
    def test_only_version_one_is_understood(self, version: JsonValue) -> None:
        with pytest.raises(SpecError):
            validate_ui_spec(minimal(version=version))

    @pytest.mark.parametrize("missing", ["version", "title", "root"])
    def test_version_title_and_root_are_required(self, missing: str) -> None:
        data = minimal()
        del data[missing]

        assert mentions(refused(data), missing, "Field required")

    @pytest.mark.parametrize("field", ["code", "script", "theme", "css", "on_load"])
    def test_unknown_top_level_fields_are_refused(self, field: str) -> None:
        assert mentions(refused(minimal(**{field: "x"})), "Extra inputs")

    @pytest.mark.parametrize("title", ["", "x" * 81, "a\nb", "a\0b", "a\u202eb", 5, None])
    def test_the_title_is_a_short_plain_line(self, title: JsonValue) -> None:
        with pytest.raises(SpecError):
            validate_ui_spec(minimal(title=title))

    def test_a_validated_spec_cannot_be_edited(self) -> None:
        spec = accepted(build(root={"kind": "box", "children": [SEPARATOR]}, state=[X_INT]))

        assert isinstance(spec.state, tuple)
        assert isinstance(spec.handlers, tuple)
        with pytest.raises(ValidationError, match="frozen"):
            setattr(spec, "title", "other")  # noqa: B010  (the assignment is the point)
        with pytest.raises(ValidationError, match="frozen"):
            setattr(spec.state[0], "default", 1)  # noqa: B010
        assert not hasattr(spec.state, "append")

    def test_round_trips_through_json(self) -> None:
        spec = accepted(build(root={"kind": "slider", "bind": "x"}, state=[X_INT]))

        again = UiSpec.model_validate_json(spec.model_dump_json())

        assert again == spec
        assert again.model_dump_json() == spec.model_dump_json()


class TestNames:
    def test_state_keys_are_unique(self) -> None:
        problems = refused(build(state=[TARGETS["bool"], TARGETS["bool"]]))

        assert mentions(problems, "state key x is declared twice")

    def test_handler_names_are_unique(self) -> None:
        setter: Json = {
            "kind": "set",
            "name": "h",
            "assignments": [{"target": "x", "value": lit_int(1)}],
        }

        assert mentions(refused(build(handlers=[AGENT, AGENT])), "handler h is declared twice")
        problems = refused(build(state=[X_INT], handlers=[AGENT, setter]))
        assert mentions(problems, "handler h is declared twice")

    def test_the_lookups(self) -> None:
        spec = accepted(build(state=[X_INT], handlers=[{"kind": "agent", "name": "ask"}]))

        assert list(spec.declarations()) == ["x"]
        assert spec.declarations()[StateKey("x")] is spec.state[0]
        assert list(spec.handlers_by_name()) == ["ask"]
        assert spec.handlers_by_name()[HandlerName("ask")] is spec.handlers[0]


# --- computed state -----------------------------------------------------------------------

LITERALS: dict[str, Json] = {
    "bool": lit_bool(value=True),
    "int": lit_int(1),
    "float": lit_float(1.5),
    "string": lit_str("s"),
}
DEFAULTS: dict[str, JsonValue] = {"bool": False, "int": 0, "float": 0.0, "string": ""}
# What each kind of state may be computed from or assigned, by the type of the expression.
FITS = {"bool": {"bool"}, "int": {"int"}, "float": {"int", "float"}, "string": {"string"}}


def computed(key: str, kind: str, expression: Json) -> Json:
    return {"kind": kind, "key": key, "default": DEFAULTS[kind], "computed": expression}


class TestComputedState:
    @pytest.mark.parametrize("kind", FITS)
    @pytest.mark.parametrize("value_type", LITERALS)
    def test_the_expression_type_must_fit_the_kind(self, kind: str, value_type: str) -> None:
        data = build(state=[computed("c", kind, LITERALS[value_type])])

        if value_type in FITS[kind]:
            assert accepted(data).state[0].key == "c"
        else:
            expected = f"an entry of kind {kind} cannot be computed from an expression of type"
            assert mentions(refused(data), "state c (computed)", expected, value_type)

    def test_an_expression_that_does_not_type_check_is_reported_with_the_entry(self) -> None:
        bad = binary("add", lit_int(1), lit_str("x"))

        problems = refused(build(state=[computed("c", "int", bad)]))

        assert mentions(problems, "state c (computed)", "add: expected two numbers, got int")

    def test_a_reference_to_an_undeclared_key_is_reported(self) -> None:
        problems = refused(build(state=[computed("c", "int", ref("missing"))]))

        assert mentions(problems, "unknown state key 'missing'")

    def test_computed_entries_may_read_each_other_and_plain_state(self) -> None:
        a: Json = {"kind": "int", "key": "a", "default": 1}
        b = computed("b", "int", binary("add", ref("a"), ref("c")))
        c = computed("c", "int", binary("mul", ref("a"), lit_int(2)))

        spec = accepted(build(state=[a, b, c]))

        assert spec.computed_order() == ("c", "b")

    def test_a_cycle_is_refused(self) -> None:
        a = computed("a", "int", ref("b"))
        b = computed("b", "int", ref("a"))

        problems = refused(build(state=[a, b]))

        assert mentions(problems, "computed state has a cycle: a -> b -> a")

    def test_reading_itself_is_a_cycle(self) -> None:
        a = computed("a", "int", binary("add", ref("a"), lit_int(1)))

        assert mentions(refused(build(state=[a])), "cycle: a -> a")

    def test_a_cycle_through_a_conditional_is_still_a_cycle(self) -> None:
        branch: Json = {
            "kind": "if",
            "condition": lit_bool(value=False),
            "then": lit_int(1),
            "otherwise": ref("b"),
        }

        problems = refused(
            build(state=[computed("a", "int", branch), computed("b", "int", ref("a"))])
        )

        assert mentions(problems, "cycle")

    @pytest.mark.parametrize("kind", ["table", "image", "list", "json", "file"])
    def test_a_computed_entry_cannot_read_a_value_with_no_scalar(self, kind: str) -> None:
        problems = refused(build(state=[TARGETS[kind], computed("c", "int", ref("x"))]))

        assert mentions(problems, f"x is of kind {kind}, which expressions cannot read")

    def test_an_enum_can_be_read_as_a_string(self) -> None:
        accepted(build(state=[TARGETS["enum"], computed("c", "string", ref("x"))]))


# --- bindings -----------------------------------------------------------------------------


def test_every_component_kind_is_classified() -> None:
    assert set(BOUND) | set(UNBOUND) == set(COMPONENT_KINDS)
    assert set(BOUND).isdisjoint(UNBOUND)
    assert len(COMPONENT_KINDS) == 18


@pytest.mark.parametrize("component", BOUND)
class TestBindings:
    def test_a_component_binds_the_kinds_it_accepts_and_no_others(self, component: str) -> None:
        template, kinds, _, _ = BOUND[component]

        for kind, target in TARGETS.items():
            data = build(template, [target])
            if kind in kinds:
                assert accepted(data).root.kind == component
            else:
                assert mentions(refused(data), f"({component})", f"binds x, whose kind is {kind}")

    def test_the_error_names_the_component_by_its_path_and_kind(self, component: str) -> None:
        template, kinds, _, _ = BOUND[component]
        wrong = next(kind for kind in TARGETS if kind not in kinds)
        root: Json = {"kind": "box", "children": [SEPARATOR, template]}

        problems = refused(build(root, [TARGETS[wrong]]))

        assert mentions(problems, f"root.children[1] ({component})", f"whose kind is {wrong}")

    def test_binding_an_undeclared_key_is_refused(self, component: str) -> None:
        problems = refused(build(BOUND[component][0]))

        assert mentions(problems, f"({component})", "binds unknown state key x")


# The components that can bind a kind of entry that can be computed, and a number.
COMPUTABLE_BINDERS = [c for c, (_, kinds, _, _) in BOUND.items() if kinds & set(FITS)]
NUMBER_BINDERS = [c for c, (_, kinds, _, _) in BOUND.items() if kinds & {"int", "float"}]


def test_the_components_that_cannot_bind_a_computed_entry_are_these() -> None:
    # No computed image, table or enum exists, so there is no computed entry to bind to them.
    assert set(BOUND) - set(COMPUTABLE_BINDERS) == {"image", "table", "dropdown"}
    assert set(NUMBER_BINDERS) == {"progress", "slider", "spin"}


@pytest.mark.parametrize("component", COMPUTABLE_BINDERS)
def test_a_computed_entry_is_read_only(component: str) -> None:
    template, kinds, writes, _ = BOUND[component]
    scalar = next(kind for kind in FITS if kind in kinds)
    entry: Json = {**TARGETS[scalar], "computed": LITERALS[scalar]}

    if writes:
        problems = refused(build(template, [entry]))
        assert mentions(problems, f"({component})", "binds x two-way, but it is computed")
    else:
        assert accepted(build(template, [entry])).root.kind == component


@pytest.mark.parametrize("component", NUMBER_BINDERS)
def test_numbers_must_have_both_bounds_where_the_widget_needs_a_range(component: str) -> None:
    template, _, _, bounded = BOUND[component]

    for target in UNBOUNDED_NUMBERS:
        if bounded:
            problems = refused(build(template, [target]))
            assert mentions(problems, "needs both a minimum and a maximum")
        else:
            accepted(build(template, [target]))


class TestWholeNumberFieldsAreExact:
    """Every field of whole numbers refuses a bool, a float and a string, as `int` fields do."""

    FIELDS: ClassVar[list[tuple[str, Json]]] = [
        ("box spacing", {"kind": "box", "spacing": 1}),
        ("grid columns", {"kind": "grid", "columns": 1}),
        ("grid row spacing", {"kind": "grid", "columns": 1, "row_spacing": 1}),
        ("heading level", {"kind": "heading", "level": 1, "text": "t"}),
    ]
    NUMBER_FIELD: ClassVar[dict[str, str]] = {
        "box spacing": "spacing",
        "grid columns": "columns",
        "grid row spacing": "row_spacing",
        "heading level": "level",
    }

    @pytest.mark.parametrize(("name", "component"), FIELDS, ids=[f[0] for f in FIELDS])
    @pytest.mark.parametrize("bad", [True, 1.0, "1", None])
    def test_a_component_number_is_a_real_int(
        self, name: str, component: Json, bad: JsonValue
    ) -> None:
        accepted(build(component))
        with pytest.raises(SpecError):
            validate_ui_spec(build({**component, self.NUMBER_FIELD[name]: bad}))

    @pytest.mark.parametrize(
        ("kind", "field"),
        [("string", "max_length"), ("list", "max_items"), ("table", "max_rows")],
    )
    @pytest.mark.parametrize("bad", [True, 1.0, "1", None])
    def test_a_state_bound_is_a_real_int(self, kind: str, field: str, bad: JsonValue) -> None:
        entry: Json = {**TARGETS[kind], field: bad}

        with pytest.raises(SpecError):
            validate_ui_spec(build(state=[entry]))


class TestTheWholeTreeIsChecked:
    """Every container is walked: a bad binding is found at any depth, under any kind of parent."""

    BAD_LABEL: ClassVar[Json] = {"kind": "label", "bind": "nope"}

    @pytest.mark.parametrize(
        ("wrap", "path"),
        [
            (lambda c: {"kind": "box", "children": [c]}, "root.children[0] (label)"),
            (lambda c: {"kind": "grid", "columns": 1, "children": [c]}, "root.children[0] (label)"),
            (
                lambda c: {"kind": "tabs", "pages": [{"title": "T", "content": c}]},
                "root.pages[0].content (label)",
            ),
            (lambda c: {"kind": "expander", "title": "E", "child": c}, "root.child (label)"),
        ],
        ids=["box", "grid", "tabs", "expander"],
    )
    def test_a_container_of_every_kind_is_walked(
        self, wrap: Callable[[Json], Json], path: str
    ) -> None:
        problems = refused(build(wrap(self.BAD_LABEL)))

        assert mentions(problems, path, "unknown state key nope")

    def test_the_path_names_each_level(self) -> None:
        tree: Json = {
            "kind": "box",
            "children": [
                SEPARATOR,
                {
                    "kind": "tabs",
                    "pages": [
                        {"title": "A", "content": {"kind": "box"}},
                        {
                            "title": "B",
                            "content": {
                                "kind": "expander",
                                "title": "E",
                                "child": {
                                    "kind": "grid",
                                    "columns": 2,
                                    "children": [SEPARATOR, self.BAD_LABEL],
                                },
                            },
                        },
                    ],
                },
            ],
        }

        problems = refused(build(tree))

        assert mentions(
            problems, "root.children[1].pages[1].content.child.children[1] (label)", "nope"
        )

    @pytest.mark.parametrize("bad", ["a\nb", "a\0b", "x" * 201])
    def test_a_page_title_and_an_expander_title_are_short_plain_lines(self, bad: str) -> None:
        pages: list[JsonValue] = [{"title": bad, "content": {"kind": "box"}}]
        trees: list[Json] = [
            {"kind": "tabs", "pages": pages},
            {"kind": "expander", "title": bad, "child": {"kind": "box"}},
        ]
        for tree in trees:
            with pytest.raises(SpecError):
                validate_ui_spec(build(tree))


def test_the_widgets_that_need_a_range_are_exactly_these() -> None:
    assert {name for name, (_, _, _, bounded) in BOUND.items() if bounded} == {
        "slider",
        "spin",
        "progress",
    }


class TestEnumLiteralsThroughIf:
    CHOOSE: ClassVar[Json] = {"kind": "bool", "key": "flag", "default": True}

    def assigned(self, value: Json) -> Json:
        return build(
            state=[TARGETS["enum"], self.CHOOSE], handlers=[set_handler("h", ("x", value))]
        )

    @staticmethod
    def choose(then: Json, otherwise: Json) -> Json:
        return {"kind": "if", "condition": ref("flag"), "then": then, "otherwise": otherwise}

    def test_an_if_of_options_may_be_assigned(self) -> None:
        accepted(self.assigned(self.choose(lit_str("a"), lit_str("b"))))
        accepted(self.assigned(self.choose(self.choose(lit_str("a"), lit_str("b")), lit_str("a"))))

    def test_an_if_with_a_literal_that_is_no_option_is_refused(self) -> None:
        problems = refused(self.assigned(self.choose(lit_str("a"), lit_str("zzz"))))

        assert mentions(problems, "handler h: assigning x", "'zzz' is not one of its options")

    def test_a_deeper_branch_is_checked_too(self) -> None:
        nested = self.choose(self.choose(lit_str("a"), lit_str("zzz")), lit_str("b"))

        assert mentions(refused(self.assigned(nested)), "'zzz' is not one of its options")

    def test_a_branch_that_cannot_be_known_is_not_guessed_at(self) -> None:
        other: Json = {"kind": "string", "key": "s", "default": ""}
        spec = build(
            state=[TARGETS["enum"], self.CHOOSE, other],
            handlers=[set_handler("h", ("x", self.choose(ref("s"), lit_str("a"))))],
        )

        accepted(spec)  # what `s` holds is for the runtime to check: the entry keeps its value

    def test_a_computed_comparison_through_if_is_checked_the_same_way(self) -> None:
        compared: Json = {
            "kind": "eq",
            "left": ref("x"),
            "right": self.choose(lit_str("a"), lit_str("nope")),
        }
        derived: Json = {"kind": "bool", "key": "d", "default": False, "computed": compared}

        problems = refused(build(state=[TARGETS["enum"], self.CHOOSE, derived]))

        assert mentions(problems, "state d (computed)", "'nope' is not an option of x")

    def test_two_enums_with_no_option_in_common_are_refused_in_a_computed_entry(self) -> None:
        other: Json = {"kind": "enum", "key": "y", "options": ["p", "q"], "default": "p"}
        derived: Json = {
            "kind": "bool",
            "key": "d",
            "default": False,
            "computed": binary("eq", ref("x"), ref("y")),
        }

        problems = refused(build(state=[TARGETS["enum"], other, derived]))

        assert mentions(problems, "x and y are enums with no option in common")


class TestTextViewEvents:
    VIEW: ClassVar[Json] = {"kind": "text_view", "bind": "x", "on_change": "h"}

    def test_a_read_only_view_has_no_edits_to_report(self) -> None:
        problems = refused(build(self.VIEW, [TARGETS["string"]], [AGENT]))

        assert mentions(problems, "(text_view)", "on_change needs editable")

    def test_an_editable_view_may_have_on_change(self) -> None:
        accepted(build({**self.VIEW, "editable": True}, [TARGETS["string"]], [AGENT]))

    def test_a_read_only_view_without_on_change_is_fine(self) -> None:
        accepted(build({"kind": "text_view", "bind": "x"}, [TARGETS["string"]]))

    def test_an_unknown_handler_and_a_read_only_view_are_both_reported(self) -> None:
        view = {**self.VIEW, "on_change": "nope"}

        problems = refused(build(view, [TARGETS["string"]]))

        assert mentions(problems, "unknown handler nope")
        assert mentions(problems, "on_change needs editable")


def number_entry(kind: str, **fields: JsonValue) -> Json:
    default: JsonValue = 0 if kind == "int" else 0.0
    return {"kind": kind, "key": "x", "default": default, **fields}


class TestWidgetRanges:
    """A slider, spin button or progress bar needs a range and a step it can work with."""

    @pytest.mark.parametrize("component", NUMBER_BINDERS)
    def test_a_sane_range_is_fine(self, component: str) -> None:
        template = BOUND[component][0]

        accepted(build(template, [number_entry("float", minimum=0, maximum=1, step=0.01)]))
        accepted(build(template, [number_entry("int", minimum=-100, maximum=100, step=5)]))

    @pytest.mark.parametrize("component", NUMBER_BINDERS)
    @pytest.mark.parametrize("kind", ["int", "float"])
    def test_the_range_may_not_be_empty(self, component: str, kind: str) -> None:
        template = BOUND[component][0]
        entry = number_entry(kind, minimum=5, maximum=5, default=5)

        assert mentions(refused(build(template, [entry])), "needs a minimum below its maximum")

    @pytest.mark.parametrize("component", NUMBER_BINDERS)
    def test_float_bounds_stay_within_the_safe_integer_range(self, component: str) -> None:
        template = BOUND[component][0]
        for minimum, maximum in [(-1e300, 0.0), (0.0, 1e300), (-1e300, 1e300), (-1e16, 1.0)]:
            entry = number_entry("float", minimum=minimum, maximum=maximum)

            assert mentions(
                refused(build(template, [entry])), f"needs bounds within +-{MAX_SAFE_INT}"
            )

    @pytest.mark.parametrize("component", NUMBER_BINDERS)
    def test_a_range_wider_than_the_safe_integers_is_refused(self, component: str) -> None:
        template = BOUND[component][0]
        wide = number_entry("int", minimum=-(2**52), maximum=2**52)  # 2**53 wide
        widest = number_entry("float", minimum=-float(MAX_SAFE_INT), maximum=float(MAX_SAFE_INT))

        for entry in (wide, widest):
            assert mentions(refused(build(template, [entry])), "needs a range at most")

    @pytest.mark.parametrize("component", NUMBER_BINDERS)
    @pytest.mark.parametrize("step", [5e-324, 1e-320, 1e-310, 2.2e-308])
    def test_a_subnormal_step_is_refused(self, component: str, step: float) -> None:
        template = BOUND[component][0]
        entry = number_entry("float", minimum=0, maximum=1e-300, step=step)

        assert mentions(refused(build(template, [entry])), "not subnormal")

    @pytest.mark.parametrize("component", NUMBER_BINDERS)
    def test_a_step_tiny_beside_the_range_is_refused(self, component: str) -> None:
        template = BOUND[component][0]
        entry = number_entry("float", minimum=0, maximum=1000, step=1e-9)

        assert mentions(refused(build(template, [entry])), "a step of at least 1/10000000")

    @pytest.mark.parametrize("component", NUMBER_BINDERS)
    def test_the_normal_smallest_step_of_a_tiny_range_is_fine(self, component: str) -> None:
        template = BOUND[component][0]

        accepted(build(template, [number_entry("float", minimum=0, maximum=2e-303, step=2.3e-308)]))

    def test_a_float_without_a_step_has_no_step_to_check(self) -> None:
        accepted(
            build({"kind": "slider", "bind": "x"}, [number_entry("float", minimum=0, maximum=1)])
        )

    def test_the_rules_apply_where_the_entry_is_bound_not_where_it_is_declared(self) -> None:
        odd = number_entry("float", minimum=5, maximum=5, default=5)

        accepted(build(state=[odd]))  # nothing binds it to a widget with a range
        accepted(build({"kind": "label", "bind": "s"}, [odd, {**TARGETS["string"], "key": "s"}]))


class TestTextView:
    def test_an_editable_view_writes_so_it_cannot_bind_computed_state(self) -> None:
        entry: Json = {**TARGETS["string"], "computed": lit_str("s")}
        read_only: Json = {"kind": "text_view", "bind": "x"}
        editable: Json = {"kind": "text_view", "bind": "x", "editable": True}

        accepted(build(read_only, [entry]))
        assert mentions(refused(build(editable, [entry])), "two-way")
        accepted(build(editable, [TARGETS["string"]]))


class TestLabelsAndHeadings:
    @pytest.mark.parametrize("kind", ["label", "heading"])
    def test_exactly_one_of_text_and_bind(self, kind: str) -> None:
        both: Json = {"kind": kind, "text": "t", "bind": "x"}
        neither: Json = {"kind": kind}

        for root in (both, neither):
            problems = refused(build(root, [TARGETS["string"]]))
            assert mentions(problems, "exactly one of `text`")
        accepted(build({"kind": kind, "text": "t"}))
        accepted(build({"kind": kind, "text": ""}))

    @pytest.mark.parametrize("level", [1, 2, 3])
    def test_a_heading_level_is_one_to_three(self, level: int) -> None:
        accepted(build({"kind": "heading", "level": level, "text": "t"}))

    @pytest.mark.parametrize("level", [0, 4, "1", 1.0, True])
    def test_no_other_heading_level(self, level: JsonValue) -> None:
        with pytest.raises(SpecError):
            validate_ui_spec(build({"kind": "heading", "level": level, "text": "t"}))


# --- handlers -----------------------------------------------------------------------------


class TestEventHandlers:
    @pytest.mark.parametrize("component", EVENTS)
    def test_an_event_names_a_declared_handler(self, component: str) -> None:
        if component in BOUND:
            template, kinds, _, _ = BOUND[component]
            state = [TARGETS[next(iter(kinds))]]
            if component == "text_view":
                template = {**template, "editable": True}  # a read-only view has no edits
        else:
            template, state = UNBOUND[component], []

        for event in EVENTS[component]:
            accepted(build({**template, event: "h"}, state, [AGENT]))
            problems = refused(build({**template, event: "nope"}, state, [AGENT]))
            assert mentions(problems, f"({component})", "refers to unknown handler nope")

    def test_every_component_with_an_event_is_listed(self) -> None:
        with_events = {
            kind
            for kind, cls in COMPONENT_KINDS.items()
            if any(name.startswith("on_") for name in cls.model_fields)
        }

        assert with_events == set(EVENTS)
        for kind, events in EVENTS.items():
            handler_fields = {n for n in COMPONENT_KINDS[kind].model_fields if n.startswith("on_")}
            assert handler_fields == set(events)

    def test_an_event_may_name_a_handler_of_any_kind(self) -> None:
        sandbox: Json = {
            "kind": "sandbox",
            "name": "s",
            "module": "m",
            "function": "f",
            "outputs": ["x"],
        }
        setter = set_handler("t", ("x", lit_int(1)))
        for name in ("h", "s", "t"):
            root: Json = {"kind": "button", "label": "Go", "on_click": name}
            accepted(build(root, [X_INT], [AGENT, sandbox, setter]))

    def test_a_button_without_a_handler_is_fine(self) -> None:
        accepted(build({"kind": "button", "label": "Go"}))

    def test_a_handler_no_component_uses_is_fine(self) -> None:
        accepted(build(handlers=[AGENT]))


def set_handler(name: str, *assignments: tuple[str, Json]) -> Json:
    items: list[JsonValue] = [{"target": target, "value": value} for target, value in assignments]
    return {"kind": "set", "name": name, "assignments": items}


def assigning(*assignments: tuple[str, Json], state: Json = X_INT) -> Json:
    return build(state=[state], handlers=[set_handler("h", *assignments)])


class TestSetHandlers:
    @pytest.mark.parametrize("kind", FITS)
    @pytest.mark.parametrize("value_type", LITERALS)
    def test_the_value_must_fit_the_target(self, kind: str, value_type: str) -> None:
        target: Json = {"kind": kind, "key": "x", "default": DEFAULTS[kind]}
        data = assigning(("x", LITERALS[value_type]), state=target)

        if value_type in FITS[kind]:
            accepted(data)
        else:
            expected = (
                f"an entry of kind {kind} cannot be assigned an expression of type {value_type}"
            )
            assert mentions(refused(data), "handler h: assigning x", expected)

    def test_a_whole_number_target_suggests_round(self) -> None:
        problems = refused(assigning(("x", lit_float(1.5))))

        assert mentions(problems, "use round for a whole number")

    def test_round_makes_a_float_assignable_to_an_int(self) -> None:
        accepted(assigning(("x", {"kind": "round", "operand": lit_float(1.5)})))

    def test_an_enum_is_assigned_a_string_that_is_one_of_its_options(self) -> None:
        enum = TARGETS["enum"]

        accepted(assigning(("x", lit_str("b")), state=enum))
        assert mentions(refused(assigning(("x", lit_str("c")), state=enum)), "'c' is not one of")
        for wrong in (lit_int(1), lit_bool(value=True)):
            problems = refused(assigning(("x", wrong), state=enum))
            assert mentions(problems, "an entry of kind enum cannot be assigned")

    @pytest.mark.parametrize("kind", ["image", "file", "table", "list", "json"])
    def test_other_kinds_cannot_be_assigned_by_an_expression(self, kind: str) -> None:
        problems = refused(assigning(("x", lit_int(1)), state=TARGETS[kind]))

        assert mentions(problems, f"an entry of kind {kind} cannot be assigned")

    def test_an_unknown_target_is_refused(self) -> None:
        problems = refused(build(handlers=[set_handler("h", ("nope", lit_int(1)))]))

        assert mentions(problems, "handler h: assigning nope: unknown state key")

    def test_a_computed_target_is_refused(self) -> None:
        derived = computed("x", "int", lit_int(1))

        problems = refused(assigning(("x", lit_int(2)), state=derived))

        assert mentions(problems, "assigning x: it is computed, so it cannot be assigned")

    def test_an_expression_that_does_not_type_check_is_refused(self) -> None:
        problems = refused(assigning(("x", binary("add", lit_int(1), lit_str("x")))))

        assert mentions(problems, "handler h: assigning x: add: expected two numbers")

    def test_the_value_may_read_other_state_and_the_target_itself(self) -> None:
        accepted(assigning(("x", binary("add", ref("x"), lit_int(1)))))

    def test_a_target_may_not_repeat(self) -> None:
        with pytest.raises(SpecError, match="must not repeat a state key"):
            validate_ui_spec(assigning(("x", lit_int(1)), ("x", lit_int(2))))

    def test_there_is_at_least_one_assignment(self) -> None:
        with pytest.raises(SpecError, match="at least 1"):
            validate_ui_spec(build(handlers=[set_handler("h")]))


def sandbox(**fields: JsonValue) -> Json:
    handler: Json = {"kind": "sandbox", "name": "h", "module": "photo", "function": "adjust"}
    handler.update(fields)
    return handler


def running(handler: Json, *state: Json) -> Json:
    return build(state=state or [X_INT], handlers=[handler])


class TestSandboxHandlers:
    def test_inputs_and_outputs_are_declared_state_keys(self) -> None:
        out: Json = {**TARGETS["image"], "key": "out"}
        handler = sandbox(inputs=["x"], outputs=["out"], debounce_ms=250)

        spec = accepted(running(handler, X_INT, out))

        found = spec.handlers_by_name()[HandlerName("h")]
        assert found.kind == "sandbox"
        assert (found.module, found.function) == ("photo", "adjust")
        assert found.inputs == ("x",)
        assert found.outputs == ("out",)
        assert found.debounce_ms == 250

    def test_inputs_are_optional_and_there_is_no_debounce_by_default(self) -> None:
        found = accepted(running(sandbox(outputs=["x"]))).handlers[0]

        assert found.kind == "sandbox"
        assert found.inputs == ()
        assert found.debounce_ms == 0

    def test_an_undeclared_output_is_refused(self) -> None:
        problems = refused(build(handlers=[sandbox(outputs=["nope"])]))

        assert mentions(problems, "handler h: output nope is not declared")

    def test_an_undeclared_input_is_refused(self) -> None:
        problems = refused(running(sandbox(inputs=["nope"], outputs=["x"])))

        assert mentions(problems, "handler h: input nope is not declared")

    def test_a_computed_output_is_refused_but_a_computed_input_is_fine(self) -> None:
        derived = computed("c", "int", lit_int(1))

        problems = refused(running(sandbox(outputs=["c"]), X_INT, derived))
        assert mentions(problems, "output c is computed, so a sandbox cannot write it")
        accepted(running(sandbox(inputs=["c"], outputs=["x"]), X_INT, derived))

    def test_an_entry_may_be_both_input_and_output(self) -> None:
        accepted(running(sandbox(inputs=["x"], outputs=["x"])))

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"outputs": []}, "at least 1"),
            ({}, "Field required"),
            ({"outputs": ["x", "x"]}, "outputs of a sandbox handler must not repeat"),
            ({"inputs": ["x", "x"], "outputs": ["x"]}, "inputs of a sandbox handler must not"),
        ],
    )
    def test_there_is_at_least_one_output_and_nothing_repeats(
        self, fields: dict[str, JsonValue], message: str
    ) -> None:
        with pytest.raises(SpecError, match=message):
            validate_ui_spec(running(sandbox(**fields)))

    @pytest.mark.parametrize("field", ["module", "function"])
    @pytest.mark.parametrize(
        "bad",
        ["", "Photo", "__init__", "_private", "a.b", "a/b", "a b", "a-b", "../x", "1a", "x" * 65],
    )
    def test_module_and_function_are_plain_names_never_paths_or_dunders(
        self, field: str, bad: str
    ) -> None:
        with pytest.raises(SpecError):
            validate_ui_spec(running(sandbox(outputs=["x"], **{field: bad})))

    @pytest.mark.parametrize("good", [0, 1, 10_000])
    def test_the_debounce_is_a_bounded_number_of_milliseconds(self, good: int) -> None:
        accepted(running(sandbox(outputs=["x"], debounce_ms=good)))

    @pytest.mark.parametrize("bad", [-1, 10_001, 0.5, "250", True, None])
    def test_no_other_debounce(self, bad: JsonValue) -> None:
        with pytest.raises(SpecError):
            validate_ui_spec(running(sandbox(outputs=["x"], debounce_ms=bad)))

    @pytest.mark.parametrize(
        "field", ["source", "code", "command", "path", "args", "env", "network", "timeout"]
    )
    def test_there_is_no_field_for_code_a_command_or_a_path(self, field: str) -> None:
        with pytest.raises(SpecError, match="Extra inputs"):
            validate_ui_spec(running(sandbox(outputs=["x"], **{field: "x"})))


class TestAgentHandlers:
    def test_the_note_is_optional_plain_text(self) -> None:
        assert accepted(build(handlers=[AGENT])).handlers[0].kind == "agent"
        noted: Json = {"kind": "agent", "name": "h", "note": "The user picked a size."}

        found = accepted(build(handlers=[noted])).handlers[0]

        assert found.kind == "agent"
        assert found.note == "The user picked a size."

    @pytest.mark.parametrize("bad", ["a\0b", "a\u202eb", "x" * 1001, 5])
    def test_a_bad_note_is_refused(self, bad: JsonValue) -> None:
        with pytest.raises(SpecError):
            validate_ui_spec(build(handlers=[{"kind": "agent", "name": "h", "note": bad}]))


def test_every_handler_kind_is_covered() -> None:
    assert set(HANDLER_KINDS) == {"set", "sandbox", "agent"}


class TestHandlersAreClosed:
    @pytest.mark.parametrize(
        "handler",
        [
            {"kind": "python", "name": "h", "source": "print(1)"},
            {"kind": "shell", "name": "h", "command": "id"},
            {"kind": "navigate", "name": "h", "url": "https://example.com"},
            {"name": "h"},
            {"kind": "set", "name": "H", "assignments": []},
        ],
    )
    def test_unknown_kinds_and_bad_names_are_refused(self, handler: Json) -> None:
        with pytest.raises(SpecError):
            validate_ui_spec(build(handlers=[handler]))


# --- problems are reported together -------------------------------------------------------


class TestProblemReports:
    def test_every_problem_is_reported_not_just_the_first(self) -> None:
        bad_entry: Json = {**X_INT, "computed": lit_str("x")}
        root: Json = {
            "kind": "box",
            "children": [
                {"kind": "label", "bind": "nope"},
                {"kind": "button", "label": "Go", "on_click": "missing"},
            ],
        }

        problems = refused(build(root, [bad_entry], [sandbox(outputs=["gone"])]))

        assert mentions(problems, "state x (computed)")
        assert mentions(problems, "root.children[0] (label)", "unknown state key nope")
        assert mentions(problems, "root.children[1] (button)", "unknown handler missing")
        assert mentions(problems, "output gone is not declared")

    def test_a_hostile_spec_gets_a_bounded_report(self) -> None:
        labels: list[JsonValue] = [{"kind": "label", "bind": f"missing_{i}"} for i in range(60)]

        problems = refused(build({"kind": "box", "children": labels}))

        assert len(problems) == MAX_REPORTED_PROBLEMS + 1
        assert problems[-1] == "... and more problems"
        assert all(len(problem) <= 300 for problem in problems)


# --- patches ------------------------------------------------------------------------------


def patch_spec() -> UiSpec:
    return accepted(
        build(
            state=[
                {**X_INT, "key": "n"},
                {**TARGETS["string"], "key": "s", "max_length": 5},
                {**TARGETS["enum"], "key": "e"},
                {**TARGETS["image"], "key": "img"},
                {**TARGETS["table"], "key": "t", "max_rows": 2},
                {**TARGETS["list"], "key": "ls"},
                computed("c", "int", lit_int(1)),
            ]
        )
    )


class TestValidatePatch:
    def test_a_valid_patch_comes_back_typed(self) -> None:
        columns: list[JsonValue] = [{"name": "a", "type": "int"}]
        patch: dict[str, object] = {
            "n": 7,
            "s": "abc",
            "e": "b",
            "img": {"path": "out/a.png"},
            "t": {"columns": columns, "rows": [[1]]},
            "ls": [1, 2],
        }

        result = patch_spec().validate_patch(patch)

        assert result[StateKey("n")] == 7
        assert result[StateKey("s")] == "abc"
        assert result[StateKey("img")] is not None
        assert isinstance(result[StateKey("t")], Table)
        assert result[StateKey("ls")] == (1, 2)
        assert set(result) == set(patch)

    def test_an_empty_patch_is_valid(self) -> None:
        assert patch_spec().validate_patch({}) == {}

    def test_an_unknown_key_is_refused(self) -> None:
        with pytest.raises(SpecError, match="nope is not declared in the spec"):
            patch_spec().validate_patch({"nope": 1})

    def test_a_malformed_key_is_refused(self) -> None:
        with pytest.raises(SpecError, match="StateKey"):
            patch_spec().validate_patch({"Not A Key": 1})

    def test_a_computed_key_cannot_be_patched(self) -> None:
        with pytest.raises(SpecError, match="c is computed and cannot be written"):
            patch_spec().validate_patch({"c": 1})

    @pytest.mark.parametrize(
        ("patch", "message"),
        [
            ({"n": "7"}, "n is an int"),
            ({"n": 7.0}, "n is an int"),
            ({"n": True}, "n is an int"),
            ({"s": "abcdef"}, "s is 6 characters, the limit is 5"),
            ({"e": "c"}, "e is one of 2 options"),
            ({"img": {"path": "/etc/passwd"}}, "img: "),
            ({"img": {"path": "a.svg"}}, "never SVG"),
            ({"ls": ["a"]}, r"ls\[0\] must be a int"),
        ],
    )
    def test_a_value_that_does_not_fit_is_refused_naming_the_key(
        self, patch: dict[str, object], message: str
    ) -> None:
        with pytest.raises(SpecError, match=message):
            patch_spec().validate_patch(patch)

    def test_all_or_nothing_and_every_problem_is_listed(self) -> None:
        with pytest.raises(SpecError) as raised:
            patch_spec().validate_patch({"n": 1, "s": 5, "e": "zzz", "c": 1, "nope": 1})

        assert len(raised.value.problems) == 4

    def test_only_restricts_the_keys_to_a_sandbox_handlers_outputs(self) -> None:
        spec = patch_spec()
        outputs = [StateKey("n"), StateKey("s")]

        assert spec.validate_patch({"n": 1}, only=outputs) == {StateKey("n"): 1}
        with pytest.raises(SpecError, match="e is not one of the entries this may write"):
            spec.validate_patch({"n": 1, "e": "a"}, only=outputs)
        with pytest.raises(SpecError, match="c is computed"):
            spec.validate_patch({"c": 1}, only=[StateKey("c")])
        with pytest.raises(SpecError, match="not declared"):
            spec.validate_patch({"nope": 1}, only=[StateKey("nope")])

    def test_an_empty_only_allows_nothing(self) -> None:
        with pytest.raises(SpecError, match="not one of the entries"):
            patch_spec().validate_patch({"n": 1}, only=[])

    def test_a_huge_hostile_patch_is_refused_after_a_bounded_amount_of_work(self) -> None:
        patch: dict[str, object] = {f"k{i}": i for i in range(100_000)}

        with pytest.raises(SpecError) as raised:
            patch_spec().validate_patch(patch)

        assert len(raised.value.problems) == MAX_REPORTED_PROBLEMS + 1
        assert len(str(raised.value)) < 4000

    def test_it_stops_reading_a_hostile_patch_after_a_bounded_number_of_keys(self) -> None:
        looked_at: list[str] = []

        class Endless(Mapping[str, object]):
            """A million keys, produced lazily and counted as they are read."""

            def __getitem__(self, key: str) -> object:
                return int(key[1:])

            def __len__(self) -> int:
                return 1_000_000

            def __iter__(self) -> Iterator[str]:
                for i in range(1_000_000):
                    looked_at.append(f"k{i}")
                    yield f"k{i}"

        with pytest.raises(SpecError):
            patch_spec().validate_patch(Endless())

        assert len(looked_at) <= MAX_REPORTED_PROBLEMS + 2

    def test_the_patch_is_not_modified(self) -> None:
        patch: dict[str, object] = {"ls": [1, 2]}

        patch_spec().validate_patch(patch)

        assert json.dumps(patch) == '{"ls": [1, 2]}'
