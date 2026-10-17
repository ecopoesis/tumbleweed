"""The widgets of the components `tumbleweed.ui/1` renderer core supports, built from the spec.

`build_widget` maps one component to one GTK widget, with `match` over the closed set of component
classes (a new one fails mypy until it is handled here). What it builds is plain:

- every text is set with `set_text` through `plain_label`, after the display transform (`text`);
- a `dropdown` is a list in place, not a popup (`choice`), and a text entry cannot open a context
  menu, an emoji chooser or a drag (`guard`): the renderer creates no surface of its own from a
  spec (#53);
- no tooltip is ever set, and no widget parses markup.

A widget is bound with `ctx.bind` (the entry it shows) and, for an input, a signal handler that
calls `ctx.user_input`. Numbers go through `NumericRange`, which makes every range and step a
validated spec can have into finite adjustment values.

The image, table, text view, tabs and expander components are S11's: until then each is a
placeholder saying so.
"""

from typing import Literal, assert_never

from tumbleweed.shell._gi import GLib, Gtk
from tumbleweed.shell.viewmodel.display import display_text
from tumbleweed.ui.gtk.choice import ChoiceList
from tumbleweed.ui.gtk.context import BuildContext
from tumbleweed.ui.gtk.guard import harden_text
from tumbleweed.ui.gtk.text import TextBlock, line_label, placeholder
from tumbleweed.ui.runtime import NumericRange, UiEvent, bind_of
from tumbleweed.ui.runtime.values import number_of
from tumbleweed.ui.spec.components import (
    Box,
    Button,
    Check,
    Component,
    Dropdown,
    Entry,
    Expander,
    Grid,
    Heading,
    Image,
    Label,
    Orientation,
    Progress,
    Separator,
    Slider,
    Spin,
    Switch,
    TableView,
    Tabs,
    TextView,
)
from tumbleweed.ui.spec.names import HandlerName, StateKey
from tumbleweed.ui.spec.state import (
    BoolState,
    EnumState,
    FloatState,
    IntState,
    StateValue,
    StringState,
)

# GTK's entry buffer holds at most this many bytes of UTF-8 (G_MAXUINT16 less a terminator): a
# `string` entry that is longer cannot be edited in one, and is shown cut and read-only instead.
ENTRY_CAPACITY_BYTES = 65_534
WARNING_CLASS = "warning"


def _orientation(orientation: Orientation) -> Gtk.Orientation:
    return Gtk.Orientation.VERTICAL if orientation == "vertical" else Gtk.Orientation.HORIZONTAL


def _event(
    handler: HandlerName | None,
    component: str,
    event: Literal["click", "change", "activate"],
    key: StateKey | None = None,
) -> UiEvent | None:
    if handler is None:
        return None
    return UiEvent(handler=handler, component=component, event=event, bind=key)


def _bound[D: (BoolState, IntState, FloatState, StringState, EnumState)](
    component: Component, ctx: BuildContext, decl_type: type[D]
) -> tuple[StateKey, D]:
    binding = bind_of(component, ctx.decls)
    if binding is None or not isinstance(binding.decl, decl_type):
        raise TypeError(f"a {component.kind} is not bound to a {decl_type.__name__}")
    return binding.key, binding.decl


def _numeric(
    component: Component, ctx: BuildContext
) -> tuple[StateKey, NumericRange, Gtk.Adjustment]:
    binding = bind_of(component, ctx.decls)
    if binding is None:
        raise TypeError(f"a {component.kind} has no binding")
    numeric = NumericRange.of(binding.decl)
    adjustment = Gtk.Adjustment.new(
        numeric.minimum, numeric.minimum, numeric.maximum, numeric.step, numeric.page, 0.0
    )
    return binding.key, numeric, adjustment


def _text_value(value: StateValue) -> str:
    return value if isinstance(value, str) else ""


# --- containers ------------------------------------------------------------------------------


def _box(component: Box, path: str, ctx: BuildContext) -> Gtk.Widget:
    box = Gtk.Box(orientation=_orientation(component.orientation), spacing=component.spacing)
    for index, child in enumerate(component.children):
        box.append(ctx.build_child(child, f"{path}.children[{index}]"))
    return box


