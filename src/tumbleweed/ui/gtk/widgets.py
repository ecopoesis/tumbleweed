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

The rich widgets are each in a module of their own, built from here: `imageview` (an image,
resolved, inspected and decoded off the main thread), `table` (a column view of plain-text cells)
and `textview` (a text view with the same bounds and hardening as an entry). Tabs are a
`Gtk.Notebook` and an expander a `Gtk.Expander`, with plain titles; neither has a popup (the
notebook's tab menu is off) and their pages and child are built as any child is, so a page that
fails is a placeholder inside its tab.
"""

from collections.abc import Callable
from typing import Literal, assert_never, final

from tumbleweed.shell._gi import Gtk, Pango
from tumbleweed.shell.viewmodel.display import display_text
from tumbleweed.ui.gtk.budget import MAX_SHOWN_PER_TEXT, NOT_SHOWN
from tumbleweed.ui.gtk.choice import ChoiceList
from tumbleweed.ui.gtk.context import BuildContext
from tumbleweed.ui.gtk.guard import harden_text, quiet_spin_input_marshalling
from tumbleweed.ui.gtk.imageview import ImageView
from tumbleweed.ui.gtk.table import TableWidget
from tumbleweed.ui.gtk.text import TextBlock, line_label
from tumbleweed.ui.gtk.textview import make_text_view
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
    ImageState,
    IntState,
    StateValue,
    StringState,
    TableState,
)
from tumbleweed.ui.spec.values import ImageRef, Table

WARNING_CLASS = "warning"
WARNING_ICON = "dialog-warning-symbolic"
TITLE_WIDTH_CHARS = 32  # a tab's title is this wide at most, then an ellipsis


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


def _bound[D: (BoolState, IntState, FloatState, StringState, EnumState, ImageState, TableState)](
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


def _title(text: str, ctx: BuildContext) -> Gtk.Label:
    """A tab's or an expander's title: one plain line, counted in the text budget."""
    label = line_label(text, budget=ctx.budget)
    label.set_wrap(False)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    label.set_max_width_chars(TITLE_WIDTH_CHARS)
    return label


def _tabs(component: Tabs, path: str, ctx: BuildContext) -> Gtk.Widget:
    notebook = Gtk.Notebook(scrollable=True)
    notebook.popup_disable()  # (off by default: the tab menu is a popover, so it stays off)
    for index, page in enumerate(component.pages):
        content = ctx.build_child(page.content, f"{path}.pages[{index}].content")
        notebook.append_page(content, _title(page.title, ctx))
    return notebook


def _expander(component: Expander, path: str, ctx: BuildContext) -> Gtk.Widget:
    expander = Gtk.Expander()
    expander.set_label_widget(_title(component.title, ctx))
    expander.set_child(ctx.build_child(component.child, f"{path}.child"))
    expander.set_expanded(component.expanded)
    return expander


# --- display ---------------------------------------------------------------------------------


def _text(component: Label | Heading, ctx: BuildContext) -> Gtk.Widget:
    classes = [f"title-{component.level}"] if isinstance(component, Heading) else []
    block = TextBlock(css_classes=classes, budget=ctx.budget)
    if component.text is not None:
        block.set_text(component.text)
        return block
    binding = bind_of(component, ctx.decls)
    if binding is None:
        raise TypeError(f"a {component.kind} has neither text nor a binding")

    def update(value: StateValue, stale: bool) -> None:
        ctx.show_text(block, _text_value(value), stale=stale)

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


def _image(component: Image, ctx: BuildContext) -> Gtk.Widget:
    key, _decl = _bound(component, ctx, ImageState)
    view = ImageView(component.alt, ctx.images, ctx.decoder, ctx.budget, ctx.pixels)
    ctx.on_close(view.release)

    def update(value: StateValue, stale: bool) -> None:
        # (an equal ref assigned again is read again, but not when the widgets are put back)
        view.set_ref(
            value if isinstance(value, ImageRef) else None,
            stale=stale,
            refresh=not ctx.resyncing,
        )

    ctx.bind(key, view, update)
    return view


def _table(component: TableView, ctx: BuildContext) -> Gtk.Widget:
    key, _decl = _bound(component, ctx, TableState)
    table = TableWidget(ctx)
    ctx.on_close(table.release)
    first = True

    def update(value: StateValue, stale: bool) -> None:
        nonlocal first
        if first:
            first = False
            table.set_table(value if isinstance(value, Table) else None, stale=stale)
            return
        # A table costs a rebuild to show: once per frame, the latest value only.
        store = ctx.runtime.store

        def later() -> None:
            current = store.value(key)
            table.set_table(
                current if isinstance(current, Table) else None, stale=store.is_stale(key)
            )

        ctx.defer(table, later)

    ctx.bind(key, table, update)
    return table


def _text_view(component: TextView, ctx: BuildContext) -> Gtk.Widget:
    key, decl = _bound(component, ctx, StringState)
    return make_text_view(component, key, decl, ctx)


def _separator(component: Separator) -> Gtk.Widget:
    return Gtk.Separator(orientation=_orientation(component.orientation))


# --- input -----------------------------------------------------------------------------------


def _button(component: Button, ctx: BuildContext) -> Gtk.Widget:
    button = Gtk.Button()
    label = line_label(component.label, budget=ctx.budget)
    label.set_xalign(0.5)
    button.set_child(label)
    event = _event(component.on_click, component.kind, "click")
    if event is not None:
        ctx.connect(button, "clicked", lambda _button: ctx.user_event(event))
    return button


def _entry(component: Entry, ctx: BuildContext) -> Gtk.Widget:
    key, decl = _bound(component, ctx, StringState)
    entry = Gtk.Entry()
    entry.set_hexpand(True)
    # An entry shows (and so lets the user edit) at most what a label may show: typing past it
    # would make the text a layout has to shape unbounded, as the labels' is not.
    entry.set_max_length(min(decl.max_length, MAX_SHOWN_PER_TEXT))
    placeholder = component.placeholder
    if placeholder and ctx.budget.fits((entry, "placeholder"), placeholder):
        entry.set_placeholder_text(display_text(placeholder))  # (a placeholder is laid out too)
    harden_text(entry, ctx.connect)
    change = _event(component.on_change, component.kind, "change", key)
    activate = _event(component.on_activate, component.kind, "activate", key)
    editing = False  # inside this entry's own `changed`: it shows what the user typed already
    first = True

    def focused() -> bool:
        delegate = entry.get_delegate()  # the Gtk.Text inside, which is what has the focus
        return isinstance(delegate, Gtk.Text) and delegate.is_focus()

    def settle() -> None:
        # Once the user is done (the next idle turn after an edit, and when the focus leaves) the
        # claim is made again as any other: it fits, or the entry is marked like the others.
        ctx.defer(entry, lambda: show(ctx.runtime.store.value(key)))

    def show(value: StateValue) -> None:
        text = _text_value(value)
        # Editable only while the whole text is shown: it is cut at the per-text cap and refused
        # (with a mark) when the tool's budget of text on the screen is spent, and then an edit
        # would write the cut text over the state's value. The state keeps all of it.
        shown = text[:MAX_SHOWN_PER_TEXT]
        whole = len(text) <= MAX_SHOWN_PER_TEXT
        # The entry the user is in keeps its claim (it must not turn into a mark under their
        # fingers), so at most that one entry is over the budget, by at most its cap.
        if ctx.budget.claim(entry, len(shown), keep=editing or focused()):
            ctx.budget.stop_waiting(entry)
        else:
            shown, whole = NOT_SHOWN, False
            ctx.budget.wait_for_room(entry, lambda: show(ctx.runtime.store.value(key)))
        entry.set_editable(whole)
        entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY, None if whole else WARNING_ICON
        )
        entry.set_icon_activatable(Gtk.EntryIconPosition.SECONDARY, False)
        if whole:
            entry.remove_css_class(WARNING_CLASS)
        else:
            entry.add_css_class(WARNING_CLASS)
        if entry.get_text() != shown:
            position = entry.get_position()
            entry.set_text(shown)  # which puts the cursor at the start
            entry.set_position(min(position, len(entry.get_text())))

    def update(value: StateValue, stale: bool) -> None:
        nonlocal first
        if editing or first:
            first = False
            show(value)  # the first time, and what the user is typing: at once
        else:
            # Anything else waits for the next idle turn, with the latest value only: a text
            # that costs a layout to show is not laid out again for every change of a slider.
            ctx.defer(entry, lambda: show(ctx.runtime.store.value(key)))

    def on_changed(_entry: object) -> None:
        nonlocal editing
        if ctx.updating:
            return
        editing = True
        try:
            ctx.user_input(key, entry.get_text(), change)
        finally:
            editing = False
        settle()

    focus = Gtk.EventControllerFocus()
    entry.add_controller(focus)
    ctx.connect(focus, "leave", lambda _controller: settle())
    ctx.connect(entry, "changed", on_changed)
    if activate is not None:
        ctx.connect(entry, "activate", lambda _entry: ctx.user_event(activate))
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

    ctx.connect(
        switch,
        "notify::active",
        lambda _switch, _param: ctx.user_input(key, switch.get_active(), change),
    )
    ctx.bind(key, switch, update)
    return switch


def _check(component: Check, ctx: BuildContext) -> Gtk.Widget:
    key, _decl = _bound(component, ctx, BoolState)
    check = Gtk.CheckButton()
    check.set_child(line_label(component.label, budget=ctx.budget))
    change = _event(component.on_change, component.kind, "change", key)

    def update(value: StateValue, stale: bool) -> None:
        if check.get_active() != (value is True):
            check.set_active(value is True)

    ctx.connect(check, "toggled", lambda _check: ctx.user_input(key, check.get_active(), change))
    ctx.bind(key, check, update)
    return check


@final
class _Memory:
    """What a slider or spin button was last told, and by whom."""

    def __init__(self) -> None:
        self.programmatic: float | None = None  # the value the runtime last gave the adjustment
        self.typed = False  # the value being set is what the user typed: exact, never snapped


def _number_changed(
    adjustment: Gtk.Adjustment,
    numeric: NumericRange,
    ctx: BuildContext,
    key: StateKey,
    change: UiEvent | None,
    memory: _Memory,
) -> Callable[[object], None]:
    """The `value-changed` handler of a slider or spin button.

    A report of the value the runtime last set is not an edit, however late GTK makes it (a
    handler that writes back a tiny value would otherwise be answered with an edit, and again,
    for ever). Of what is left, a value that is the held value stepped by a whole number of steps
    or pages is cleaned of floating-point noise (50.0 in steps of 0.01 gives 50.08999999999998),
    in the widget and the state; a typed value and a dragged one are exactly what they are.
    """

    def on_changed(_widget: object) -> None:
        if ctx.updating:
            return
        value = adjustment.get_value()
        if memory.programmatic is not None and value == memory.programmatic:
            return
        memory.programmatic = None
        held = number_of(ctx.runtime.store.value(key))
        snapped = value
        if not memory.typed and held is not None:
            snapped = numeric.snap_step(value, held)
        if snapped != value:
            with ctx.programmatic():
                adjustment.set_value(snapped)
        ctx.user_input(key, numeric.to_state(snapped), change)

    return on_changed


def _slider(component: Slider, ctx: BuildContext) -> Gtk.Widget:
    key, numeric, adjustment = _numeric(component, ctx)
    scale = Gtk.Scale.new(Gtk.Orientation.HORIZONTAL, adjustment)
    scale.set_digits(numeric.digits)
    scale.set_draw_value(True)
    scale.set_value_pos(Gtk.PositionType.RIGHT)
    scale.set_hexpand(True)
    change = _event(component.on_change, component.kind, "change", key)

    memory = _Memory()

    def update(value: StateValue, stale: bool) -> None:
        number = number_of(value)
        if number is not None:
            memory.programmatic = float(number)
            if adjustment.get_value() != float(number):
                adjustment.set_value(float(number))

    ctx.connect(
        scale, "value-changed", _number_changed(adjustment, numeric, ctx, key, change, memory)
    )
    ctx.bind(key, scale, update)
    return scale


def _spin(component: Spin, ctx: BuildContext) -> Gtk.Widget:
    key, numeric, adjustment = _numeric(component, ctx)
    spin = Gtk.SpinButton.new(adjustment, 1.0, numeric.digits)  # climb rate: GTK's usual
    # Not numeric-only: GTK then refuses to take the text of a value with more decimals than
    # `digits`, or with an exponent (1e-300), which is how a float is shown whole. What is typed
    # is checked by `on_input` instead.
    spin.set_numeric(False)
    ctx.on_close(quiet_spin_input_marshalling())
    memory = _Memory()
    harden_text(spin, ctx.connect)
    change = _event(component.on_change, component.kind, "change", key)

    def show_whole() -> None:
        # The text shows the value whole (every digit that reads back as the same number). GTK's
        # own output rounds to `digits` places, and when the user leaves the field or presses
        # Enter it parses the text back: a value that is not on that grid (a default, a patch, a
        # clamped result) would be rewritten rounded, and reported as an edit by the user.
        value = adjustment.get_value()
        spin.set_text(str(round(value)) if numeric.integer else repr(value))

    def output(_spin: object) -> bool:
        show_whole()
        return True

    def on_input(_spin: object, _new_value: object) -> int:
        # What the user typed, when they leave the field or press Enter. Strictly a number, finite,
        # within the range (a whole number for an int): a valid one is put into the adjustment
        # here (GTK itself ignores a change of 1e-10 or less, which is how a tiny value would be
        # lost); anything else is dropped. Either way the text is then rewritten from the
        # adjustment, so that GTK's own parse, which follows, reads the value the widget holds
        # and sees no change. (Returning GTK's input error instead makes GTK read a value that was
        # never set, and write 0.)
        try:
            parsed = numeric.parse(spin.get_text())
            if parsed is not None:
                memory.typed = True
                try:
                    adjustment.set_value(float(parsed))
                finally:
                    memory.typed = False
        finally:
            # Even if the above raised: GTK's own parse follows, and would read the raw text
            # ("abc" as 0, "0x10" as 10) as an edit by the user.
            show_whole()
        return 0  # not handled: the default parse comes next

    def update(value: StateValue, stale: bool) -> None:
        number = number_of(value)
        # Through the adjustment: the spin button's own `set_value` ignores a change under 1e-10.
        if number is not None:
            memory.programmatic = float(number)
            if adjustment.get_value() != float(number):
                adjustment.set_value(float(number))

    ctx.connect(spin, "output", output)
    ctx.connect(spin, "input", on_input)
    ctx.connect(
        spin, "value-changed", _number_changed(adjustment, numeric, ctx, key, change, memory)
    )
    ctx.bind(key, spin, update)
    show_whole()  # the first text, whatever GTK would have written (a default off the grid)
    return spin


def _dropdown(component: Dropdown, ctx: BuildContext) -> Gtk.Widget:
    key, decl = _bound(component, ctx, EnumState)
    change = _event(component.on_change, component.kind, "change", key)

    def choose(option: str) -> None:
        ctx.user_input(key, option, change)

    choices = ChoiceList(decl.options, choose, ctx.budget, ctx.connect)
    ctx.on_close(choices.release)

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
        case Image():
            return _image(component, ctx)
        case TableView():
            return _table(component, ctx)
        case TextView():
            return _text_view(component, ctx)
        case Tabs():
            return _tabs(component, path, ctx)
        case Expander():
            return _expander(component, path, ctx)
        case _ as unreachable:
            assert_never(unreachable)
