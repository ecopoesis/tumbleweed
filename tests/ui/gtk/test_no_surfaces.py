"""The renderer creates no surface that could cover trusted chrome from a spec (#53, I5).

Clipping the canvas contains what is drawn in the window's own surface, and nothing else: a
popover, a dialog, a tooltip and a drag icon are windows of their own. So the renderer must not
create them from a spec, and these tests look for them, on the real shell window, after the
golden specs and a hostile one have been rendered and every widget in them has been used:

- no toplevel (`Gtk.Window.list_toplevels`) but the shell's own window;
- no `Gtk.Native` below the window that is not the window (a popover, a popup menu, a drag icon
  is one), no popover-bearing widget (menu button, drop-down, combo box, dialog button), no
  tooltip on anything, no drag source;
- a text entry, spin button or text view cannot open its context menu or emoji chooser (the
  actions are disabled) and has no long-press, and the controls that prove the search is not blind
  are an entry and a text view that have not been made safe, which do grow a popover;
- the rich widgets (S11) are in every spec searched: an image (a real one, decoded), a table of
  hostile cells, text views (read-only and editable, typed into), tabs (every page visited) and
  expanders (opened and closed). None creates a tooltip, a popover, a menu or a drag source.
"""

from typing import TYPE_CHECKING

import pytest

from tests.shell.gtk_support import Shell, descendants, pump, pump_until
from tests.ui.gtk.hostile import hostile_spec
from tests.ui.gtk.support import golden_spec
from tests.ui.image_support import FakeResolver, png

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gtk

pytestmark = pytest.mark.gtk

SPECS = {
    "tip_calculator": lambda: golden_spec("tip_calculator"),
    "photo_adjuster": lambda: golden_spec("photo_adjuster"),
    "csv_viewer": lambda: golden_spec("csv_viewer"),
    "hostile": hostile_spec,
}


def images() -> FakeResolver:
    """A workspace with the images the specs above point at (`-rf.png` is the hostile name)."""
    return FakeResolver({"-rf.png": png(3, 2), "out/adjusted.png": png(4, 3)})


def show(shell: Shell, name: str) -> None:
    """Render the spec called `name` on the canvas, with an image of its own to load."""
    tool = shell.window.canvas.show_spec(SPECS[name](), images=images())
    if name == "photo_adjuster":
        tool.runtime.patch_state({"result": {"path": "out/adjusted.png"}})
    present(shell)


def present(shell: Shell) -> None:
    from tumbleweed.ui.gtk.imageview import ImageView

    shell.window.present()
    assert pump_until(lambda: shell.window.get_mapped() and shell.window.canvas.get_width() > 0)
    views = [w for w in descendants(shell.window.canvas) if isinstance(w, ImageView)]
    # (an image arrives from a worker thread: wait until each one has shown what it has)
    assert pump_until(
        lambda: all(v.texture is not None or v.notice != "Loading the image..." for v in views)
    )
    pump(0.1)


def use_every_widget(shell: Shell) -> None:
    """Do to each widget of the tool what a user does, as far as a test can."""
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk.choice import ChoiceList

    for widget in list(descendants(shell.window.canvas)):
        if isinstance(widget, Gtk.Notebook):
            for index in range(widget.get_n_pages()):
                widget.set_current_page(index)  # every tab, as a click on it would
        elif isinstance(widget, Gtk.Expander):
            widget.set_expanded(not widget.get_expanded())
            widget.set_expanded(not widget.get_expanded())
            widget.set_expanded(True)
        elif isinstance(widget, Gtk.TextView):
            widget.get_buffer().insert_at_cursor("typed")
            widget.emit("move-cursor", Gtk.MovementStep.BUFFER_ENDS, -1, True)  # select all
        if isinstance(widget, Gtk.Button) and not isinstance(widget.get_parent(), Gtk.SpinButton):
            widget.emit("clicked")
        elif isinstance(widget, Gtk.Switch | Gtk.CheckButton):
            widget.set_active(not widget.get_active())
        elif isinstance(widget, Gtk.Scale | Gtk.SpinButton):
            adjustment = widget.get_adjustment()
            adjustment.set_value(adjustment.get_upper())
            adjustment.set_value(adjustment.get_lower())
        elif isinstance(widget, Gtk.Entry):
            widget.set_text("typed")
            widget.emit("activate")
        elif isinstance(widget, ChoiceList):
            widget.rows[-1].activate()
        widget.grab_focus()
    pump(0.1)


