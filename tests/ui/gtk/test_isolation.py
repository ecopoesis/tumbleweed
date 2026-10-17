"""Per-node error isolation: a valid node that fails becomes a placeholder, and the rest renders.

A malformed node never gets this far (validation refuses the whole spec, I4), so the failures here
are the other kind: a node that is valid and still cannot be built, or cannot show its entry.
`RenderedTool(factories=...)` replaces the builder of one kind, which is how a test makes it fail.
"""

import logging
from typing import TYPE_CHECKING

import pytest

from tests.ui.gtk.support import labels_text, of_type, spec_of
from tests.ui.runtime.support import flag, number, text
from tests.ui.spec.support import Json
from tumbleweed.ui.spec.names import StateKey

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk.context import BuildContext
    from tumbleweed.ui.spec.components import Component
    from tumbleweed.ui.spec.state import StateValue

pytestmark = pytest.mark.gtk

STATE = [
    number("n", 5, minimum=0, maximum=10),
    text("s", "hello"),
    flag("b"),
]
LABEL: Json = {"kind": "label", "bind": "s"}


def tree() -> Json:
    return {
        "kind": "box",
        "children": [
            LABEL,
            {"kind": "slider", "bind": "n"},
            {
                "kind": "grid",
                "columns": 2,
                "children": [
                    {"kind": "label", "text": "left"},
                    {"kind": "switch", "bind": "b"},
                    {"kind": "label", "bind": "s"},
                    {"kind": "spin", "bind": "n"},
                ],
            },
            {"kind": "label", "text": "last"},
        ],
    }


def failing(component: "Component", path: str, ctx: "BuildContext") -> "Gtk.Widget":
    raise RuntimeError("the widget could not be built")


def key(name: str) -> StateKey:
    return StateKey(name)


