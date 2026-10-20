# UI spec reference (tumbleweed.ui/1)

show_ui takes a spec in this format and validates it before anything is shown. This reference is generated from the format's schema and its documentation.

## Shapes

Every object is closed: a field not listed is an error. `name` is a lowercase snake_case name of at most 64 characters. Ranges are (min..max).

spec: version: "tumbleweed.ui/1", title: string(1..80), state?: [StateDecl](..128), root: Component, handlers?: [Handler](..64)

Components, by `kind`:
- box: orientation?: "vertical"|"horizontal", spacing?: int(0..64), children?: [Component](..64)
- button: label: string(..200), on_click?: name|null
- check: label: string(..200), bind: name, on_change?: name|null
- dropdown: bind: name, on_change?: name|null
- entry: bind: name, placeholder?: string(..200), on_change?: name|null, on_activate?: name|null
- expander: title: string(..200), expanded?: bool, child: Component
- grid: columns: int(1..12), row_spacing?: int(0..64), column_spacing?: int(0..64), children?: [Component](..64)
- heading: level?: int(1..3), text?: string(..1000)|null, bind?: name|null
- image: bind: name, alt?: string(..200)
- label: text?: string(..1000)|null, bind?: name|null
- progress: bind: name
- separator: orientation?: "vertical"|"horizontal"
- slider: bind: name, on_change?: name|null
- spin: bind: name, on_change?: name|null
- switch: bind: name, on_change?: name|null
- table: bind: name
- tabs: pages: [TabPage](1..64)
- text_view: bind: name, editable?: bool, monospace?: bool, on_change?: name|null

State entries, by `kind`:
- bool: key: name, default: bool, computed?: Expr|null
- enum: key: name, options: [string(1..128)](1..64), default: string(1..128)
- file: key: name, default?: FileRef|null
- float: key: name, default: number, minimum?: number|null, maximum?: number|null, step?: number(>0..)|null, computed?: Expr|null
- image: key: name, default?: ImageRef|null
- int: key: name, default: int(-9007199254740991..9007199254740991), minimum?: int(-9007199254740991..9007199254740991)|null, maximum?: int(-9007199254740991..9007199254740991)|null, step?: int(1..9007199254740991), computed?: Expr|null
- json: key: name, default?: JsonValue
- list: key: name, item_type: "bool"|"int"|"float"|"string", max_items?: int(1..1000), default?: [bool|number|string(..1000)](..1000)
- string: key: name, default: string(..65536), max_length?: int(1..65536), computed?: Expr|null
- table: key: name, max_rows?: int(1..10000), default?: Table

Handlers, by `kind`:
- agent: name: name, note?: string(..1000)|null
- sandbox: name: name, module: name, function: name, inputs?: [name](..16), outputs: [name](1..16), debounce_ms?: int(0..10000)
- set: name: name, assignments: [SetAssignment](1..16)

Expressions, by `kind`:
- abs: operand: Expr
- add: left: Expr, right: Expr
- and: args: [Expr](2..8)
- bool: value: bool
- concat: args: [Expr](2..8)
- div: left: Expr, right: Expr
- eq: left: Expr, right: Expr
- float: value: number
- format_number: operand: Expr, decimals?: int(0..10), grouping?: bool
- ge: left: Expr, right: Expr
- gt: left: Expr, right: Expr
- if: condition: Expr, then: Expr, otherwise: Expr
- int: value: int(-9007199254740991..9007199254740991)
- le: left: Expr, right: Expr
- lt: left: Expr, right: Expr
- max: args: [Expr](2..8)
- min: args: [Expr](2..8)
- mul: left: Expr, right: Expr
- ne: left: Expr, right: Expr
- not: operand: Expr
- or: args: [Expr](2..8)
- ref: key: name
- round: operand: Expr
- string: value: string(..1000)
- sub: left: Expr, right: Expr

Other shapes:
- FileRef: path: string(1..4096)
- ImageRef: path: string(1..4096)
- SetAssignment: target: name, value: Expr
- TabPage: title: string(..200), content: Component
- Table: columns?: [TableColumn](..32), rows?: [[bool|number|string(..1000)](..32)](..10000)
- TableColumn: name: string(1..1000), type: "string"|"int"|"float"|"bool"

## 2. An example

A tip calculator: three inputs, everything else computed, no handlers. It works with no round trip
to the agent.

