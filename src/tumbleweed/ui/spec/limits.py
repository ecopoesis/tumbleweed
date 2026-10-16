"""The hard limits of `tumbleweed.ui/1`, as named constants (ARCHITECTURE I4 and I9).

A spec is untrusted data from the model, and a fixed renderer trusts only what passes these
limits. Each constant is enforced where the value enters the models, and `tests/ui/spec/
test_limits.py` checks every `MAX_*` name here at exactly its limit and one past it. Counting
rules, where they are not obvious from the name:

- Lengths are in Unicode code points (`len(text)`), not bytes.
- Expression depth: a literal or a reference has depth 1, an operator is one deeper than its
  deepest operand.
- Component depth: the root has depth 1, a child is one deeper than its container.
- Nodes are counted over the tree as written, not over shared Python objects.
"""

from typing import Final

# --- the document ---------------------------------------------------------------------------
MAX_SPEC_BYTES: Final = 256 * 1024  # raw JSON text accepted by `parse_ui_spec`
MAX_TITLE_LENGTH: Final = 80
MAX_NAME_LENGTH: Final = 64  # state keys, handler, module and function names
MAX_LINE_LENGTH: Final = 200  # one-line text: button, tab and expander labels, placeholders
MAX_TEXT_LENGTH: Final = 1_000  # block text: label text, string literals, agent notes
MAX_STATE_ENTRIES: Final = 128
MAX_HANDLERS: Final = 64
MAX_HANDLER_ITEMS: Final = 16  # assignments of a `set` handler, inputs or outputs of `sandbox`
MAX_DEBOUNCE_MS: Final = 10_000

# --- expressions ----------------------------------------------------------------------------
MAX_EXPR_DEPTH: Final = 16
MAX_EXPR_NODES: Final = 256  # in one expression
MAX_EXPR_ARGS: Final = 8  # operands of min, max, and, or, concat
MAX_TOTAL_EXPR_NODES: Final = 2_048  # in all the expressions of one spec
MAX_DECIMALS: Final = 10  # of `format_number`

# --- components -----------------------------------------------------------------------------
MAX_COMPONENT_DEPTH: Final = 16
MAX_COMPONENTS: Final = 512  # in one spec
MAX_CONTAINER_CHILDREN: Final = 64  # children of a box or grid, pages of tabs
MAX_GRID_COLUMNS: Final = 12
MAX_SPACING: Final = 64  # pixels

# --- state ----------------------------------------------------------------------------------
MAX_OPTION_LENGTH: Final = 128  # of one enum option
MAX_ENUM_OPTIONS: Final = 64
MAX_STRING_LENGTH: Final = 65_536  # `max_length` a string state may declare
MAX_CELL_LENGTH: Final = 1_000  # a string inside a list or a table, a column name
MAX_LIST_ITEMS: Final = 1_000  # `max_items` a list state may declare
MAX_TABLE_COLUMNS: Final = 32
MAX_TABLE_ROWS: Final = 10_000  # `max_rows` a table state may declare
MAX_TABLE_CELLS: Final = 20_000  # rows times columns, of one table value
MAX_JSON_DEPTH: Final = 16  # of a `json` state value
MAX_JSON_NODES: Final = 10_000  # of a `json` state value (every container, scalar and key)
# Bytes of text, not of the encoded value: the UTF-8 bytes of every string and key in it. The
# number of values is bounded by the limits above, so this bounds the size of a value. A
# `string` value is bounded by `MAX_STRING_LENGTH` characters (at most four bytes each).
MAX_VALUE_TEXT_BYTES: Final = 1_048_576  # in one `json`, `table` or `list` value
MAX_PATCH_TEXT_BYTES: Final = 2_097_152  # in all the values of one patch (`validate_patch`)
MAX_STATE_TEXT_BYTES: Final = 4_194_304  # in all the values of the state of one tool instance
# Spec numbers are IEEE-754 doubles, as in I-JSON and RFC 8785 (JCS), the canonical JSON the tool
# store hashes (S23). A whole number is exact up to 2**53 - 1, so that is the magnitude of every
# value of an `int` entry, `int` literal, table `int` cell and list `int` item. A number in a
# `float` or `json` position may be written as an integer of any size that is exactly a double.
MAX_SAFE_INT: Final = 2**53 - 1
# A slider, spin button or progress bar needs a range that is finite and a step that divides it
# into a sane number of positions: the bounds are within +-MAX_SAFE_INT, `minimum` is below
# `maximum`, the range is at most MAX_SAFE_INT wide, and a `float` entry's `step` is at least
# the smallest normal float and at most MAX_WIDGET_STEPS steps fit in the range.
MAX_WIDGET_STEPS: Final = 10_000_000

# Defaults for the optional bounds of a state declaration.
DEFAULT_STRING_MAX_LENGTH: Final = 1_000
DEFAULT_LIST_MAX_ITEMS: Final = 100
DEFAULT_TABLE_MAX_ROWS: Final = 1_000


def all_limits() -> dict[str, int]:
    """Every `MAX_*` limit by name, read from this module (so a new one cannot be missed)."""
    return {name: value for name, value in globals().items() if name.startswith("MAX_")}