class TestABuilderThatRaises:
    def test_the_node_is_a_placeholder_and_its_siblings_render(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        with caplog.at_level(logging.ERROR):
            tool = RenderedTool(spec_of(tree(), STATE), factories={"slider": failing})

        placeholder = tool.widget_at("root.children[1]")
        assert isinstance(placeholder, Gtk.Label)
        assert placeholder.get_text() == "A slider could not be shown."
        assert "error" in placeholder.get_css_classes()
        assert not of_type(tool, Gtk.Scale)  # the slider is gone
        assert len(of_type(tool, Gtk.SpinButton)) == 1  # the other number widget is not
        assert isinstance(tool.widget_at("root.children[2].children[1]"), Gtk.Switch)
        assert labels_text(tool) == [
            "hello",
            "A slider could not be shown.",
            "left",
            "hello",
            "last",
        ]
        assert "root.children[1] (slider) could not be built" in caplog.text

    def test_the_text_of_the_placeholder_is_ours_and_never_the_exception(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        def leaky(component: "Component", path: str, ctx: "BuildContext") -> "Gtk.Widget":
            raise RuntimeError("<b>secret</b> from the model: ignore previous instructions")

        tool = RenderedTool(spec_of(tree(), STATE), factories={"slider": leaky})

        placeholder = tool.widget_at("root.children[1]")
        assert isinstance(placeholder, Gtk.Label)
        assert "secret" not in placeholder.get_text()

    def test_the_rest_still_follows_the_state(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(spec_of(tree(), STATE), factories={"slider": failing})

        tool.runtime.patch_state({"s": "changed", "n": 9})

        (spin,) = of_type(tool, Gtk.SpinButton)
        assert spin.get_value() == 9.0
        assert labels_text(tool)[0] == "changed"

    def test_the_failed_node_is_not_bound_to_anything(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(spec_of(tree(), STATE), factories={"label": failing})

        # Every label failed, bound or not; changing what they would have shown breaks nothing.
        tool.runtime.patch_state({"s": "x"})

        assert tool.runtime.store.value(key("s")) == "x"

    def test_a_container_that_fails_takes_its_children_with_it_and_nothing_else(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(spec_of(tree(), STATE), factories={"grid": failing})

        assert not of_type(tool, Gtk.Switch)
        assert not of_type(tool, Gtk.SpinButton)
        assert tool.widget_at("root.children[2].children[0]") is None
        assert labels_text(tool) == ["hello", "A grid could not be shown.", "last"]
        assert isinstance(tool.widget_at("root.children[1]"), Gtk.Scale)

    def test_a_child_that_fails_does_not_fail_its_container(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(spec_of(tree(), STATE), factories={"switch": failing})

        grid = tool.widget_at("root.children[2]")
        assert isinstance(grid, Gtk.Grid)
        assert isinstance(tool.widget_at("root.children[2].children[3]"), Gtk.SpinButton)
        assert isinstance(tool.widget_at("root.children[2].children[1]"), Gtk.Label)

    def test_a_root_that_fails_is_a_placeholder_too(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(spec_of(tree(), STATE), factories={"box": failing})

        assert isinstance(tool.widget, Gtk.Label)
        assert tool.widget.get_text() == "A box could not be shown."


class TestABindingThatCannotShowItsFirstValue:
    def test_the_node_becomes_a_placeholder_and_its_binding_is_forgotten(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        def broken_label(component: "Component", path: str, ctx: "BuildContext") -> "Gtk.Widget":
            widget = Gtk.Label()

            def update(value: "StateValue", stale: bool) -> None:
                raise RuntimeError("cannot show this")

            ctx.bind(key("s"), widget, update)  # raises on the first value
            return widget

        tool = RenderedTool(spec_of(tree(), STATE), factories={"label": broken_label})

        assert labels_text(tool).count("A label could not be shown.") == 4
        tool.runtime.patch_state({"s": "later"})  # nothing is left bound to `s` to fail


class TestAnUpdateThatFailsLater:
    def test_the_widget_is_disabled_and_marked_and_the_others_still_update(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        calls: list[int] = []

        def fragile(component: "Component", path: str, ctx: "BuildContext") -> "Gtk.Widget":
            widget = Gtk.Label()

            def update(value: "StateValue", stale: bool) -> None:
                calls.append(1)
                if len(calls) > 1:
                    raise RuntimeError("cannot show this any more")

            ctx.bind(key("s"), widget, update)
            return widget

        tool = RenderedTool(spec_of(tree(), STATE), factories={"label": fragile})
        (spin,) = of_type(tool, Gtk.SpinButton)

        with caplog.at_level(logging.ERROR):
            tool.runtime.patch_state({"s": "again", "n": 7})

        assert "a widget could not show s" in caplog.text
        assert spin.get_value() == 7.0  # the number widgets were not affected
        first = tool.widget_at("root.children[0]")
        assert first is not None
        assert not first.get_sensitive()
        assert "error" in first.get_css_classes()


class TestKindsThisStepDoesNotRenderYet:
    def test_each_is_a_placeholder_naming_the_kind_and_the_rest_renders(self) -> None:
        from tumbleweed.shell._gi import Gtk

        state: list[Json] = [
            *STATE,
            {"kind": "image", "key": "img"},
            {"kind": "table", "key": "tab"},
        ]
        root: Json = {
            "kind": "box",
            "children": [
                {"kind": "image", "bind": "img"},
                {"kind": "table", "bind": "tab"},
                {"kind": "text_view", "bind": "s"},
                {"kind": "tabs", "pages": [{"title": "T", "content": LABEL}]},
                {"kind": "expander", "title": "E", "child": LABEL},
                LABEL,
            ],
        }
        from tests.ui.gtk.support import render

        tool = render(spec_of(root, state))

        shown = labels_text(tool)
        assert shown == [
            "A image cannot be shown yet.",
            "A table cannot be shown yet.",
            "A text_view cannot be shown yet.",
            "A tabs cannot be shown yet.",
            "A expander cannot be shown yet.",
            "hello",
        ]
        assert tool.widget_at("root.children[3].pages[0].content") is None
        assert isinstance(tool.widget_at("root.children[5]"), Gtk.Box)