```json
{
  "version": "tumbleweed.ui/1",
  "title": "Tip calculator",
  "state": [
    {"kind": "float", "key": "bill", "default": 50, "minimum": 0, "maximum": 10000, "step": 0.01},
    {"kind": "int", "key": "tip_percent", "default": 18, "minimum": 0, "maximum": 50},
    {"kind": "int", "key": "people", "default": 2, "minimum": 1, "maximum": 20},
    {
      "kind": "float",
      "key": "tip_amount",
      "default": 0,
      "computed": {
        "kind": "div",
        "left": {
          "kind": "round",
          "operand": {
            "kind": "mul",
            "left": {
              "kind": "div",
              "left": {
                "kind": "mul",
                "left": {"kind": "ref", "key": "bill"},
                "right": {"kind": "ref", "key": "tip_percent"}
              },
              "right": {"kind": "int", "value": 100}
            },
            "right": {"kind": "int", "value": 100}
          }
        },
        "right": {"kind": "int", "value": 100}
      }
    },
    {
      "kind": "float",
      "key": "total",
      "default": 0,
      "computed": {
        "kind": "add",
        "left": {"kind": "ref", "key": "bill"},
        "right": {"kind": "ref", "key": "tip_amount"}
      }
    },
    {
      "kind": "float",
      "key": "per_person",
      "default": 0,
      "computed": {
        "kind": "div",
        "left": {"kind": "ref", "key": "total"},
        "right": {"kind": "ref", "key": "people"}
      }
    },
    {
      "kind": "bool",
      "key": "shared",
      "default": false,
      "computed": {
        "kind": "gt",
        "left": {"kind": "ref", "key": "people"},
        "right": {"kind": "int", "value": 1}
      }
    },
    {
      "kind": "string",
      "key": "tip_text",
      "default": "",
      "computed": {
        "kind": "concat",
        "args": [
          {"kind": "string", "value": "Tip ("},
          {"kind": "format_number", "operand": {"kind": "ref", "key": "tip_percent"}},
          {"kind": "string", "value": "%): $"},
          {
            "kind": "format_number",
            "operand": {"kind": "ref", "key": "tip_amount"},
            "decimals": 2,
            "grouping": true
          }
        ]
      }
    },
    {
      "kind": "string",
      "key": "total_text",
      "default": "",
      "computed": {
        "kind": "concat",
        "args": [
          {"kind": "string", "value": "Total: $"},
          {
            "kind": "format_number",
            "operand": {"kind": "ref", "key": "total"},
            "decimals": 2,
            "grouping": true
          }
        ]
      }
    },
    {
      "kind": "string",
      "key": "per_person_text",
      "default": "",
      "computed": {
        "kind": "if",
        "condition": {"kind": "ref", "key": "shared"},
        "then": {
          "kind": "concat",
          "args": [
            {"kind": "string", "value": "Each of "},
            {"kind": "format_number", "operand": {"kind": "ref", "key": "people"}},
            {"kind": "string", "value": " pays $"},
            {
              "kind": "format_number",
              "operand": {"kind": "ref", "key": "per_person"},
              "decimals": 2,
              "grouping": true
            }
          ]
        },
        "otherwise": {"kind": "string", "value": ""}
      }
    },
    {
      "kind": "string",
      "key": "verdict",
      "default": "",
      "computed": {
        "kind": "if",
        "condition": {
          "kind": "ge",
          "left": {"kind": "ref", "key": "tip_percent"},
          "right": {"kind": "int", "value": 20}
        },
        "then": {"kind": "string", "value": "A generous tip"},
        "otherwise": {
          "kind": "if",
          "condition": {
            "kind": "lt",
            "left": {"kind": "ref", "key": "tip_percent"},
            "right": {"kind": "int", "value": 10}
          },
          "then": {"kind": "string", "value": "A modest tip"},
          "otherwise": {"kind": "string", "value": "A customary tip"}
        }
      }
    }
  ],
  "root": {
    "kind": "box",
    "spacing": 12,
    "children": [
      {"kind": "heading", "text": "Tip calculator"},
      {
        "kind": "grid",
        "columns": 2,
        "children": [
          {"kind": "label", "text": "Bill ($)"},
          {"kind": "spin", "bind": "bill"},
          {"kind": "label", "text": "Tip (%)"},
          {"kind": "slider", "bind": "tip_percent"},
          {"kind": "label", "text": "People"},
          {"kind": "spin", "bind": "people"}
        ]
      },
      {"kind": "separator"},
      {"kind": "label", "bind": "tip_text"},
      {"kind": "label", "bind": "total_text"},
      {"kind": "label", "bind": "per_person_text"},
      {"kind": "label", "bind": "verdict"}
    ]
  }
}
```

## 3. The envelope

| Field | Type | |
|---|---|---|
| `version` | the string `"tumbleweed.ui/1"` | required; any other version is refused |
| `title` | one line, 1 to 80 characters | required; shown on the canvas only, never in trusted chrome (I5) |
| `state` | list of state declarations | optional, at most 128 |
| `root` | one component | required |
| `handlers` | list of handlers | optional, at most 64 |

State keys are unique and handler names are unique.

## 4. Names, text and numbers

**Names.** State keys, handler names, module names and function names are lowercase snake_case
ASCII: a letter, then letters, digits and `_`, at most 64 characters. They are four distinct
types in the code (`StateKey`, `HandlerName`, `ModuleName`, `FunctionName`). A module or function
name cannot start with `_`, so no dunder is called, and cannot contain `.` or `/`.

**Authored text** is what the model writes into the spec: the title, labels, placeholders,
option names, string literals, notes. It is part of what a saved tool hashes. It may not contain
control characters (a *block*, such as a label's text or a string literal, may contain newline
and tab; a *line*, such as a button label, may not), Unicode format characters (bidirectional
overrides, zero-width space, byte-order mark; the zero-width joiner and non-joiner, which real
scripts and emoji need, are allowed), line or paragraph separators, or a lone surrogate. Text
is plain: the renderer uses plain-text widget APIs, never markup (I4).

