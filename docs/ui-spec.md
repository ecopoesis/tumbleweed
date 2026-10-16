# UI spec reference: `tumbleweed.ui/1`

The model emits a UI spec as JSON. A fixed renderer builds GTK widgets from it and trusts
nothing else. This is the contract between the two ([ARCHITECTURE section 4](ARCHITECTURE.md),
[D5](DECISIONS.md), invariants I1, I4 and I9). It is implemented, without GTK, in
[`src/tumbleweed/ui/spec/`](../src/tumbleweed/ui/spec/), described for machines by
[`schemas/ui-spec.v1.json`](../schemas/ui-spec.v1.json), and tested in
[`tests/ui/spec/`](../tests/ui/spec/).

Parts of this page are generated from the code and checked against it by
`tests/ui/spec/test_docs.py`: the limits, the kinds and their fields, the port types, and every
JSON example, which are the golden specs in `tests/ui/spec/golden/` (a tip calculator, a photo
adjuster and a CSV viewer) or parts of them. Regenerate them with
`uv run python -m tests.ui.spec.docgen --write`. The two tables of components (bindings and
events) are compared with the data the tests use. The rest is prose, and the rules it states are
each covered by a test.

## 1. How a spec is accepted

```python
from tumbleweed.ui.spec import parse_ui_spec, validate_ui_spec, SpecError

spec = parse_ui_spec(text)  # JSON text or bytes, from a file, a package or a frame
spec = validate_ui_spec(data)  # JSON already parsed, such as a tool call's arguments
```

Both return an immutable `UiSpec` or raise `SpecError`, never anything else. `SpecError.problems`
lists at most ten problems (then a note that there are more), each at most 300 characters, each saying
where it is and what is wrong, so that it can be handed back to the model to correct itself. A
spec that fails is never rendered.

Four properties hold for everything below:

- **Closed.** Every object has a fixed set of fields and a `kind` from a fixed set. An unknown
  field or `kind` is an error. There is no extension point, no code, no CSS, no markup and no URL.
- **Strict.** A value has exactly the type that is written: `"5"` is not a number, `1` is not a
  `true`, `5.0` is not an integer, a string is not a list. Nothing is converted, except that a
  float field accepts an integer (`5` means `5.0`).
- **Immutable.** A validated spec cannot be edited; sequences are tuples. (The one exception is
  the content of a `json` state value, which is arbitrary JSON.)