def is_idle_tooltip_window(widget: object) -> bool:
    """GTK's own tooltip window, which it creates under a window the first time a pointer moves
    over it (so it is there or not depending on the pointer, not on the spec), and keeps hidden
    unless a widget asks for a tooltip, which none does (checked in `assert_no_surface`)."""
    from tumbleweed.shell._gi import Gtk

    return (
        type(widget).__name__ == "GtkTooltipWindow"
        and isinstance(widget, Gtk.Widget)
        and not widget.get_mapped()
    )


def surfaces_below(window: object) -> list[object]:
    """Every native surface-bearing widget under the window that is not the window itself."""
    from tumbleweed.shell._gi import Gtk

    assert isinstance(window, Gtk.Window)
    below = list(descendants(window))[1:]  # the window is the first of its own descendants
    return [w for w in below if isinstance(w, Gtk.Native) and not is_idle_tooltip_window(w)]


POPOVER_BEARING = (
    "MenuButton",
    "DropDown",
    "ComboBox",
    "ColorButton",
    "FontButton",
    "ColorDialogButton",
    "FontDialogButton",
    "AppChooserButton",
    "EmojiChooser",
    "Popover",
    "PopoverMenu",
    "PopoverMenuBar",
    "Dialog",
    "AboutDialog",
    "FileChooserNative",
)


def assert_no_surface(shell: Shell) -> None:
    from tumbleweed.shell._gi import Gtk

    window = shell.window
    assert (
        Gtk.Window.list_toplevels() == [window]
        or [w for w in Gtk.Window.list_toplevels() if w is not window] == []
    )
    assert surfaces_below(window) == []
    for widget in descendants(window):
        assert type(widget).__name__ not in POPOVER_BEARING, type(widget).__name__
        assert not isinstance(widget, Gtk.Popover)
        assert not widget.get_has_tooltip(), f"{type(widget).__name__} has a tooltip"
        assert widget.get_tooltip_text() is None
        assert widget.get_tooltip_markup() is None
        for controller in widget.observe_controllers():
            assert not isinstance(controller, Gtk.DragSource), type(widget).__name__


@pytest.mark.parametrize("name", list(SPECS))
def test_a_rendered_spec_creates_no_surface_besides_the_main_window(
    echo_shell: Shell, name: str
) -> None:
    show(echo_shell, name)
    assert_no_surface(echo_shell)  # as rendered

    use_every_widget(echo_shell)

    assert_no_surface(echo_shell)  # and after every widget was used


def test_the_hostile_spec_really_has_every_kind_of_widget(echo_shell: Shell) -> None:
    # Guards the test above against a hostile spec that quietly stopped covering a widget.
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk.choice import ChoiceList
    from tumbleweed.ui.gtk.imageview import ImageView
    from tumbleweed.ui.gtk.table import TableWidget

    show(echo_shell, "hostile")
    kinds = {type(w) for w in descendants(echo_shell.window.canvas)}
    images_shown = [
        w for w in descendants(echo_shell.window.canvas) if isinstance(w, ImageView) and w.texture
    ]

    for expected in (
        Gtk.Entry,
        Gtk.SpinButton,
        Gtk.Scale,
        Gtk.Switch,
        Gtk.CheckButton,
        Gtk.Button,
        Gtk.ProgressBar,
        Gtk.Separator,
        Gtk.Grid,
        Gtk.ListBox,
        ChoiceList,
        Gtk.TextView,
        Gtk.Notebook,
        Gtk.Expander,
        Gtk.ColumnView,
        Gtk.Picture,
        ImageView,
        TableWidget,
    ):
        assert expected in kinds, expected
    assert images_shown  # a real image was decoded and is on show, not only a placeholder