**Data text** is what users and sandboxes produce: the value of a `string` entry, a table cell,
a list item, a string inside a `json` value. It may contain anything except NUL and a lone
surrogate, because a CSV can contain anything and GTK cannot hold a NUL.

**Numbers** are IEEE-754 doubles, as in I-JSON and RFC 8785 (JCS), the canonical JSON that the
tool store hashes. One rule covers every place a number can be, so that a spec is still valid
after canonicalisation, which writes `1e16` as `10000000000000000`, `5.0` as `5` and `2**60` as
`1152921504606847000`:

- in an `int` position (an `int` entry, `minimum`, `maximum`, `step` and `default`; an `int`
  literal; an `int` cell of a table; an item of a list of ints) a number is a whole number
  within +-(2**53 - 1), and is never written with a fraction (`5.0` is not an `int`);
- in a `float` position (a `float` entry, bound and step; a `float` literal; a `float` column;
  an item of a list of floats) a number is a finite double. It may be written as an integer of
  any size that stands for a double (see below), and is then stored as a float, so `5`, `5.0`
  and `5e0` are one value;
- in a `json` value a number is a finite double, an integer being accepted by the same test (and
  stored as a float if it is beyond 2**53 - 1, so that it reads back as the same value). Inside a
  `json` value whether a number was written as an integer or with a fraction does not survive
  canonicalisation (`5.0` is written `5`): do not give meaning to the difference. A value whose type
  matters belongs in an `int` or `float` entry, a table column or a list.

An integer **stands for a double** if it is exactly that double (`5`, `10000000000000000`,
2**100) or if it is how canonical JSON writes the nearest one: JCS writes a double from 2**53 up
to 1e21 as its shortest digits padded with zeros, so 2**60 becomes `1152921504606847000` and
2**68 becomes `295147905179352830000` (the number in RFC 8785 appendix B), which are not
exactly those doubles but are what canonicalisation makes of them, and so must be accepted when a
saved spec is read back. No type is inferred from how a number is written. An integer that is
neither, such as 2**53 + 1, 2**60 + 1 or an integer of 400 digits, is refused in a float or JSON position, not
rounded in silence. The formatter and the canonicaliser are `tumbleweed.core.jcs`.

## 5. State

A state declaration has a `kind`, a `key` and a `default`, plus the fields of its kind.

| `kind` | Fields (`?` is optional) |
|---|---|
| `bool` | `key`, `default`, `computed?` |
| `int` | `key`, `default`, `minimum?`, `maximum?`, `step?`, `computed?` |
| `float` | `key`, `default`, `minimum?`, `maximum?`, `step?`, `computed?` |
| `string` | `key`, `default`, `max_length?`, `computed?` |
| `enum` | `key`, `options`, `default` |
| `image` | `key`, `default?` |
| `file` | `key`, `default?` |
| `table` | `key`, `max_rows?`, `default?` |
| `list` | `key`, `item_type`, `max_items?`, `default?` |
| `json` | `key`, `default?` |

| Kind | Value | Bounds |
|---|---|---|
| `bool` | `true` or `false` | |
| `int` | integer within +-(2**53 - 1) | `minimum`, `maximum` (inclusive), `step` (a hint for widgets, at least 1) |
| `float` | finite double | `minimum`, `maximum` (inclusive), `step` (a hint, above 0) |
| `string` | data text | `max_length`, 1 to 65,536 (default 1,000) |
| `enum` | one of the `options` | 1 to 64 distinct options, each a non-empty line of at most 128 characters; `default` is one of them |
| `image` | an `ImageRef` or null | |
| `file` | a `FileRef` or null | |
| `table` | a `Table` | `max_rows`, 1 to 10,000 (default 1,000) |
| `list` | list of `bool`, `int`, `float` or `string` (`item_type`) | `max_items`, 1 to 1,000 (default 100) |
| `json` | any JSON value within the limits | |

A default that breaks its own bounds is an error. Options that differ only in Unicode
composition (NFC, as the tool store normalises) count as the same option.

**Size.** A `string` is at most `max_length` characters. A `json`, `table` or `list` value holds at
most 1,048,576 bytes of text (the UTF-8 bytes of every string, key and column name in it;
numbers and flags are bounded by the count limits). A patch holds at most 2,097,152 bytes of
text, and the state of a tool at most 4,194,304. `UiSpec` checks the defaults against the last,
`UiSpec.validate_patch` a patch against the second and, given `current`, the values the state
holds now, the state after the patch against the third, but only if the patch makes the text of
the state grow: a state that is over its budget can still shrink and still change what holds no
text, or a tool would be wedged. `current` is required and is the whole state, computed entries
included (their values are text too, as the defaults of computed entries are in the check of the
defaults); pass `{}` for an empty state. `value_text_bytes` measures a value; the runtime keeps
the sum within the budget as values change (evaluation). The checks stop at the first limit crossed, and
run in C for text, so a hostile value costs little.

**Computed state.** An entry of kind `bool`, `int`, `float` or `string` may have a `computed`
expression over other entries. It is read-only: no widget, `set` handler, sandbox or
`patch_state` can write it. It still carries a `default`, which must be valid. The dependency
graph must be acyclic, and an entry that reads itself is a cycle; `UiSpec.computed_order()` gives
the order to evaluate in. An expression can read entries of kind `bool`, `int`, `float`, `string`
and `enum` (which reads as a string), and no others.