def _grid(component: Grid, path: str, ctx: BuildContext) -> Gtk.Widget:
    grid = Gtk.Grid(row_spacing=component.row_spacing, column_spacing=component.column_spacing)
    for index, child in enumerate(component.children):
        widget = ctx.build_child(child, f"{path}.children[{index}]")
        grid.attach(widget, index % component.columns, index // component.columns, 1, 1)
    return grid


# --- display ---------------------------------------------------------------------------------


def _text(component: Label | Heading, ctx: BuildContext) -> Gtk.Widget:
    classes = [f"title-{component.level}"] if isinstance(component, Heading) else []
    block = TextBlock(css_classes=classes)
    if component.text is not None:
        block.set_text(component.text)
        return block
    binding = bind_of(component, ctx.decls)
    if binding is None:
        raise TypeError(f"a {component.kind} has neither text nor a binding")

    def update(value: StateValue, stale: bool) -> None:
        block.set_text(_text_value(value), stale=stale)

    ctx.bind(binding.key, block, update)
    return block


def _progress(component: Progress, ctx: BuildContext) -> Gtk.Widget:
    binding = bind_of(component, ctx.decls)
    if binding is None:
        raise TypeError("a progress has no binding")
    numeric = NumericRange.of(binding.decl, stepped=False)
    bar = Gtk.ProgressBar()
    bar.set_hexpand(True)

    def update(value: StateValue, stale: bool) -> None:
        number = number_of(value)
        bar.set_fraction(0.0 if stale or number is None else numeric.fraction(float(number)))
        bar.set_show_text(stale)
        bar.set_text("Out of date" if stale else None)
        if stale:
            bar.add_css_class("error")
        else:
            bar.remove_css_class("error")

    ctx.bind(binding.key, bar, update)
    return bar


def _separator(component: Separator) -> Gtk.Widget:
    return Gtk.Separator(orientation=_orientation(component.orientation))


# --- input -----------------------------------------------------------------------------------


def _button(component: Button, ctx: BuildContext) -> Gtk.Widget:
    button = Gtk.Button()
    label = line_label(component.label)
    label.set_xalign(0.5)
    button.set_child(label)
    event = _event(component.on_click, component.kind, "click")
    if event is not None:
        button.connect("clicked", lambda _button: ctx.user_event(event))
    return button


def _entry(component: Entry, ctx: BuildContext) -> Gtk.Widget:
    key, decl = _bound(component, ctx, StringState)
    entry = Gtk.Entry()
    entry.set_hexpand(True)
    entry.set_max_length(min(decl.max_length, 65_535))
    entry.set_placeholder_text(display_text(component.placeholder) or None)
    harden_text(entry)
    change = _event(component.on_change, component.kind, "change", key)
    activate = _event(component.on_activate, component.kind, "activate", key)
    editing = False  # inside this entry's own `changed`: its text must not be replaced there
    sync_pending = False

    def show(value: StateValue) -> None:
        text = _text_value(value)
        fits = len(text) * 4 <= ENTRY_CAPACITY_BYTES or len(text.encode()) <= ENTRY_CAPACITY_BYTES
        entry.set_editable(fits)
        if fits:
            entry.remove_css_class(WARNING_CLASS)
        else:
            entry.add_css_class(WARNING_CLASS)
        if entry.get_text() != text:
            entry.set_text(text)

    def sync() -> bool:
        nonlocal sync_pending
        sync_pending = False
        with ctx.programmatic():
            show(ctx.runtime.store.value(key))  # whatever the state holds by now
        return GLib.SOURCE_REMOVE

    def update(value: StateValue, stale: bool) -> None:
        nonlocal sync_pending
        if not editing:
            show(value)
        elif not sync_pending and entry.get_text() != _text_value(value):
            # The state answered an edit with another text (a handler rewrote it, the budget
            # refused it): replacing the text inside its own `changed` signal makes GTK run
            # away (every replacement is announced again), so it is done once that signal is over.
            sync_pending = True
            GLib.idle_add(sync)

    def on_changed(_entry: Gtk.Entry) -> None:
        nonlocal editing
        if ctx.updating:
            return
        editing = True
        try:
            ctx.user_input(key, entry.get_text(), change)
        finally:
            editing = False

    entry.connect("changed", on_changed)
    if activate is not None:
        entry.connect("activate", lambda _entry: ctx.user_event(activate))
    ctx.bind(key, entry, update)
    return entry


def _switch(component: Switch, ctx: BuildContext) -> Gtk.Widget:
    key, _decl = _bound(component, ctx, BoolState)
    switch = Gtk.Switch()
    switch.set_halign(Gtk.Align.START)
    switch.set_valign(Gtk.Align.CENTER)
    change = _event(component.on_change, component.kind, "change", key)

    def update(value: StateValue, stale: bool) -> None:
        if switch.get_active() != (value is True):
            switch.set_active(value is True)

    switch.connect(
        "notify::active", lambda _switch, _param: ctx.user_input(key, switch.get_active(), change)
    )
    ctx.bind(key, switch, update)
    return switch


def _check(component: Check, ctx: BuildContext) -> Gtk.Widget:
    key, _decl = _bound(component, ctx, BoolState)
    check = Gtk.CheckButton()
    check.set_child(line_label(component.label))
    change = _event(component.on_change, component.kind, "change", key)

    def update(value: StateValue, stale: bool) -> None:
        if check.get_active() != (value is True):
            check.set_active(value is True)

    check.connect("toggled", lambda _check: ctx.user_input(key, check.get_active(), change))
    ctx.bind(key, check, update)
    return check


def _slider(component: Slider, ctx: BuildContext) -> Gtk.Widget:
    key, numeric, adjustment = _numeric(component, ctx)
    scale = Gtk.Scale.new(Gtk.Orientation.HORIZONTAL, adjustment)
    scale.set_digits(numeric.digits)
    scale.set_draw_value(True)
    scale.set_value_pos(Gtk.PositionType.RIGHT)
    scale.set_hexpand(True)
    change = _event(component.on_change, component.kind, "change", key)

    def update(value: StateValue, stale: bool) -> None:
        number = number_of(value)
        if number is not None and scale.get_value() != float(number):
            scale.set_value(float(number))

    scale.connect(
        "value-changed",
        lambda _scale: ctx.user_input(key, numeric.to_state(scale.get_value()), change),
    )
    ctx.bind(key, scale, update)
    return scale


def _spin(component: Spin, ctx: BuildContext) -> Gtk.Widget:
    key, numeric, adjustment = _numeric(component, ctx)
    spin = Gtk.SpinButton.new(adjustment, 1.0, numeric.digits)  # climb rate: GTK's usual
    spin.set_numeric(True)
    harden_text(spin)
    change = _event(component.on_change, component.kind, "change", key)

    def update(value: StateValue, stale: bool) -> None:
        number = number_of(value)
        if number is not None and spin.get_value() != float(number):
            spin.set_value(float(number))

    spin.connect(
        "value-changed",
        lambda _spin: ctx.user_input(key, numeric.to_state(spin.get_value()), change),
    )
    ctx.bind(key, spin, update)
    return spin


def _dropdown(component: Dropdown, ctx: BuildContext) -> Gtk.Widget:
    key, decl = _bound(component, ctx, EnumState)
    change = _event(component.on_change, component.kind, "change", key)

    def choose(option: str) -> None:
        ctx.user_input(key, option, change)

    choices = ChoiceList(decl.options, choose)

    def update(value: StateValue, stale: bool) -> None:
        choices.select(_text_value(value))

    ctx.bind(key, choices, update)
    return choices


def build_widget(component: Component, path: str, ctx: BuildContext) -> Gtk.Widget:
    """The widget for `component`, which stands at `path` in the spec (`root.children[1]`)."""
    match component:
        case Box():
            return _box(component, path, ctx)
        case Grid():
            return _grid(component, path, ctx)
        case Label() | Heading():
            return _text(component, ctx)
        case Button():
            return _button(component, ctx)
        case Entry():
            return _entry(component, ctx)
        case Switch():
            return _switch(component, ctx)
        case Check():
            return _check(component, ctx)
        case Slider():
            return _slider(component, ctx)
        case Spin():
            return _spin(component, ctx)
        case Dropdown():
            return _dropdown(component, ctx)
        case Progress():
            return _progress(component, ctx)
        case Separator():
            return _separator(component)
        case Image() | TableView() | TextView() | Tabs() | Expander():
            return placeholder(f"A {component.kind} cannot be shown yet.")
        case _ as unreachable:
            assert_never(unreachable)