def entries(shell: Shell) -> list["object"]:
    from tumbleweed.shell._gi import Gtk

    return [
        w for w in descendants(shell.window.canvas) if isinstance(w, Gtk.Entry | Gtk.SpinButton)
    ]


def text_views(shell: Shell) -> list["Gtk.TextView"]:
    from tumbleweed.shell._gi import Gtk

    return [w for w in descendants(shell.window.canvas) if isinstance(w, Gtk.TextView)]


@pytest.mark.parametrize("action", ["menu.popup", "misc.insert-emoji"])
def test_the_context_menu_and_the_emoji_chooser_of_a_text_widget_cannot_be_opened(
    echo_shell: Shell, action: str
) -> None:
    from tumbleweed.shell._gi import Gtk

    show(echo_shell, "hostile")
    texts = entries(echo_shell)
    assert len(texts) >= 6
    views = text_views(echo_shell)
    assert len(views) >= 4  # the text views of the hostile spec, in tabs and expanders too

    for widget in texts:
        assert isinstance(widget, Gtk.Editable)
        delegate = widget.get_delegate()
        assert isinstance(delegate, Gtk.Text)
        delegate.activate_action(action, None)  # (GTK says True even for a disabled action)
    for view in views:
        view.activate_action(action, None)
    pump(0.2)

    assert_no_surface(echo_shell)  # ... but it opened nothing


def test_the_search_for_surfaces_is_not_blind(echo_shell: Shell) -> None:
    # The same action, on an entry nobody made safe, does open a popover under the window.
    from tumbleweed.shell._gi import Gtk

    window = echo_shell.window
    entry = Gtk.Entry()
    window.canvas.set_content(entry)
    present(echo_shell)
    assert surfaces_below(window) == []
    delegate = entry.get_delegate()
    assert isinstance(delegate, Gtk.Text)

    delegate.activate_action("menu.popup", None)
    pump(0.2)

    found = surfaces_below(window)
    assert found, "an unguarded entry's context menu did not show up as a native widget"
    assert any(isinstance(w, Gtk.Popover) for w in found)
    delegate.activate_action("menu.popup", None)  # (it is closed by the window's teardown)


def test_a_text_widget_has_no_long_press_and_capture_clicks_that_deny_the_menu_and_touch(
    echo_shell: Shell,
) -> None:
    from tumbleweed.shell._gi import Gtk

    echo_shell.window.canvas.show_spec(hostile_spec())

    for widget in entries(echo_shell):
        assert isinstance(widget, Gtk.Editable)
        delegate = widget.get_delegate()
        assert isinstance(delegate, Gtk.Text)
        controllers = list(delegate.observe_controllers())
        assert not [c for c in controllers if isinstance(c, Gtk.GestureLongPress)]
        capture = [
            c
            for c in controllers
            if isinstance(c, Gtk.GestureClick)
            and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
        ]
        pointer = [c for c in capture if not c.get_touch_only()]
        touch = [c for c in capture if c.get_touch_only()]
        assert len(pointer) == 1
        assert pointer[0].get_button() == 0  # every button
        assert len(touch) == 1  # touchscreen sequences are claimed: no selection bubble, handles


def test_a_secondary_press_is_claimed_and_any_other_press_drops_the_selection() -> None:
    from tumbleweed.shell._gi import Gdk, Gtk
    from tumbleweed.ui.gtk.guard import on_press

    class Gesture:
        def __init__(self) -> None:
            self.states: list[Gtk.EventSequenceState] = []

        def set_state(self, state: Gtk.EventSequenceState) -> bool:
            self.states.append(state)
            return True

    entry = Gtk.Entry()
    entry.set_text("select this text")
    entry.select_region(2, 8)
    assert entry.get_selection_bounds()

    secondary = Gesture()
    on_press(Gdk.BUTTON_SECONDARY, secondary, entry)
    assert secondary.states == [Gtk.EventSequenceState.CLAIMED]  # the text's gestures are denied
    assert entry.get_selection_bounds()  # and the selection is not touched

    primary = Gesture()
    on_press(Gdk.BUTTON_PRIMARY, primary, entry)
    assert primary.states == []
    assert not entry.get_selection_bounds()  # no selection left to start a drag from