An `enum` entry cannot be computed: its value must be one of its options, which an
expression cannot guarantee before it runs. To derive a choice, compute a `string`.

**Checking a value.** `check_value(declaration, value)` decides whether a value belongs to an
entry. It is how defaults are checked, and `UiSpec.validate_patch(patch, current=..., only=...)`
uses it for everything that arrives later, from `patch_state` or from a sandbox: every key must be
a declared, non-computed entry (and in `only`, a sandbox handler's declared outputs), every value
must fit, the size budgets above must hold, and a patch with any problem is refused whole. Every
error names the key (`size`) or the item (`ratings[2]`) it is about.

## 7. Expressions

An expression is a JSON object with a `kind`. Operands are expressions, never strings.

| `kind` | Fields (`?` is optional) |
|---|---|
| `bool` | `value` |
| `int` | `value` |
| `float` | `value` |
| `string` | `value` |
| `ref` | `key` |
| `add` | `left`, `right` |
| `sub` | `left`, `right` |
| `mul` | `left`, `right` |
| `div` | `left`, `right` |
| `min` | `args` |
| `max` | `args` |
| `abs` | `operand` |
| `round` | `operand` |
| `eq` | `left`, `right` |
| `ne` | `left`, `right` |
| `lt` | `left`, `right` |
| `le` | `left`, `right` |
| `gt` | `left`, `right` |
| `ge` | `left`, `right` |
| `and` | `args` |
| `or` | `args` |
| `not` | `operand` |
| `if` | `condition`, `then`, `otherwise` |
| `concat` | `args` |
| `format_number` | `operand`, `decimals?`, `grouping?` |

`bool`, `int`, `float` and `string` are literals (`{"kind": "int", "value": 100}`); `ref` reads
a state entry (`{"kind": "ref", "key": "bill"}`). `min`, `max`, `and`, `or` and `concat` take
2 to 8 operands in `args`. `if` takes `condition`, `then` and `otherwise` (`else` is a reserved
word in some languages, so it is not used). `format_number` takes a number and the bounded
parameters `decimals` (0 to 10, default 0) and `grouping` (default `false`); there is no format
string and no locale.

For example, the tip, rounded to cents, from the calculator above (`bill * tip_percent / 100`,
to two decimals):

```json
{
  "kind": "div",
  "left": {
    "kind": "round",
    "operand": {
      "kind": "mul",
      "left": {
        "kind": "div",
        "left": {
          "kind": "mul",
          "left": {
            "kind": "ref",
            "key": "bill"
          },
          "right": {
            "kind": "ref",
            "key": "tip_percent"
          }
        },
        "right": {
          "kind": "int",
          "value": 100
        }
      },
      "right": {
        "kind": "int",
        "value": 100
      }
    }
  },
  "right": {
    "kind": "int",
    "value": 100
  }
}
```

### Types

There are four types: `bool`, `int`, `float` and `string`. The type checker (`check_expr`, written by
hand over the closed set of nodes) gives each expression one type or names the node that
fails, as `add.left` or `concat[1]`:

- A literal has its own type. A `ref` has the type of its entry; an `enum` entry is a `string`.
  An entry of kind `image`, `file`, `table`, `list` or `json` cannot be read.
- `add`, `sub` and `mul` take two numbers (an `int` or a `float`) and are an `int` if both are,
  else a `float`. `div` takes two numbers and is always a `float`.
- `min` and `max` take numbers and are an `int` if all are, else a `float`. `abs` keeps the type
  of its number. `round` takes a number and is an `int`: it is how a float becomes an int.
- `eq` and `ne` take two numbers, two strings or two bools. `lt`, `le`, `gt` and `ge` take two
  numbers. They are `bool`s. Comparing an `enum` entry with a string literal also checks that the
  literal is one of its options, and so does a literal that an `if` can produce; comparing two
  `enum` entries needs an option in common (otherwise it is always false, or always true for
  `ne`), and so does an enum entry that an `if` can produce on either side.
- `and`, `or` and `not` take bools and are a `bool`.
- `if` takes a `bool` and two branches of one type, or two numbers (then a `float` if either is
  one). Both branches are always checked.
- `concat` takes strings and is a `string`. A number is never converted to a string except by
  `format_number`.

Nothing converts between a number and a string, a bool and a number, or a float and an int. A
computed entry or an assignment must have a type that fits its target: `bool` to `bool`, `int`
to `int`, `int` or `float` to `float`, `string` to `string` or to an `enum`. An `enum` entry must be
able to take what is assigned: every literal the expression can produce, directly or through `if`,
must be one of its options, and every `enum` entry it can produce the same way must share an option
with the target (an assignment from an enum with no option in common never takes effect). What a
`concat` or a `ref` of a `string` gives is for the runtime to check, and a value that is no option
leaves the entry as it was.

### Limits of an expression

At most 16 levels, 256 nodes, and 2,048 nodes in all the expressions of one spec
(computed entries and `set` assignments together). The first two are checked by every node as it
is built, so an over-deep expression is refused at the first node that exceeds the limit,
wherever it was built.

### Evaluation

The syntax and the types above are what `tumbleweed.ui.spec` defines and checks. The meaning below is
what `tumbleweed.ui.runtime` implements (`interpreter`, `store`, `writes`), with a test for each
rule in `tests/ui/runtime/`, so that a spec means the same everywhere:

- The interpreter is total: no expression raises, whatever the state it reads. `div` by zero is
  `0.0`. It is also iterative, so a caller close to the recursion limit still gets a value.
- An `int` result is clamped to +-(2**53 - 1). A `float` result that is infinite becomes the
  largest finite float of that sign, and one that is not a number becomes `0.0`.
- `round` rounds halves away from zero (`0.49999999999999994` rounds to `0`). `min` and `max` of
  mixed numbers are `float`s, and so is an `if` whose branches are an `int` and a `float`: the value
  of the chosen branch is converted to a float, whichever branch it is (`if(c, 1, 2.5)` is `1.0`
  when `c` is true). Every operand is evaluated, as every branch is type-checked; nothing has a side
  effect, so this changes no result.
- `eq` and `ne` compare numbers by value (`1` equals `1.0`) and strings by code point, with no
  normalisation.
- `concat` stops at 65,536 characters, the longest a `string` entry can be, so that an
  expression that repeats a long string cannot build one of hundreds of megabytes. Nothing else
  that an entry can hold is cut short by it.
- `format_number` writes the number rounded half away from zero to `decimals` places, with `.`
  before the decimals, `-` for a negative number (never `-0`), and, with `grouping`, a `,`
  between groups of three digits of the whole part. It never depends on the locale. A float is
  rounded as the shortest decimal that reads back as it (what `repr`, and JSON, write), which is
  the number a person sees: `2.675` to two places is `2.68`, although the double nearest it is a
  hair below.
- When a value is written to an entry (by a computed entry, a `set` handler or a widget), a number is
  clamped into `minimum` and `maximum`, an `int` written to a `float` entry becomes a float, a
  string is cut to `max_length` characters, and an `enum` entry given a string that is not one of
  its options keeps its old value. Values that arrive from `patch_state` or a sandbox are not
  clamped: they are checked by `check_value` and refused whole.
- A write (by a widget, a `set` handler or a computed value) that would take the text of the
  state over `MAX_STATE_TEXT_BYTES` and makes that text grow is not applied: the entry keeps its
  old value. A write that shrinks the text, or leaves it the same size, is applied even when the
  state is over the budget, so that a state that got there can recover. The state is all its
  entries, computed ones included, as `validate_patch(current=...)` counts them. An edit of a
  widget that is not applied puts the widget back to the entry's value and fires no handler.
- A computed entry whose write is held back keeps a value that no longer matches its expression:
  it is **stale**, and so is every computed entry that reads a stale one, until a recomputation
  applies. The renderer shows "Out of date" in place of a stale value, never the old value as if it
  were current. Whenever a write makes the text of the state smaller, the stale entries are
  evaluated again, and again (up to twice more) while a pass frees more room, since an entry that
  comes earlier in the order may fit only after a later one has shrunk. They catch up when room
  appears.
- Computed entries are evaluated in `computed_order()` and re-evaluated, in that order,
  whenever an entry they read changes. A `set` handler's assignments are applied together, so the
  computed entries are re-evaluated once, after all of them.
- Listeners are told of every write, whether or not the value changed, so that a widget reloads a
  rewritten file (section 13); a computed entry is announced
  when its value or its stale mark changes.

## 8. Components

A component is a JSON object with a `kind`.

| `kind` | Fields (`?` is optional) |
|---|---|
| `box` | `orientation?`, `spacing?`, `children?` |
| `grid` | `columns`, `row_spacing?`, `column_spacing?`, `children?` |
| `label` | `text?`, `bind?` |
| `heading` | `level?`, `text?`, `bind?` |
| `button` | `label`, `on_click?` |
| `entry` | `bind`, `placeholder?`, `on_change?`, `on_activate?` |
| `switch` | `bind`, `on_change?` |
| `check` | `label`, `bind`, `on_change?` |
| `slider` | `bind`, `on_change?` |
| `spin` | `bind`, `on_change?` |
| `dropdown` | `bind`, `on_change?` |
| `progress` | `bind` |
| `separator` | `orientation?` |
| `image` | `bind`, `alt?` |
| `table` | `bind` |
| `text_view` | `bind`, `editable?`, `monospace?`, `on_change?` |
| `tabs` | `pages` |
| `expander` | `title`, `expanded?`, `child` |

`box` lays its children out in a row or a column; `grid` places them left to right, then top to
bottom, `columns` to a row; `tabs` shows one page at a time, each page a `title` and a `content`
component; `expander` shows or hides its `child`. A container has at most 64 children (or pages).
`label` and `heading` show either static `text` or the value of an entry (`bind`): exactly one of
the two. A heading's `level` is 1 to 3. `image` has `alt` text for accessibility. `text_view`
is read-only unless `editable`. All text a component shows is plain text.

**Text on the screen is bounded (I9).** A renderer lays text out on the thread that also serves the
trusted chrome, and laying out text takes time that grows faster than its length, so what is shown is
bounded, though the state holds the whole value. The reference renderer shows at most 8,192
characters of one `label`, `heading`, bound text or `entry`, followed by ` (cut)` for the labels,
and at most 65,536 characters in all in a tool
(`tumbleweed.ui.gtk.budget.MAX_SHOWN_PER_TOOL`, a renderer constant that happens to equal that
limit, not a spec limit): text blocks, button and check box labels, entry texts and placeholders, and
the options of a `dropdown` all count. A text that does not fit is replaced by a line saying so
(and shown when room appears, which is when another text shrinks), an option that does not fit is
left out of its list (which ends in a line saying that more are not shown, and gets them when room
appears), and a text shown is laid out at most once per frame, the latest value only. A `dropdown`
is a list in place, never a popup. An `entry` shows a text of up to 8,192 characters whole and
editable; a longer text, or one the budget has no room for, is shown cut (or as a line saying so),
read-only and with a warning icon: the state still holds it all, and an edit would otherwise write
the cut text over it.
The entry the user is in (the one that has the keyboard focus, or is being edited this moment)
keeps its text whatever the budget says, so that it does not turn into a mark under their fingers:
for as long as it has the focus the tool may show up to 8,192 characters more than the budget, in
that one entry; once the focus leaves (or, for an edit made without focus, at the next idle turn)
it claims like any other.

**The rich widgets (S11)** keep to the same bounds. What follows is the reference renderer's
behaviour (`tumbleweed.ui.gtk`; the numbers are renderer constants, not spec limits); DECISIONS D21
has the reasons.

- **`image`** shows the file an `ImageRef` names. The host passes a resolver that reads it
  (`RenderedTool(spec, images=...)`; with none every image says that images are not available): the
  ref is a file name and nothing else, opened without following links (see section 6), and what
  comes back is at most `8 MiB`. Those bytes are *looked at before they are decoded*: the magic
  number must be a PNG, a JPEG or a still WebP (an SVG is none, whatever the file is called; an
  animated WebP is refused, and so is a JPEG of more than `64` scans), and the size in the header
  must be at most `8,192` pixels a side and `16,777,216` pixels (width times height times 4 bytes is
  at most `64 MiB` decoded). Only then do the bytes go to a decoder, pinned to the format that was
  found, on a worker thread: the interface never waits for a file or a decode. What is refused,
  missing or broken is a line of fixed text in the image's place (`The image was not found.`,
  `The image is not allowed.`, `The image is too large.`,
  `Only PNG, JPEG and WebP images can be shown.`, `The image could not be read.`), never a word of
  the file or the error; a line `Loading the image...` stands there while it loads. A new ref (or
  the same one assigned again) replaces what is shown at once: the widget never shows an image the
  entry no longer holds. An image is scaled down to fit `480` pixels as it is decoded, never up (one
  that size or smaller keeps its pixels), so a tool's images together keep at most `64 MiB` of
  pixels; over that an image says it is too large. `alt` is for assistive technology only (it is not
  displayed).
