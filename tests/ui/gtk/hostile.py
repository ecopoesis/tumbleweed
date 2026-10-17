"""A hostile spec: every component the renderer supports, with the worst text and numbers allowed.

Every limit it can reach is at its limit: 64 options of 128 characters, 200-character lines,
1,000-character blocks, 64 children, ranges and steps at their extremes, markup and mnemonics
and format characters in every text. It validates (it is a legal spec); it is a model's worst
day, not a malformed one.
"""

from typing import TYPE_CHECKING

from tests.ui.runtime.support import choice, flag, number, real, text
from tests.ui.spec.support import Json, binary, computed, lit_int, lit_str, ref, variadic
from tumbleweed.ui.spec import UiSpec, validate_ui_spec
from tumbleweed.ui.spec.limits import MAX_SAFE_INT

if TYPE_CHECKING:
    from tumbleweed.core.types import JsonValue

MARKUP = "<b>bold</b> <a href='file:///etc/passwd'>x</a> &amp; _under_score <span foreground='red'>"
LONG_LINE = "W" * 200
LONG_BLOCK = ("각" * 300 + " ") + "Q" * 99  # one huge cluster, then a run
NO_SPACES = "x" * 1000
OPTIONS = [f"option {i:02d} " + "m" * 118 for i in range(64)]


def hostile_json() -> Json:
    state: list[Json] = [
        text("s", MARKUP, max_length=65536),
        text("long", "각" * 20000, max_length=65536),
        text("run", "y" * 10000, max_length=65536),
        choice("e", OPTIONS, OPTIONS[0]),
        flag("b", default=True),
        number("wide", 0, minimum=-(MAX_SAFE_INT // 2), maximum=MAX_SAFE_INT // 2, step=1),
        number("big", 7, minimum=0, maximum=MAX_SAFE_INT),
        real("tiny", 1e-300, minimum=0.0, maximum=1e-290, step=2.2250738585072014e-308),
        real("fine", 0.5, minimum=-1.0, maximum=1.0, step=0.000001),
        real("narrow", 0.0, minimum=0.0, maximum=5e-324),
        real("whole", 2.0, minimum=0.0, maximum=4.0, step=4.0),
        {"kind": "image", "key": "img"},
        {"kind": "table", "key": "tab"},
        computed("shown", "string", variadic("concat", ref("s"), lit_str(MARKUP)), ""),
        {
            **computed("bar", "float", binary("mul", ref("fine"), lit_int(2)), 0.0),
            "minimum": -1.0,
            "maximum": 1.0,
        },
    ]
    state[13] = {**state[13], "max_length": 65536}
    children: list[JsonValue] = [
        {"kind": "heading", "level": 1, "text": MARKUP},
        {"kind": "heading", "level": 3, "bind": "long"},
        {"kind": "label", "text": LONG_BLOCK},
        {"kind": "label", "text": NO_SPACES},
        {"kind": "label", "bind": "s"},
        {"kind": "label", "bind": "long"},
        {"kind": "label", "bind": "run"},
        {"kind": "label", "bind": "shown"},
        {"kind": "label", "bind": "e"},
        {"kind": "entry", "bind": "s", "placeholder": LONG_LINE},
        {"kind": "entry", "bind": "long", "placeholder": MARKUP},
        {"kind": "entry", "bind": "run"},
        {"kind": "switch", "bind": "b"},
        {"kind": "check", "label": LONG_LINE, "bind": "b"},
        {"kind": "check", "label": MARKUP, "bind": "b"},
        {"kind": "button", "label": LONG_LINE},
        {"kind": "button", "label": MARKUP},
        {"kind": "slider", "bind": "wide"},
        {"kind": "slider", "bind": "big"},
        {"kind": "slider", "bind": "tiny"},
        {"kind": "slider", "bind": "fine"},
        {"kind": "slider", "bind": "whole"},
        {"kind": "slider", "bind": "narrow"},  # no step to default to: a placeholder
        {"kind": "spin", "bind": "wide"},
        {"kind": "spin", "bind": "big"},
        {"kind": "spin", "bind": "tiny"},
        {"kind": "spin", "bind": "fine"},
        {"kind": "spin", "bind": "whole"},
        {"kind": "progress", "bind": "wide"},
        {"kind": "progress", "bind": "big"},
        {"kind": "progress", "bind": "tiny"},
        {"kind": "progress", "bind": "narrow"},
        {"kind": "progress", "bind": "bar"},
        {"kind": "dropdown", "bind": "e"},
        {"kind": "separator", "orientation": "vertical"},
        {"kind": "image", "bind": "img", "alt": MARKUP},
        {"kind": "table", "bind": "tab"},
        {"kind": "text_view", "bind": "s", "editable": True},
        {
            "kind": "tabs",
            "pages": [{"title": MARKUP[:80], "content": {"kind": "label", "bind": "s"}}],
        },
        {"kind": "expander", "title": MARKUP[:80], "child": {"kind": "label", "bind": "s"}},
        {
            "kind": "grid",
            "columns": 12,
            "children": [{"kind": "label", "text": LONG_LINE}] * 24,
        },
        {
            "kind": "box",
            "orientation": "horizontal",
            "spacing": 64,
            "children": [{"kind": "button", "label": LONG_LINE}] * 8,
        },
    ]
    return {
        "version": "tumbleweed.ui/1",
        "title": MARKUP[:80],
        "state": state,  # type: ignore[dict-item]  # JSON by construction
        "root": {"kind": "box", "spacing": 64, "children": children},
    }


def hostile_spec() -> UiSpec:
    return validate_ui_spec(hostile_json())