def test_a_dropdown_is_a_list_in_place_not_a_popup(echo_shell: Shell) -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk.choice import ChoiceList

    echo_shell.window.canvas.show_spec(golden_spec("csv_viewer"))
    present(echo_shell)

    widgets = list(descendants(echo_shell.window.canvas))

    assert not [w for w in widgets if isinstance(w, Gtk.DropDown | Gtk.ComboBox)]
    (choices,) = [w for w in widgets if isinstance(w, ChoiceList)]
    assert choices.get_mapped()
    assert not [w for w in descendants(choices) if isinstance(w, Gtk.Popover)]


def test_a_touch_is_claimed_and_focuses_the_text() -> None:
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk.guard import on_touch

    class Gesture:
        def __init__(self) -> None:
            self.states: list[Gtk.EventSequenceState] = []

        def set_state(self, state: Gtk.EventSequenceState) -> bool:
            self.states.append(state)
            return True

    class Text:
        focused = 0

        def grab_focus(self) -> bool:
            self.focused += 1
            return True

    gesture, text = Gesture(), Text()

    on_touch(gesture, text)

    assert gesture.states == [Gtk.EventSequenceState.CLAIMED]  # the text's own gestures: denied
    assert text.focused == 1


def test_the_search_is_not_blind_to_a_text_view_either(echo_shell: Shell) -> None:
    # An unhardened text view's context menu is a native widget under the window. (Not its emoji
    # chooser: that one needs GSettings schemas that a bare development host may lack, and aborts.)
    from tumbleweed.shell._gi import Gtk

    window = echo_shell.window
    view = Gtk.TextView()
    window.canvas.set_content(view)
    present(echo_shell)
    assert surfaces_below(window) == []

    view.activate_action("menu.popup", None)
    pump(0.2)

    found = surfaces_below(window)
    assert any(isinstance(w, Gtk.Popover) for w in found)
    view.activate_action("menu.popup", None)  # (it is closed by the window's teardown)


def test_a_text_view_has_no_long_press_and_capture_clicks_that_deny_the_menu_and_touch(
    echo_shell: Shell,
) -> None:
    from tumbleweed.shell._gi import Gtk

    show(echo_shell, "hostile")
    views = text_views(echo_shell)
    assert len(views) >= 4

    for view in views:
        controllers = list(view.observe_controllers())
        assert not [c for c in controllers if isinstance(c, Gtk.GestureLongPress)]
        capture = [
            c
            for c in controllers
            if isinstance(c, Gtk.GestureClick)
            and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
        ]
        assert len([c for c in capture if not c.get_touch_only() and c.get_button() == 0]) == 1
        assert len([c for c in capture if c.get_touch_only()]) == 1
        assert view.get_input_hints() & Gtk.InputHints.NO_EMOJI
        assert view.get_extra_menu() is None


def test_a_press_in_a_text_view_drops_the_selection_a_drag_would_start_from(
    echo_shell: Shell,
) -> None:
    from tumbleweed.shell._gi import Gdk, Gtk
    from tumbleweed.ui.gtk.guard import _ViewSelection, on_press

    show(echo_shell, "hostile")
    (view, *_rest) = text_views(echo_shell)
    buffer = view.get_buffer()
    buffer.set_text("select this text", -1)
    buffer.select_range(buffer.get_iter_at_offset(2), buffer.get_iter_at_offset(8))
    assert buffer.get_has_selection()
    pointer = [
        c
        for c in view.observe_controllers()
        if isinstance(c, Gtk.GestureClick)
        and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
        and not c.get_touch_only()
    ]

    class Gesture:
        states: list[Gtk.EventSequenceState]

        def __init__(self) -> None:
            self.states = []

        def set_state(self, state: Gtk.EventSequenceState) -> bool:
            self.states.append(state)
            return True

    secondary = Gesture()
    on_press(Gdk.BUTTON_SECONDARY, secondary, _ViewSelection(view))
    assert secondary.states == [Gtk.EventSequenceState.CLAIMED]  # the text view's gestures: denied
    assert buffer.get_has_selection()  # and the selection is not touched

    pointer[0].emit("pressed", 1, 0.0, 0.0)  # any other press, through the real handler

    assert not buffer.get_has_selection()  # nothing left to start a drag from
    assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == (
        "select this text"
    )  # (the text itself is untouched)