- **`table`** is a `Gtk.ColumnView` of the columns and rows of a `table` entry, rebuilt when the
  entry changes. At most `1,000` rows are shown; a cell shows at most `256` characters (then the
  mark `(cut)`) on one line (line ends become spaces), a column name at most `64`; every text goes
  through the display transform and into a label with `set_text`, and all of it counts in the tool's
  text budget, so rows that do not fit are left out, the table ends in `(more rows not shown)`, and
  they are added when room appears. Columns are typed: `int` and `float` cells are right-aligned and
  a `bool` reads `yes` or `no`. There is no selection, the columns cannot be dragged, and there is
  no header menu.
- **`text_view`** is a `Gtk.TextView` that wraps at any character. It shows a `string` entry as it
  is (no display transform: nothing is inserted into the text, so what the user selects and copies
  is the source text), at most `8,192` characters then the mark `(cut)`, counted in the budget like
  an entry, and is two-way only while the whole text is shown (a cut text, or one the budget has no
  room for, is read-only with a warning style). What the user types or pastes is held to the entry's
  `max_length` and to `8,192` characters: an insert that would pass it is refused whole.
- **`tabs`** are a `Gtk.Notebook` (the tab menu is off, tabs cannot be reordered or detached), each
  title one plain, ellipsized line counted in the budget, and **`expander`** is a `Gtk.Expander`
  with a plain title. Their pages and child are built like any other child, so a page that cannot be
  built is a placeholder inside its tab.

