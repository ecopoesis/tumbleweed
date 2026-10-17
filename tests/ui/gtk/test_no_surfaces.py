"""The renderer creates no surface that could cover trusted chrome from a spec (#53, I5).

Clipping the canvas contains what is drawn in the window's own surface, and nothing else: a
popover, a dialog, a tooltip and a drag icon are windows of their own. So the renderer must not
create them from a spec, and these tests look for them, on the real shell window, after the
golden specs and a hostile one have been rendered and every widget in them has been used:

- no toplevel (`Gtk.Window.list_toplevels`) but the shell's own window;
- no `Gtk.Native` below the window that is not the window (a popover, a popup menu, a drag icon
  is one), no popover-bearing widget (menu button, drop-down, combo box, dialog button), no
  tooltip on anything, no drag source;
- a text entry or spin button cannot open its context menu or emoji chooser (the actions are
  disabled) and has no long-press, and the control that proves the search is not blind is an
  entry that has not been made safe, which does grow a popover.
"""

import pytest

from tests.shell.gtk_support import Shell, descendants, pump, pump_until
from tests.ui.gtk.hostile import hostile_spec
from tests.ui.gtk.support import golden_spec

pytestmark = pytest.mark.gtk

SPECS = {
    "tip_calculator": lambda: golden_spec("tip_calculator"),
    "photo_adjuster": lambda: golden_spec("photo_adjuster"),
    "csv_viewer": lambda: golden_spec("csv_viewer"),
    "hostile": hostile_spec,
}


def present(shell: Shell) -> None:
    shell.window.present()
    assert pump_until(lambda: shell.window.get_mapped() and shell.window.canvas.get_width() > 0)
    pump(0.1)


def use_every_widget(shell: Shell) -> None:
    """Do to each widget of the tool what a user does, as far as a test can."""
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk.choice import ChoiceList

    for widget in list(descendants(shell.window.canvas)):
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


def surfaces_below(window: object) -> list[object]:
    """Every native surface-bearing widget under the window that is not the window itself."""
    from tumbleweed.shell._gi import Gtk

    assert isinstance(window, Gtk.Window)
    below = list(descendants(window))[1:]  # the window is the first of its own descendants
    return [w for w in below if isinstance(w, Gtk.Native)]


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
    echo_shell.window.canvas.show_spec(SPECS[name]())
    present(echo_shell)
    assert_no_surface(echo_shell)  # as rendered

    use_every_widget(echo_shell)

    assert_no_surface(echo_shell)  # and after every widget was used


def test_the_hostile_spec_really_has_every_kind_of_widget(echo_shell: Shell) -> None:
    # Guards the test above against a hostile spec that quietly stopped covering a widget.
    from tumbleweed.shell._gi import Gtk
    from tumbleweed.ui.gtk.choice import ChoiceList

    echo_shell.window.canvas.show_spec(hostile_spec())
    kinds = {type(w) for w in descendants(echo_shell.window.canvas)}

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
    ):
        assert expected in kinds, expected


def entries(shell: Shell) -> list["object"]:
    from tumbleweed.shell._gi import Gtk

    return [
        w for w in descendants(shell.window.canvas) if isinstance(w, Gtk.Entry | Gtk.SpinButton)
    ]


@pytest.mark.parametrize("action", ["menu.popup", "misc.insert-emoji"])
def test_the_context_menu_and_the_emoji_chooser_of_a_text_widget_cannot_be_opened(
    echo_shell: Shell, action: str
) -> None:
    from tumbleweed.shell._gi import Gtk

    echo_shell.window.canvas.show_spec(hostile_spec())
    present(echo_shell)
    texts = entries(echo_shell)
    assert len(texts) >= 6

    for widget in texts:
        assert isinstance(widget, Gtk.Editable)
        delegate = widget.get_delegate()
        assert isinstance(delegate, Gtk.Text)
        delegate.activate_action(action, None)  # (GTK says True even for a disabled action)
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


def test_a_text_widget_has_no_long_press_and_a_capture_click_that_denies_the_menu(
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
        assert len(capture) == 1
        assert capture[0].get_button() == 0  # every button


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