def test_a_touch_on_a_text_view_is_claimed_and_focuses_it(echo_shell: Shell) -> None:
    from tumbleweed.shell._gi import Gtk

    show(echo_shell, "hostile")
    (view, *_rest) = text_views(echo_shell)
    touch = [
        c
        for c in view.observe_controllers()
        if isinstance(c, Gtk.GestureClick) and c.get_touch_only()
    ]
    assert len(touch) == 1

    touch[0].emit("pressed", 1, 0.0, 0.0)

    assert view.is_focus()


def test_a_press_that_opens_a_context_menu_on_this_platform_is_claimed_whatever_its_button() -> (
    None
):
    # macOS opens the menu on Ctrl+click: the primary button. GDK says so with
    # `triggers_context_menu`, and the guard goes by that as well as by the button.
    from tumbleweed.shell._gi import Gdk, Gtk
    from tumbleweed.ui.gtk.guard import _opens_menu, on_press

    class Gesture:
        def __init__(self) -> None:
            self.states: list[Gtk.EventSequenceState] = []

        def set_state(self, state: Gtk.EventSequenceState) -> bool:
            self.states.append(state)
            return True

    class Selectable:
        dropped = 0

        def select_region(self, start_pos: int, end_pos: int) -> None:
            self.dropped += 1

    menu, plain = Gesture(), Gesture()
    text = Selectable()

    on_press(Gdk.BUTTON_PRIMARY, menu, text, triggers_menu=True)
    on_press(Gdk.BUTTON_PRIMARY, plain, text)

    assert menu.states == [Gtk.EventSequenceState.CLAIMED]
    assert plain.states == []
    assert text.dropped == 1  # only the ordinary press dropped the selection

    class Event:
        def __init__(self, opens: bool) -> None:
            self.opens = opens

        def triggers_context_menu(self) -> bool:
            return self.opens

    class Click:
        def __init__(self, event: Event | None) -> None:
            self.event = event

        def get_current_event(self) -> Event | None:
            return self.event

    assert _opens_menu(Click(Event(True))) is True  # type: ignore[arg-type]  # a stand-in
    assert _opens_menu(Click(Event(False))) is False  # type: ignore[arg-type]
    assert _opens_menu(Click(None)) is False  # type: ignore[arg-type]


def test_a_selection_in_a_text_view_cannot_start_a_drag_with_an_icon(echo_shell: Shell) -> None:
    # A drag starts from a press inside a selection. The capture-phase press (the real handler)
    # drops it first, and nothing the drag would show (a drag icon is a surface) exists after.
    from tumbleweed.shell._gi import Gtk

    show(echo_shell, "hostile")
    (view, *_rest) = text_views(echo_shell)
    buffer = view.get_buffer()
    buffer.set_text("drag this text away", -1)
    buffer.select_range(buffer.get_iter_at_offset(0), buffer.get_iter_at_offset(9))
    pointer = [
        c
        for c in view.observe_controllers()
        if isinstance(c, Gtk.GestureClick)
        and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
        and not c.get_touch_only()
    ]
    pointer[0].emit("pressed", 1, 0.0, 0.0)
    pump(0.2)

    assert not buffer.get_has_selection()
    assert not [w for w in descendants(echo_shell.window) if isinstance(w, Gtk.DragIcon)]
    assert_no_surface(echo_shell)