- **Bounded.** Every size has a limit ([section 11](#11-limits)); at the limit is valid, one past
  it is not.

`parse_ui_spec` also refuses text over `MAX_SPEC_BYTES`, text that is not UTF-8 or not JSON, `NaN`
and `Infinity`, and an object that repeats a key (a parser would silently keep the last one, so
`{"root": A, "root": B}` would mean `B` to us and `A` to a reader of the text).
`UiSpec.model_validate_json` neither limits the size nor notices a repeated key; use it only on
text that came from `model_dump_json`.

**Nothing in a spec is parsed or evaluated (I1).** An expression is a tree of JSON objects
([section 7](#7-expressions)). Where an expression goes, a string is a validation error. There
is no node that calls a function, reads an attribute or takes a format string. The package
contains no `eval`, `exec`, `compile` or import of code, and a test fails if one is added.

## 2. An example

A tip calculator: three inputs, everything else computed, no handlers. It works with no round trip
to the agent.

<!-- generated: fixture tip_calculator -->
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
<!-- /generated -->

## 3. The envelope

| Field | Type | |
|---|---|---|
| `version` | the string `"tumbleweed.ui/1"` | required; any other version is refused |
| `title` | one line, 1 to 80 characters | required; shown on the canvas only, never in trusted chrome (I5) |
| `state` | list of [state declarations](#5-state) | optional, at most 128 |
| `root` | one [component](#8-components) | required |
| `handlers` | list of [handlers](#9-handlers) | optional, at most 64 |

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

**Numbers.** Integers are within +-(2**53 - 1), the range that stays exact in canonical JSON.
Floats are finite. The type of a number is never inferred from how it is written, because
canonical JSON writes `5.0` as `5`.

## 5. State

A state declaration has a `kind`, a `key` and a `default`, plus the fields of its kind.

<!-- generated: kinds state -->
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
<!-- /generated -->

| Kind | Value | Bounds |
|---|---|---|
| `bool` | `true` or `false` | |
| `int` | integer | `minimum`, `maximum` (inclusive), `step` (a hint for widgets, at least 1) |
| `float` | finite number | `minimum`, `maximum` (inclusive), `step` (a hint, above 0) |
| `string` | data text | `max_length`, 1 to 65,536 (default 1,000) |
| `enum` | one of the `options` | 1 to 64 distinct options, each a non-empty line of at most 128 characters; `default` is one of them |
| `image` | an `ImageRef` or null | |
| `file` | a `FileRef` or null | |
| `table` | a `Table` | `max_rows`, 1 to 10,000 (default 1,000) |
| `list` | list of `bool`, `int`, `float` or `string` (`item_type`) | `max_items`, 1 to 1,000 (default 100) |
| `json` | any JSON value within the limits | |

A default that breaks its own bounds is an error. Options that differ only in Unicode
composition (NFC, as the tool store normalises) count as the same option.

**Computed state.** An entry of kind `bool`, `int`, `float` or `string` may have a `computed`
expression over other entries. It is read-only: no widget, `set` handler, sandbox or
`patch_state` can write it. It still carries a `default`, which must be valid. The dependency
graph must be acyclic, and an entry that reads itself is a cycle; `UiSpec.computed_order()` gives
the order to evaluate in. An expression can read entries of kind `bool`, `int`, `float`, `string`
and `enum` (which reads as a string), and no others.

**Checking a value.** `check_value(declaration, value)` decides whether a value belongs to an
entry. It is how defaults are checked, and `UiSpec.validate_patch(patch, only=...)` uses it for
everything that arrives later, from `patch_state` or from a sandbox: every key must be a
declared, non-computed entry (and in `only`, a sandbox handler's declared outputs), every value
must fit, and a patch with any problem is refused whole.

## 6. Value types and ports

These are the value types that flow between tools ([ARCHITECTURE section 6](ARCHITECTURE.md)).

**`ImageRef` and `FileRef`** are `{"path": "<workspace-relative path>"}` and nothing more. The path
is a `WorkspaceRelPath`: relative, no `..`, `.` or empty component, no control characters,
within the Linux path and name limits. On top of that, a reference may not look like a URL or a
drive (a colon in the first component: `https:/x`, `file:/etc/passwd`, `data:image/png;...`,
`C:\x`), and an `ImageRef` must end in `.png`, `.jpg`, `.jpeg` or `.webp`, in any case, never
`.svg` (I4). These checks are lexical. They cannot see symlinks, so the renderer and the broker
still open a reference component by component without following links (I11), and still sniff
an image's bytes before decoding it (S11).

**`Table`** is `{"columns": [{"name": ..., "type": ...}], "rows": [[...], ...]}`. A column type is
`string`, `int`, `float` or `bool`, and every row has one cell of that type per column (an
integer is a valid `float` cell; `1.5` is not an `int` cell, nor is an integer beyond 2**53 - 1).
Column names are distinct and non-empty. A table has at most 32 columns, 10,000 rows and 20,000
cells. A `table` entry bounds the rows but does not fix the columns: the value brings its own,
because a CSV viewer cannot know the headers of a file it has not read.

**`json`** values are any JSON within the depth and node limits, with strings of at most 65,536
characters and safe integers.

**Ports.** A tool's typed input and output ports, in the tool store (S23) and in composition
(S25), use `PortType`. Each kind of state maps to a port type, or to none:

<!-- generated: ports -->
| State kind | Port type |
|---|---|
| `bool` | none: local to the tool |
| `int` | none: local to the tool |
| `float` | none: local to the tool |
| `string` | `text` |
| `enum` | none: local to the tool |
| `image` | `image` |
| `file` | `file` |
| `table` | `table` |
| `list` | none: local to the tool |
| `json` | `json` |

The port types are `text`, `json`, `table`, `image`, `file`.
<!-- /generated -->

## 7. Expressions

An expression is a JSON object with a `kind`. Operands are expressions, never strings.

<!-- generated: kinds expr -->
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
<!-- /generated -->

`bool`, `int`, `float` and `string` are literals (`{"kind": "int", "value": 100}`); `ref` reads
a state entry (`{"kind": "ref", "key": "bill"}`). `min`, `max`, `and`, `or` and `concat` take
2 to 8 operands in `args`. `if` takes `condition`, `then` and `otherwise` (`else` is a reserved
word in some languages, so it is not used). `format_number` takes a number and the bounded
parameters `decimals` (0 to 10, default 0) and `grouping` (default `false`); there is no format
string and no locale.

For example, the tip, rounded to cents, from the calculator above (`bill * tip_percent / 100`,
to two decimals):

<!-- generated: fragment tip_calculator /state/3/computed -->
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
<!-- /generated -->

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
  literal is one of its options.
- `and`, `or` and `not` take bools and are a `bool`.
- `if` takes a `bool` and two branches of one type, or two numbers (then a `float` if either is
  one). Both branches are always checked.
- `concat` takes strings and is a `string`. A number is never converted to a string except by
  `format_number`.

Nothing converts between a number and a string, a bool and a number, or a float and an int. A
computed entry or an assignment must have a type that fits its target: `bool` to `bool`, `int`
to `int`, `int` or `float` to `float`, `string` to `string` or to an `enum` (a literal must be
one of its options).

### Limits of an expression

At most 16 levels, 256 nodes, and 2,048 nodes in all the expressions of one spec. The limits are
checked by every node as it is built, so an over-deep expression is refused at the first node
that exceeds the limit, wherever it was built.

### Evaluation

The syntax and the types above are what this step defines and checks. The interpreter is S10's.
The meaning below is part of the contract so that a spec means the same everywhere, and S10
implements it:

- The interpreter is total: no expression raises. `div` by zero is `0.0`.
- An `int` result is clamped to +-(2**53 - 1). A `float` result that is infinite becomes the
  largest finite float of that sign, and one that is not a number becomes `0.0`.
- `round` rounds halves away from zero. `min` and `max` of mixed numbers are `float`s.
- `eq` and `ne` compare numbers by value (`1` equals `1.0`) and strings by code point, with no
  normalisation.
- `format_number` writes the number rounded half away from zero to `decimals` places, with `.`
  before the decimals, `-` for a negative number (never `-0`), and, with `grouping`, a `,`
  between groups of three digits of the whole part. It never depends on the locale.
- When a value is written to an entry (by a computed entry, a `set` handler or a widget), a number is
  clamped into `minimum` and `maximum`, a string is cut to `max_length` characters, and an
  `enum` entry given a string that is not one of its options keeps its old value. Values that
  arrive from `patch_state` or a sandbox are not clamped: they are checked by `check_value` and
  refused whole.
- Computed entries are evaluated in `computed_order()` and re-evaluated, in that order,
  whenever an entry they read changes.

## 8. Components

A component is a JSON object with a `kind`.

<!-- generated: kinds component -->
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
<!-- /generated -->

`box` lays its children out in a row or a column; `grid` places them left to right, then top to
bottom, `columns` to a row; `tabs` shows one page at a time, each page a `title` and a `content`
component; `expander` shows or hides its `child`. A container has at most 64 children (or pages).
`label` and `heading` show either static `text` or the value of an entry (`bind`): exactly one of
the two. A heading's `level` is 1 to 3. `image` has `alt` text for accessibility. `text_view`
is read-only unless `editable`. All text a component shows is plain text.

What a widget shows or edits is an entry named by `bind`. The entry's declaration supplies range,
options and limits, so they are written once. Which kinds of entry a component accepts:

<!-- verified: bindings -->
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
`maximum` (a slider or a progress bar has no range otherwise).

A component's events name a handler, or none:

<!-- verified: events -->
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

The tree is at most 16 levels deep and has at most 512 components.

## 9. Handlers

A handler has a `name` and a `kind`.

<!-- generated: kinds handler -->
| `kind` | Fields (`?` is optional) |
|---|---|
| `set` | `name`, `assignments` |
| `sandbox` | `name`, `module`, `function`, `inputs?`, `outputs`, `debounce_ms?` |
| `agent` | `name`, `note?` |
<!-- /generated -->

- **`set`** assigns the results of expressions to entries, in the renderer. Every expression is
  evaluated against the state as it was when the event fired, and then the assignments are
  applied together, so no assignment sees another's result. A target may appear once. A target is
  a declared, non-computed entry of kind `bool`, `int`, `float`, `string` or `enum`, and the
  expression's type must fit it ([section 7](#types)).
- **`sandbox`** calls `function` of the tool's code `module` in a sandbox (S20). The call gets
  the `inputs` as JSON and nothing else. Its result may write only the `outputs`: declared,
  non-computed entries, validated with `validate_patch(result, only=outputs)` before any is
  applied. `debounce_ms` (0 to 10,000) waits that long after the last event before calling; 0
  calls at once. At most one call per tool instance runs at a time (I9). The spec holds no code and
  does not say whether the module exists: the tool package does (S23). There is no field for source,
  a command, a path, an environment, the network or a timeout; the broker's policy decides those.
- **`agent`** sends the event and a snapshot of the state to the agent as untrusted input (S12).
  `note` is the author's hint about what the event means, and is data too.

Handlers fire only when a user acts on a widget. A change of state from anywhere else (a
computed entry updating, a `set` or sandbox result, `patch_state`, the model) never fires a
handler, so handlers cannot trigger one another.

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
- the component tree and the expressions are within their limits.

All problems are reported together (up to ten), each naming the component by its path and
kind (`root.children[2] (slider)`), or the entry or handler.

## 11. Limits

<!-- generated: limits -->
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
| `MAX_SAFE_INT` | 9,007,199,254,740,991 | magnitude of an integer anywhere in a spec (exact in canonical JSON) |
<!-- /generated -->

## 12. Canonical form and compatibility

The tool store (S23) hashes a canonical serialisation of the spec (RFC 8785 JSON, with text
normalised to NFC and LF line endings) when a tool is saved, and keeps the saved bytes. The
models are built so that this needs no change to the spec:

- every number is exact in canonical JSON (safe integers, finite floats), and no type depends on how
  a number is written;
- the dump of a spec (`model_dump_json`) writes every field, so a default that is written and one
  that is left out dump to the same bytes;
- two things that are distinct as written and the same after NFC normalisation, such as enum
  options and table column names, are refused as duplicates now rather than after saving;
- a spec holds defaults, never the user's current values.

Text is not required to be NFC. Saving normalises it and validates the result again; a few
characters are longer in NFC, so text exactly at its limit can be refused then.

The models are closed, so an application opens only the `version` it knows, and refuses another with a
clear error. Within `tumbleweed.ui/1` changes keep every spec that was valid valid: new optional
fields with defaults, and new kinds. A change that would make a valid spec invalid, or change what
one means, is `tumbleweed.ui/2`.

## 13. For the steps that use this

- **Runtime (S10, S11).** `UiSpec.declarations()`, `computed_order()` and `check_value` for typed state; the
  [evaluation rules](#evaluation); handlers fire only from user input events (origin tracking). Equal
  values assigned to an `image` entry (a handler rewriting the same file) must still refresh the
  widget: notify on assignment, not on change.
- **Agent (S12).** `parse_ui_spec` for text and `validate_ui_spec` for a tool call's arguments;
  return `SpecError.problems` to the model; `UiSpec.validate_patch(patch)` for `patch_state`; the
  system prompt section is generated from `schemas/ui-spec.v1.json` and this page.
- **Sandboxed code (S20).** `validate_patch(result, only=handler.outputs)` on every result.
- **Tool store and composition (S23, S25).** `PortType` and `port_type_of`, `ImageRef`, `FileRef`,
  `Table`, and the canonical form above.

## 14. More examples

A photo adjuster: sandbox handlers, a `set` handler, an image result. The photo to adjust is
a `string` entry holding the path the user picked; the renderer never opens it, the sandbox
reads it if a grant allows (S19, S20). The result is an `image` entry, which is a workspace
reference.

<!-- generated: fixture photo_adjuster -->
```json
{
  "version": "tumbleweed.ui/1",
  "title": "Photo adjuster",
  "state": [
    {"kind": "string", "key": "photo_path", "default": "", "max_length": 1024},
    {"kind": "int", "key": "brightness", "default": 0, "minimum": -100, "maximum": 100},
    {"kind": "int", "key": "contrast", "default": 0, "minimum": -100, "maximum": 100},
    {"kind": "bool", "key": "grayscale", "default": false},
    {"kind": "image", "key": "result"},
    {
      "kind": "string",
      "key": "status",
      "default": "Enter the path of a photo, then press Adjust.",
      "max_length": 200
    },
    {
      "kind": "string",
      "key": "summary",
      "default": "",
      "computed": {
        "kind": "concat",
        "args": [
          {"kind": "string", "value": "Brightness "},
          {"kind": "format_number", "operand": {"kind": "ref", "key": "brightness"}},
          {"kind": "string", "value": ", contrast "},
          {"kind": "format_number", "operand": {"kind": "ref", "key": "contrast"}},
          {
            "kind": "if",
            "condition": {"kind": "ref", "key": "grayscale"},
            "then": {"kind": "string", "value": ", grayscale"},
            "otherwise": {"kind": "string", "value": ""}
          }
        ]
      }
    }
  ],
  "root": {
    "kind": "box",
    "children": [
      {"kind": "heading", "text": "Photo adjuster"},
      {
        "kind": "entry",
        "bind": "photo_path",
        "placeholder": "Path of a photo",
        "on_activate": "adjust"
      },
      {"kind": "image", "bind": "result", "alt": "The adjusted photo"},
      {
        "kind": "grid",
        "columns": 2,
        "children": [
          {"kind": "label", "text": "Brightness"},
          {"kind": "slider", "bind": "brightness", "on_change": "adjust"},
          {"kind": "label", "text": "Contrast"},
          {"kind": "slider", "bind": "contrast", "on_change": "adjust"},
          {"kind": "label", "text": "Grayscale"},
          {"kind": "switch", "bind": "grayscale", "on_change": "adjust"}
        ]
      },
      {"kind": "label", "bind": "summary"},
      {
        "kind": "box",
        "orientation": "horizontal",
        "children": [
          {"kind": "button", "label": "Adjust", "on_click": "adjust"},
          {"kind": "button", "label": "Reset", "on_click": "reset"}
        ]
      },
      {"kind": "label", "bind": "status"}
    ]
  },
  "handlers": [
    {
      "kind": "sandbox",
      "name": "adjust",
      "module": "photo",
      "function": "adjust",
      "inputs": ["photo_path", "brightness", "contrast", "grayscale"],
      "outputs": ["result", "status"],
      "debounce_ms": 250
    },
    {
      "kind": "set",
      "name": "reset",
      "assignments": [
        {"target": "brightness", "value": {"kind": "int", "value": 0}},
        {"target": "contrast", "value": {"kind": "int", "value": 0}},
        {"target": "grayscale", "value": {"kind": "bool", "value": false}}
      ]
    }
  ]
}
```
<!-- /generated -->

A CSV viewer: a `table` whose columns come from the file, tabs, a dropdown and a read-only
text view.

<!-- generated: fixture csv_viewer -->
```json
{
  "version": "tumbleweed.ui/1",
  "title": "CSV viewer",
  "state": [
    {"kind": "string", "key": "csv_path", "default": "", "max_length": 1024},
    {"kind": "enum", "key": "delimiter", "options": ["comma", "semicolon", "tab"], "default": "comma"},
    {"kind": "bool", "key": "has_header", "default": true},
    {"kind": "table", "key": "data", "max_rows": 1000},
    {"kind": "string", "key": "raw_head", "default": "", "max_length": 4096},
    {
      "kind": "string",
      "key": "status",
      "default": "Enter the path of a CSV file, then press Load.",
      "max_length": 200
    }
  ],
  "root": {
    "kind": "box",
    "children": [
      {"kind": "heading", "text": "CSV viewer"},
      {
        "kind": "grid",
        "columns": 2,
        "children": [
          {"kind": "label", "text": "File"},
          {"kind": "entry", "bind": "csv_path", "placeholder": "Path of a CSV file", "on_activate": "load"},
          {"kind": "label", "text": "Delimiter"},
          {"kind": "dropdown", "bind": "delimiter"},
          {"kind": "label", "text": "First row is a header"},
          {"kind": "switch", "bind": "has_header"}
        ]
      },
      {"kind": "button", "label": "Load", "on_click": "load"},
      {"kind": "label", "bind": "status"},
      {
        "kind": "tabs",
        "pages": [
          {"title": "Table", "content": {"kind": "table", "bind": "data"}},
          {
            "title": "Raw text",
            "content": {"kind": "text_view", "bind": "raw_head", "monospace": true}
          }
        ]
      }
    ]
  },
  "handlers": [
    {
      "kind": "sandbox",
      "name": "load",
      "module": "csv_view",
      "function": "load",
      "inputs": ["csv_path", "delimiter", "has_header"],
      "outputs": ["data", "raw_head", "status"]
    }
  ]
}
```
<!-- /generated -->

## 15. Changing the spec

After a change to the models, regenerate the schema and this page, and commit them:

```sh
uv run python -m tumbleweed.ui.spec.schema --write
uv run python -m tests.ui.spec.docgen --write
```

`tests/ui/spec/test_schema.py` and `tests/ui/spec/test_docs.py` fail until you do.