What a widget shows or edits is an entry named by `bind`. The entry's declaration supplies range,
options and limits, so they are written once. Which kinds of entry a component accepts:

| Component | Binds | Two-way | Needs both bounds |
|---|---|---|---|
| `label` | `string`, `enum` | no | no |
| `heading` | `string`, `enum` | no | no |
| `progress` | `int`, `float` | no | yes |
| `image` | `image` | no | no |
| `table` | `table` | no | no |
| `entry` | `string` | yes | no |
| `switch` | `bool` | yes | no |
| `check` | `bool` | yes | no |
| `slider` | `int`, `float` | yes | yes |
| `spin` | `int`, `float` | yes | yes |
| `dropdown` | `enum` | yes | no |
| `text_view` | `string` | if `editable` | no |

`box`, `grid`, `tabs`, `expander`, `separator` and `button` bind nothing. A two-way binding can
never target a computed entry. "Needs both bounds" means the entry must declare both `minimum` and
`maximum`, and the range must be one a widget can work with: both bounds within +-(2**53 - 1),
`minimum` below `maximum` (a zero-width range has no positions, and a progress bar divides by it),
the range at most 2**53 - 1 wide, and, for a `slider` or `spin` bound to an entry that has a
`step`, a step that is at least the smallest normal float (about 2.2e-308) and no wider than the
range. These are rules of numeric safety, not of taste: how many positions the step makes is up
to the author (a range of a million in steps of 0.01 is a fine slider). A `progress` has no step,
so the step of its entry is not checked for it. A `float` entry without a `step` leaves the step
to the renderer, which uses a power of ten about a hundredth of the range, so a `slider` or `spin` bound to a float entry
without a `step` needs a range at least as wide as the smallest normal float (a narrower one leaves no step
that is not subnormal, and is refused).
A `spin` also needs a step above 1e-10, declared or chosen: GTK ignores a change of that size or
less, so a smaller step could never be taken with its arrows (a `slider` has no such limit). What a
user types into a `spin` is taken only if it is a finite number inside the range (a whole number
for an `int`); anything else (`abc`, `1,5`, `0x10`, `inf`, an empty field, a number outside the
range) is dropped and the text of the value it holds comes back, and neither the state nor a handler
is touched. A value that a step or page increment of a `slider` or `spin` produced (the arrows, the
keyboard, the scroll wheel) is the value held plus whole steps, added as decimals, when it lies
within a millionth of a step of that: so 50.0 in steps of 0.01 reads 50.09, not 50.08999999999998.
That is the widget's doing, a transform of stepped values and no part of evaluation: a typed
number (parsed exactly), a patched value and a default are never changed. A dragged slider position
is not special either way: it is snapped if, and only if, it lies within a millionth of a step of
1 to 20 whole steps (or pages) from the value held, and the snapped value is within the range;
otherwise (a move of more steps, however far, or one that is not on whole steps) it is kept exactly,
whatever its magnitude.

