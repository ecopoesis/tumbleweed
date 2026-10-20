"""Every golden spec rendered: the widget tree, and the interactions that make it live.

A signal on a widget changes the state, the computed entries follow, and every widget bound to
what changed shows it, all in the shell's process with no agent: the tip calculator is the proof.
"""

from typing import TYPE_CHECKING

import pytest

from tests.shell.gtk_support import pump_until
from tests.ui.gtk.support import golden_spec, labels_text, of_type, render
from tests.ui.image_support import FakeResolver, png
from tumbleweed.ui.runtime import Dispatch, Origin, UiEvent, UiRuntime
from tumbleweed.ui.spec.handlers import SandboxHandler
from tumbleweed.ui.spec.names import HandlerName, StateKey

if TYPE_CHECKING:
    from collections.abc import Mapping

    from tumbleweed.ui.spec.state import StateValue

pytestmark = pytest.mark.gtk


def key(name: str) -> StateKey:
    return StateKey(name)


class TestTipCalculator:
    def test_the_widget_tree(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk.text import TextBlock

        tool = render(golden_spec("tip_calculator"))

        box = tool.widget_at("root")
        grid = tool.widget_at("root.children[1]")
        assert isinstance(box, Gtk.Box)
        assert box.get_orientation() == Gtk.Orientation.VERTICAL
        assert isinstance(grid, Gtk.Grid)
        assert isinstance(tool.widget_at("root.children[0]"), TextBlock)
        assert isinstance(tool.widget_at("root.children[1].children[1]"), Gtk.SpinButton)
        assert isinstance(tool.widget_at("root.children[1].children[3]"), Gtk.Scale)
        assert isinstance(tool.widget_at("root.children[1].children[5]"), Gtk.SpinButton)
        assert isinstance(tool.widget_at("root.children[2]"), Gtk.Separator)
        for index in range(3, 7):
            assert isinstance(tool.widget_at(f"root.children[{index}]"), TextBlock)
        assert tool.widget is box

    def test_the_grid_places_children_row_major_two_columns_wide(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(golden_spec("tip_calculator"))
        grid = tool.widget_at("root.children[1]")
        assert isinstance(grid, Gtk.Grid)

        positions = []
        for index in range(6):
            child = tool.widget_at(f"root.children[1].children[{index}]")
            assert child is not None
            layout = grid.get_layout_manager()
            assert layout is not None
            cell = layout.get_layout_child(child)
            positions.append((cell.get_column(), cell.get_row()))  # type: ignore[attr-defined]  # GridLayoutChild

        assert positions == [(0, 0), (1, 0), (0, 1), (1, 1), (0, 2), (1, 2)]

    def test_it_starts_with_the_defaults_computed(self) -> None:
        tool = render(golden_spec("tip_calculator"))

        assert labels_text(tool) == [
            "Tip calculator",
            "Bill ($)",
            "Tip (%)",
            "People",
            "Tip (18%): $9.00",
            "Total: $59.00",
            "Each of 2 pays $29.50",
            "A customary tip",
        ]

    def test_the_inputs_show_the_defaults(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(golden_spec("tip_calculator"))
        bill, people = of_type(tool, Gtk.SpinButton)
        (tip,) = of_type(tool, Gtk.Scale)

        assert (bill.get_value(), tip.get_value(), people.get_value()) == (50.0, 18.0, 2.0)
        assert bill.get_digits() == 2
        assert people.get_digits() == 0

    def test_a_spin_button_and_a_slider_drive_the_whole_calculator_with_no_agent(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(golden_spec("tip_calculator"))
        bill, people = of_type(tool, Gtk.SpinButton)
        (tip,) = of_type(tool, Gtk.Scale)

        bill.set_value(120.0)
        tip.set_value(25.0)
        people.set_value(4.0)

        assert labels_text(tool)[4:] == [
            "Tip (25%): $30.00",
            "Total: $150.00",
            "Each of 4 pays $37.50",
            "A generous tip",
        ]
        store = tool.runtime.store
        assert store.value(key("bill")) == 120.0
        assert store.value(key("tip_percent")) == 25
        assert type(store.value(key("tip_percent"))) is int
        assert store.value(key("people")) == 4

    def test_the_value_changed_signal_is_what_changes_the_state(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(golden_spec("tip_calculator"))
        (tip,) = of_type(tool, Gtk.Scale)
        tip.get_adjustment().set_value(5.0)  # emits `value-changed` on the scale

        assert tool.runtime.store.value(key("tip_percent")) == 5
        assert labels_text(tool)[7] == "A modest tip"

        tip.emit("value-changed")  # emitting again with the same value changes nothing more
        assert tool.runtime.store.value(key("tip_percent")) == 5

    def test_a_slider_dragged_between_whole_numbers_writes_a_whole_number(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(golden_spec("tip_calculator"))
        (tip,) = of_type(tool, Gtk.Scale)

        tip.set_value(17.5)

        assert tool.runtime.store.value(key("tip_percent")) == 18  # half away from zero
        assert tip.get_value() == 18.0  # and the slider follows it

    def test_a_patch_updates_the_widgets_and_does_not_look_like_the_user(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(golden_spec("tip_calculator"))
        bill, _people = of_type(tool, Gtk.SpinButton)
        origins: list[Origin] = []
        tool.runtime.store.subscribe(lambda change: origins.append(change.origin))

        tool.runtime.patch_state({"bill": 80.0, "tip_percent": 10})

        assert bill.get_value() == 80.0
        assert labels_text(tool)[4:6] == ["Tip (10%): $8.00", "Total: $88.00"]
        assert Origin.USER not in origins  # the widgets' own updates are not edits

    def test_a_spin_button_that_is_typed_out_of_range_is_put_back_in_range(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(golden_spec("tip_calculator"))
        _bill, people = of_type(tool, Gtk.SpinButton)

        people.set_value(500.0)

        assert people.get_value() == 20.0
        assert tool.runtime.store.value(key("people")) == 20

    def test_one_person_has_nobody_to_share_with(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(golden_spec("tip_calculator"))
        _bill, people = of_type(tool, Gtk.SpinButton)

        people.set_value(1.0)

        assert labels_text(tool)[6] == ""
        assert labels_text(tool)[5] == "Total: $59.00"


class RecordingSandbox:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[StateKey, StateValue]]] = []

    def run(
        self,
        handler: SandboxHandler,
        event: UiEvent,
        inputs: "Mapping[StateKey, StateValue]",
    ) -> None:
        self.calls.append((handler.name, dict(inputs)))


class TestPhotoAdjuster:
    def render_with_sandbox(self) -> tuple["object", RecordingSandbox]:
        from tumbleweed.ui.gtk import RenderedTool

        sandbox = RecordingSandbox()
        spec = golden_spec("photo_adjuster")
        return RenderedTool(spec, runtime=UiRuntime(spec, sandbox=sandbox)), sandbox

    def test_the_inputs_and_the_image_which_starts_empty(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk.imageview import ImageView

        tool = render(golden_spec("photo_adjuster"))

        assert len(of_type(tool, Gtk.Entry)) == 1
        assert len(of_type(tool, Gtk.Scale)) == 2
        assert len(of_type(tool, Gtk.Switch)) == 1
        assert len(of_type(tool, Gtk.Button)) == 2
        image = tool.widget_at("root.children[2]")
        assert isinstance(image, ImageView)  # a real image widget, with nothing to show yet
        assert image.texture is None
        assert image.notice == ""

    def test_the_adjusted_photo_is_shown_when_a_handler_sets_the_image(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool
        from tumbleweed.ui.gtk.imageview import ImageView

        resolver = FakeResolver({"out/adjusted.png": png(3, 2)})
        tool = RenderedTool(golden_spec("photo_adjuster"), images=resolver)
        image = tool.widget_at("root.children[2]")
        assert isinstance(image, ImageView)

        tool.runtime.patch_state({"result": {"path": "out/adjusted.png"}})  # what S20 will do
        assert pump_until(lambda: image.texture is not None)

        texture = image.texture
        assert texture is not None
        assert (texture.get_width(), texture.get_height()) == (3, 2)
        assert [path for path, _limit in resolver.reads] == ["out/adjusted.png"]

    def test_moving_a_slider_sends_the_inputs_to_the_sandbox_sink(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        sandbox = RecordingSandbox()
        spec = golden_spec("photo_adjuster")
        tool = RenderedTool(spec, runtime=UiRuntime(spec, sandbox=sandbox))
        brightness, _contrast = of_type(tool, Gtk.Scale)

        brightness.set_value(40.0)

        assert sandbox.calls == [
            (
                "adjust",
                {
                    "photo_path": "",
                    "brightness": 40,
                    "contrast": 0,
                    "grayscale": False,
                },
            )
        ]

    def test_pressing_enter_in_the_entry_and_clicking_adjust_do_the_same(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk import RenderedTool

        sandbox = RecordingSandbox()
        spec = golden_spec("photo_adjuster")
        tool = RenderedTool(spec, runtime=UiRuntime(spec, sandbox=sandbox))
        (entry,) = of_type(tool, Gtk.Entry)
        adjust, _reset = of_type(tool, Gtk.Button)

        entry.set_text("pictures/cat.png")  # typing: written to the state, no handler on_change
        assert sandbox.calls == []
        entry.emit("activate")
        adjust.emit("clicked")

        assert tool.runtime.store.value(key("photo_path")) == "pictures/cat.png"
        assert [name for name, _ in sandbox.calls] == ["adjust", "adjust"]
        assert sandbox.calls[0][1][key("photo_path")] == "pictures/cat.png"

    def test_reset_is_a_set_handler_and_the_widgets_follow_it(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(golden_spec("photo_adjuster"))
        brightness, contrast = of_type(tool, Gtk.Scale)
        (switch,) = of_type(tool, Gtk.Switch)
        _adjust, reset = of_type(tool, Gtk.Button)
        brightness.set_value(60.0)
        contrast.set_value(-30.0)
        switch.set_active(True)
        assert labels_text(tool)[-3] == "Brightness 60, contrast -30, grayscale"

        reset.emit("clicked")

        assert (brightness.get_value(), contrast.get_value(), switch.get_active()) == (0, 0, False)
        assert labels_text(tool)[-3] == "Brightness 0, contrast 0"

    def test_a_click_with_no_sink_does_nothing_and_does_not_fail(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(golden_spec("photo_adjuster"))
        adjust, _reset = of_type(tool, Gtk.Button)

        adjust.emit("clicked")  # a sandbox handler with nowhere to go (S20)

        assert (
            tool.runtime.store.value(key("status"))
            == "Enter the path of a photo, then press Adjust."
        )


class TestCsvViewer:
    def test_the_dropdown_is_a_list_with_the_options_and_the_default_selected(self) -> None:
        from tumbleweed.ui.gtk.choice import ChoiceList

        tool = render(golden_spec("csv_viewer"))

        choices = of_type(tool, ChoiceList)
        assert len(choices) == 1
        assert choices[0].options == ("comma", "semicolon", "tab")
        assert choices[0].selected == "comma"
        assert [labels_text_of(row) for row in choices[0].rows] == ["comma", "semicolon", "tab"]

    def test_choosing_an_option_writes_it_and_a_patch_moves_the_selection(self) -> None:
        from tumbleweed.ui.gtk.choice import ChoiceList

        tool = render(golden_spec("csv_viewer"))
        (choices,) = of_type(tool, ChoiceList)
        origins: list[Origin] = []
        tool.runtime.store.subscribe(lambda change: origins.append(change.origin))

        choices.rows[2].activate()  # what a click does: selects the row
        assert tool.runtime.store.value(key("delimiter")) == "tab"
        assert choices.selected == "tab"

        tool.runtime.patch_state({"delimiter": "semicolon"})
        assert choices.selected == "semicolon"
        assert origins == [Origin.USER, Origin.PATCH]

    def test_the_switch_and_the_load_button(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(golden_spec("csv_viewer"))
        (switch,) = of_type(tool, Gtk.Switch)

        assert switch.get_active() is True
        switch.set_active(False)
        assert tool.runtime.store.value(key("has_header")) is False

    def test_the_tabs_hold_the_table_and_the_raw_text(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk.table import TableWidget

        tool = render(golden_spec("csv_viewer"))

        tabs = tool.widget_at("root.children[4]")
        assert isinstance(tabs, Gtk.Notebook)
        assert tabs.get_n_pages() == 2
        table = tool.widget_at("root.children[4].pages[0].content")
        text = tool.widget_at("root.children[4].pages[1].content")
        assert isinstance(table, TableWidget)
        assert isinstance(text, Gtk.ScrolledWindow)
        view = text.get_child()
        assert isinstance(view, Gtk.TextView)
        assert view.get_monospace() is True
        assert view.get_editable() is False  # a read-only view of the raw head of the file

    def test_a_loaded_file_fills_the_table_and_the_raw_text(self) -> None:
        from tumbleweed.shell._gi import Gtk
        from tumbleweed.ui.gtk.table import TableWidget

        tool = render(golden_spec("csv_viewer"))
        table = tool.widget_at("root.children[4].pages[0].content")
        assert isinstance(table, TableWidget)
        (view,) = of_type(tool, Gtk.TextView)

        tool.runtime.patch_state(  # what the sandbox handler's result will be (S20)
            {
                "data": {
                    "columns": [{"name": "city", "type": "string"}, {"name": "pop", "type": "int"}],
                    "rows": [["Oslo", 709000], ["Lima", 10700000]],
                },
                "raw_head": "city,pop\nOslo,709000\nLima,10700000",
                "status": "2 rows",
            }
        )
        tool.flush()

        assert table.column_names == ("city", "pop")
        assert table.cells == (("Oslo", "709000"), ("Lima", "10700000"))
        buffer = view.get_buffer()
        assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == (
            "city,pop\nOslo,709000\nLima,10700000"
        )


def labels_text_of(row: "object") -> str:
    from tests.shell.gtk_support import labels_in
    from tumbleweed.shell._gi import Gtk

    assert isinstance(row, Gtk.Widget)
    return "".join(label.get_text() for label in labels_in(row))


class TestEveryGoldenSpec:
    @pytest.mark.parametrize("name", ["tip_calculator", "photo_adjuster", "csv_viewer"])
    def test_it_renders_every_widget_for_real_with_no_placeholder(self, name: str) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render(golden_spec(name))

        placeholders = [
            w.get_text() for w in of_type(tool, Gtk.Label) if "error" in w.get_css_classes()
        ]
        assert placeholders == []  # none of the 18 kinds is a placeholder any more
        assert "cannot be shown" not in " ".join(labels_text(tool))

    def test_closing_the_tool_stops_it_following_the_state(self) -> None:
        tool = render(golden_spec("tip_calculator"))
        before = labels_text(tool)

        tool.close()
        tool.close()  # twice is harmless
        tool.runtime.patch_state({"bill": 80.0})

        assert tool.runtime.store.value(key("bill")) == 80.0  # the state still moves
        assert labels_text(tool) == before  # the widgets do not

    def test_the_dispatch_results_are_what_the_widgets_see(self) -> None:
        tool = render(golden_spec("tip_calculator"))

        result = tool.runtime.user_event(UiEvent(HandlerName("nope"), "button", "click"))

        assert result is Dispatch.IGNORED