A component's events name a handler, or none:

| Component | Events |
|---|---|
| `button` | `on_click` |
| `entry` | `on_change`, `on_activate` |
| `switch` | `on_change` |
| `check` | `on_change` |
| `slider` | `on_change` |
| `spin` | `on_change` |
| `dropdown` | `on_change` |
| `text_view` | `on_change` |

A `text_view` that has `on_change` must be `editable`: a read-only view has no edits to report.

The tree is at most 16 levels deep and has at most 512 components, and a spacing is at most
64 pixels, a grid at most 12 columns wide.

## 9. Handlers

A handler has a `name` and a `kind`.

| `kind` | Fields (`?` is optional) |
|---|---|
| `set` | `name`, `assignments` |
| `sandbox` | `name`, `module`, `function`, `inputs?`, `outputs`, `debounce_ms?` |
| `agent` | `name`, `note?` |

- **`set`** assigns the results of expressions to entries, in the renderer. Every expression is
  evaluated against the state as it was when the event fired, and then the assignments are
  applied together, so no assignment sees another's result. A target may appear once. A target is
  a declared, non-computed entry of kind `bool`, `int`, `float`, `string` or `enum`, and the
  expression's type must fit it (section 7).
- **`sandbox`** calls `function` of the tool's code `module` in a sandbox (S20). The call gets
  the `inputs` as JSON and nothing else. Its result may write only the `outputs`: declared,
  non-computed entries, validated with `validate_patch(result, only=outputs)` before any is
  applied. A `set` handler holds at most 16 assignments and a `sandbox` handler at most
  16 inputs and 16 outputs, at least one output (the result is a patch: a call that may write nothing gives
  the user no feedback, and a handler that only has side effects can write a status entry). `debounce_ms` (0 to 10,000) waits that long after the last event before calling; 0
  calls at once in the spec, but S20's runtime applies a minimum debounce floor to a sandbox
  handler driven by `on_change` of a slider or a spin button (I9: slider-driven handlers are
  debounced), and coalesces events that arrive while a call is in flight. At most one call per tool instance runs at a time (I9). The spec holds no code and
  does not say whether the module exists: the tool package does (S23). There is no field for source,
  a command, a path, an environment, the network or a timeout; the broker's policy decides those.
  Until S20 connects a sandbox, the renderer builds a control whose handler is a `sandbox` one
  disabled, with a plain line under it that says it needs a sandbox that is not available yet
  (never a tooltip), and no event of it is passed on (S12).
- **`agent`** sends the event and a snapshot of the state to the agent as untrusted input (S12).
  `note` is the author's hint about what the event means, and is data too. The agent gets the
  event in a frame that says it is data, with the whole state as it was at the event, capped: a
  string longer than 1 KiB (as the model receives it, where `<`, `>` and `&` take six bytes each) is cut (and says how much was not shown), a list, table, JSON,
  image or file value over that is replaced by a marker that names its kind and size, and the whole
  snapshot is at most 6 KiB, the biggest values going first, then the last entries counted and left
  out (`tumbleweed.agent.canvas_events`). A tool instance starts at most one turn every 3 seconds,
  and one turn runs at a time: an event that arrives sooner waits, and a newer event of the same
  instance replaces it, so a handler on `on_change` of a slider or an entry costs a few turns, not
  one per tick. A handler author does not choose any of this: there is no field for it.
  A control whose handler is `agent` or `set` works the same on every host; the agent's answer
  comes back as a message and any `show_ui` or `patch_state` it makes, never as a handler.

Handlers fire only when a user acts on a widget. A change of state from anywhere else (a
computed entry updating, a `set` or sandbox result, `patch_state`, the model) never fires a
handler, so handlers cannot trigger one another. The runtime tracks where every change came from
(`tumbleweed.ui.runtime.Origin`) and the widgets tell an edit by the user from the runtime putting a
value into them, so that the second is never taken for the first. An `on_change` handler fires after
the edit has been written, and does not fire if the write was held back (evaluation)
or the edit left the entry's value as it was (a slider moved between two positions that round to the
same number).

## 10. What is checked across the spec

When a `UiSpec` is built, besides each part's own rules:

- every expression type-checks against the declarations, and fits the entry it computes or is
  assigned to;
- computed state has no cycle;
- every `bind` names a declared entry of a kind the component accepts, a two-way binding never
  targets a computed entry, and a slider, spin button or progress bar has an entry with both bounds;
- every `on_click`, `on_change` and `on_activate` names a declared handler;
- every `set` target and `sandbox` output is a declared, non-computed entry, and every `sandbox`
  input is a declared entry;
- every literal an expression can produce for an `enum` entry, through `if` as well, is one of its
  options, and two `enum` entries compared or assigned (through `if` as well) have an option in
  common;
- a slider, spin button or progress bar has a range and a step it can work with
  (section 8), and a `text_view` with `on_change` is `editable`;
- the component tree and the expressions are within their limits, and the text in the defaults of
  all the state is within its budget.

All problems are reported together (up to 10), each naming the component by its path and
kind (`root.children[2] (slider)`), or the entry or handler.

## 11. Limits

| Constant | Value | Bounds |
|---|---:|---|
| `MAX_SPEC_BYTES` | 262,144 | bytes of the raw JSON text (`parse_ui_spec`) |
| `MAX_TITLE_LENGTH` | 80 | characters in the title |
| `MAX_NAME_LENGTH` | 64 | characters in a state key, handler, module or function name |
| `MAX_LINE_LENGTH` | 200 | characters in one-line text: button, check, tab and expander labels, placeholders, alt text |
| `MAX_TEXT_LENGTH` | 1,000 | characters in block text: label and heading text, string literals, agent notes |
| `MAX_STATE_ENTRIES` | 128 | state declarations |
| `MAX_HANDLERS` | 64 | handlers |
| `MAX_HANDLER_ITEMS` | 16 | assignments of a `set` handler; inputs, and outputs, of a `sandbox` handler |
| `MAX_DEBOUNCE_MS` | 10,000 | `debounce_ms` of a `sandbox` handler |
| `MAX_EXPR_DEPTH` | 16 | levels of one expression (a literal or a reference is level 1) |
| `MAX_EXPR_NODES` | 256 | nodes in one expression |
| `MAX_EXPR_ARGS` | 8 | operands of `min`, `max`, `and`, `or` and `concat` |
| `MAX_TOTAL_EXPR_NODES` | 2,048 | nodes in all the expressions of one spec |
| `MAX_DECIMALS` | 10 | `decimals` of `format_number` |
| `MAX_COMPONENT_DEPTH` | 16 | levels of the component tree (the root is level 1) |
| `MAX_COMPONENTS` | 512 | components in one spec |
| `MAX_CONTAINER_CHILDREN` | 64 | children of a `box` or `grid`, pages of `tabs` |
| `MAX_GRID_COLUMNS` | 12 | `columns` of a `grid` |
| `MAX_SPACING` | 64 | pixels of spacing |
| `MAX_OPTION_LENGTH` | 128 | characters in one enum option |
| `MAX_ENUM_OPTIONS` | 64 | options of an `enum` |
| `MAX_STRING_LENGTH` | 65,536 | characters a `string` entry may declare as `max_length` |
| `MAX_CELL_LENGTH` | 1,000 | characters in a table cell, a column name or a list item |
| `MAX_LIST_ITEMS` | 1,000 | items a `list` entry may declare as `max_items` |
| `MAX_TABLE_COLUMNS` | 32 | columns of a table |
| `MAX_TABLE_ROWS` | 10,000 | rows a `table` entry may declare as `max_rows`, and rows of a table value |
| `MAX_TABLE_CELLS` | 20,000 | rows times columns of a table value |
| `MAX_JSON_DEPTH` | 16 | levels of a `json` value |
| `MAX_JSON_NODES` | 10,000 | nodes of a `json` value (containers, scalars and keys) |
| `MAX_OBJECT_MEMBERS` | 4,999 | members of one object in the text of a spec |
| `MAX_VALUE_TEXT_BYTES` | 1,048,576 | UTF-8 bytes of text in one `json`, `table` or `list` value |
| `MAX_PATCH_TEXT_BYTES` | 2,097,152 | UTF-8 bytes of text in all the values of one patch |
| `MAX_STATE_TEXT_BYTES` | 4,194,304 | UTF-8 bytes of text in the whole state of a tool |
| `MAX_SAFE_INT` | 9,007,199,254,740,991 | magnitude of an integer in an `int` position; width of a widget's range |
