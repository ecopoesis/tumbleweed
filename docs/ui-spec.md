# UI spec reference: `tumbleweed.ui/1`

The model emits a UI spec as JSON. A fixed renderer builds GTK widgets from it and trusts
nothing else. This is the contract between the two ([ARCHITECTURE section 4](ARCHITECTURE.md),
[D5](DECISIONS.md), invariants I1, I4 and I9). It is implemented, without GTK, in
[`src/tumbleweed/ui/spec/`](../src/tumbleweed/ui/spec/) (what a spec is) and
[`src/tumbleweed/ui/runtime/`](../src/tumbleweed/ui/runtime/) (what it does: [section 7](#evaluation),
[section 9](#9-handlers)), described for machines by
[`schemas/ui-spec.v1.json`](../schemas/ui-spec.v1.json) (shape, types and bounds; **looser than the
models**, because JSON Schema cannot say what [section 10](#10-what-is-checked-across-the-spec)
lists, nor the character rules for text and paths, nor the size budgets: it is a guide for
writing a spec, never a validator), and tested in
[`tests/ui/spec/`](../tests/ui/spec/) and [`tests/ui/runtime/`](../tests/ui/runtime/).

Parts of this page are generated from the code and checked against it by
`tests/ui/spec/test_docs.py`: the limits, the kinds and their fields, the port types, and every
JSON example, which are the golden specs in `tests/ui/spec/golden/` (a tip calculator, a photo
adjuster and a CSV viewer) or parts of them. Regenerate them with
`uv run python -m tests.ui.spec.docgen --write`. The numbers in the prose (limits and defaults) are
written between `limit` comments and regenerated the same way, and a test fails on a bare number
that is a limit. The two tables of components (bindings and events) are compared with the data the
tests use. The rest is prose. Every rule it states about what is accepted or refused is covered by
a test in `tests/ui/spec/`, and every rule of evaluation and of handlers by a test in
`tests/ui/runtime/`; what it says about the sandbox, the debounce floor and the tool store is the
contract that S20 and S23 implement and test.

## 1. How a spec is accepted

```python
from tumbleweed.ui.spec import parse_ui_spec, validate_ui_spec, SpecError

spec = parse_ui_spec(text)  # JSON text or bytes, from a file, a package or a frame
spec = validate_ui_spec(data)  # JSON already parsed, such as a tool call's arguments
```

Untrusted text, from the model, a file or another process, goes through one of these two and
nothing else. `validate_ui_spec` writes the data out as JSON text and gives that to
`parse_ui_spec`, so the two have the same size limit, the same refusals and the same messages;
the data must be what `json.loads` would give: a `dict` with `str` keys, a `list`, `str`, `int`,
`float`, `bool` or `None`, exactly. A tuple, a set, `bytes`, an enum member, a subclass of any of
these, a key that is not a string (`json.dumps` would quietly write `{1: "a"}` as `{"1": "a"}`),
`NaN` and a cycle are a `SpecError`, never converted.

Both return an immutable `UiSpec` or raise `SpecError`, never anything else. `SpecError.problems`
lists at most <!-- limit: MAX_REPORTED_PROBLEMS -->10<!-- /limit --> problems (then a note that there are more), each at most
<!-- limit: MAX_PROBLEM_LENGTH -->300<!-- /limit --> characters, each saying where it is and what is wrong, so that it can be handed back to
the model to correct itself. A spec that fails is never rendered. A problem is inert text: a
character that is not printable (a control character, a bidirectional override, a line or
paragraph separator, a lone surrogate) is written `\uXXXX`, so what the model sent cannot forge a
line or reorder text in a log, a terminal or the chat; an unknown field name is cut to
<!-- limit: MAX_LOCATION_PART -->40<!-- /limit --> characters and an unknown `kind` is not echoed at all. Collecting the problems
costs in proportion to the size of the spec and no more: the sequences of a spec stop at their
first error, and an object of more than <!-- limit: MAX_OBJECT_MEMBERS -->4,999<!-- /limit --> members is refused while the text is parsed, so a spec of the largest
size costs a few tens of megabytes at most to refuse (a test holds it to a fixed budget).

Four properties hold for everything below:

- **Closed.** Every object has a fixed set of fields and a `kind` from a fixed set. An unknown
  field or `kind` is an error. There is no extension point, no code, no CSS, no markup and no URL.
- **Strict.** A value has exactly the type that is written: `"5"` is not a number, `1` is not a
  `true`, `5.0` is not an integer, a string is not a list. Nothing is converted, except that a
  float position accepts an integer that stands for a double (`5` means `5.0`,
  [section 4](#4-names-text-and-numbers)).
- **Immutable.** A validated spec cannot be edited; sequences are tuples. (The one exception is
  the content of a `json` state value, which is arbitrary JSON.)
- **Bounded.** Every size has a limit ([section 11](#11-limits)); at the limit is valid, one past
  it is not.

`parse_ui_spec` also refuses text over <!-- limit: MAX_SPEC_BYTES -->262,144<!-- /limit --> bytes (`MAX_SPEC_BYTES`), text that is
not UTF-8 or not JSON, `NaN` and `Infinity`, an object of more than `MAX_OBJECT_MEMBERS`
members, and an object that repeats a key (a parser would
silently keep the last one, so `{"root": A, "root": B}` would mean `B` to us and `A` to a reader of
the text; found in one pass, so a huge object costs no more than its size). `UiSpec.model_validate`
and `UiSpec.model_validate_json` neither limit the size nor notice a repeated key: use them only on
what came from `model_dump_json`, never on untrusted input.

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
| `title` | one line, 1 to <!-- limit: MAX_TITLE_LENGTH -->80<!-- /limit --> characters | required; shown on the canvas only, never in trusted chrome (I5) |
| `state` | list of [state declarations](#5-state) | optional, at most <!-- limit: MAX_STATE_ENTRIES -->128<!-- /limit --> |
| `root` | one [component](#8-components) | required |
| `handlers` | list of [handlers](#9-handlers) | optional, at most <!-- limit: MAX_HANDLERS -->64<!-- /limit --> |

State keys are unique and handler names are unique.

## 4. Names, text and numbers

**Names.** State keys, handler names, module names and function names are lowercase snake_case
ASCII: a letter, then letters, digits and `_`, at most <!-- limit: MAX_NAME_LENGTH -->64<!-- /limit --> characters. They are four distinct
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
| `int` | integer within +-(2**53 - 1) | `minimum`, `maximum` (inclusive), `step` (a hint for widgets, at least 1) |
| `float` | finite double | `minimum`, `maximum` (inclusive), `step` (a hint, above 0) |
| `string` | data text | `max_length`, 1 to <!-- limit: MAX_STRING_LENGTH -->65,536<!-- /limit --> (default <!-- limit: DEFAULT_STRING_MAX_LENGTH -->1,000<!-- /limit -->) |
| `enum` | one of the `options` | 1 to <!-- limit: MAX_ENUM_OPTIONS -->64<!-- /limit --> distinct options, each a non-empty line of at most <!-- limit: MAX_OPTION_LENGTH -->128<!-- /limit --> characters; `default` is one of them |
| `image` | an `ImageRef` or null | |
| `file` | a `FileRef` or null | |
| `table` | a `Table` | `max_rows`, 1 to <!-- limit: MAX_TABLE_ROWS -->10,000<!-- /limit --> (default <!-- limit: DEFAULT_TABLE_MAX_ROWS -->1,000<!-- /limit -->) |
| `list` | list of `bool`, `int`, `float` or `string` (`item_type`) | `max_items`, 1 to <!-- limit: MAX_LIST_ITEMS -->1,000<!-- /limit --> (default <!-- limit: DEFAULT_LIST_MAX_ITEMS -->100<!-- /limit -->) |
| `json` | any JSON value within the limits | |

A default that breaks its own bounds is an error. Options that differ only in Unicode
composition (NFC, as the tool store normalises) count as the same option.

**Size.** A `string` is at most `max_length` characters. A `json`, `table` or `list` value holds at
most <!-- limit: MAX_VALUE_TEXT_BYTES -->1,048,576<!-- /limit --> bytes of text (the UTF-8 bytes of every string, key and column name in it;
numbers and flags are bounded by the count limits). A patch holds at most <!-- limit: MAX_PATCH_TEXT_BYTES -->2,097,152<!-- /limit --> bytes of
text, and the state of a tool at most <!-- limit: MAX_STATE_TEXT_BYTES -->4,194,304<!-- /limit -->. `UiSpec` checks the defaults against the last,
`UiSpec.validate_patch` a patch against the second and, given `current`, the values the state
holds now, the state after the patch against the third, but only if the patch makes the text of
the state grow: a state that is over its budget can still shrink and still change what holds no
text, or a tool would be wedged. `current` is required and is the whole state, computed entries
included (their values are text too, as the defaults of computed entries are in the check of the
defaults); pass `{}` for an empty state. `value_text_bytes` measures a value; the runtime keeps
the sum within the budget as values change ([evaluation](#evaluation)). The checks stop at the first limit crossed, and
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

## 6. Value types and ports

These are the value types that flow between tools ([ARCHITECTURE section 6](ARCHITECTURE.md)).

**`ImageRef` and `FileRef`** are `{"path": "<workspace-relative path>"}` and nothing more. The path
is a `WorkspaceRelPath`: relative, no `..`, `.` or empty component, no control characters,
within the Linux path and name limits. On top of that, a reference has no backslash, and its
first component may not begin with a URL scheme or a drive letter (a letter, then letters,
digits, `+`, `.` or `-`, then a colon, after any blanks, as URL parsers skip them: `https:/x`,
`file:/etc/passwd`, `data:image/png;...`, `c:/x`). A colon that ends no scheme is a file name:
`report 12:30.png` and `2024-01-01T10:30:00.png` are fine. An `ImageRef` must end in `.png`,
`.jpg`, `.jpeg` or `.webp`, in any case, never `.svg` (I4). These checks are lexical: they
cannot see symlinks.

**A reference is a file name and nothing else.** Trusted code opens it only through a directory
file descriptor (`dirfd`: `openat`, component by component, with `O_NOFOLLOW`), without following
links (I11), and still sniffs an image's bytes before decoding it (S11). It is never passed to
anything that interprets a string: not `expanduser` (`~root/x.png` is a directory called `~root`,
not a home), not a URI builder or a URL or percent decoder (`%2e%2e/x.png` is a directory called
`%2e%2e`, not `..`, and a `file:` URI is never made from one), not a glob or a shell, and not an
argument of a command unless it goes after `--` or has `./` in front (`-rf.png` is a valid
reference, and an option to most programs). The spec accepts all three of those names on purpose,
each being a legal file name; the code that opens a reference is what has to treat them as
plain names (S11 tests `-rf.png`, `~root/x.png` and `%2e%2e/x.png`).

**`Table`** is `{"columns": [{"name": ..., "type": ...}], "rows": [[...], ...]}`. A column type is
`string`, `int`, `float` or `bool`, and every row has one cell of that type per column (an
integer is accepted in a `float` column and stored as a float; `1.5` is not an `int` cell, nor is an
integer beyond 2**53 - 1). A cell is checked once, as a string, a number or a bool, whatever
the column.
Column names are non-empty and distinct once normalised as the tool store does (NFC, line feeds
for line endings). A table has at most <!-- limit: MAX_TABLE_COLUMNS -->32<!-- /limit --> columns, <!-- limit: MAX_TABLE_ROWS -->10,000<!-- /limit --> rows and <!-- limit: MAX_TABLE_CELLS -->20,000<!-- /limit -->
cells, a cell is at most <!-- limit: MAX_CELL_LENGTH -->1,000<!-- /limit --> characters. A `table` entry bounds the rows but does not fix the columns: the value brings its own,
because a CSV viewer cannot know the headers of a file it has not read.

**`json`** values are any JSON at most <!-- limit: MAX_JSON_DEPTH -->16<!-- /limit --> levels deep and <!-- limit: MAX_JSON_NODES -->10,000<!-- /limit --> nodes, with strings of
at most <!-- limit: MAX_STRING_LENGTH -->65,536<!-- /limit --> characters, numbers by the rule of [section 4](#4-names-text-and-numbers),
and object keys that are distinct once normalised (NFC, line feeds).

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
2 to <!-- limit: MAX_EXPR_ARGS -->8<!-- /limit --> operands in `args`. `if` takes `condition`, `then` and `otherwise` (`else` is a reserved
word in some languages, so it is not used). `format_number` takes a number and the bounded
parameters `decimals` (0 to <!-- limit: MAX_DECIMALS -->10<!-- /limit -->, default 0) and `grouping` (default `false`); there is no format
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

At most <!-- limit: MAX_EXPR_DEPTH -->16<!-- /limit --> levels, <!-- limit: MAX_EXPR_NODES -->256<!-- /limit --> nodes, and <!-- limit: MAX_TOTAL_EXPR_NODES -->2,048<!-- /limit --> nodes in all the expressions of one spec
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
- `concat` stops at <!-- limit: MAX_STRING_LENGTH -->65,536<!-- /limit --> characters, the longest a `string` entry can be, so that an
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
  rewritten file ([section 13](#13-for-the-steps-that-use-this)); a computed entry is announced
  when its value or its stale mark changes.

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
component; `expander` shows or hides its `child`. A container has at most <!-- limit: MAX_CONTAINER_CHILDREN -->64<!-- /limit --> children (or pages).
`label` and `heading` show either static `text` or the value of an entry (`bind`): exactly one of
the two. A heading's `level` is 1 to 3. `image` has `alt` text for accessibility. `text_view`
is read-only unless `editable`. All text a component shows is plain text.

**Text on the screen is bounded (I9).** A renderer lays text out on the thread that also serves the
trusted chrome, and laying out text takes time that grows faster than its length, so what is shown is
bounded, though the state holds the whole value. The reference renderer shows at most 8,192
characters of one `label`, `heading`, bound text or `entry`, followed by ` (cut)` for the labels,
and at most <!-- limit: MAX_STRING_LENGTH -->65,536<!-- /limit --> characters in all in a tool
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

A `text_view` that has `on_change` must be `editable`: a read-only view has no edits to report.

The tree is at most <!-- limit: MAX_COMPONENT_DEPTH -->16<!-- /limit --> levels deep and has at most <!-- limit: MAX_COMPONENTS -->512<!-- /limit --> components, and a spacing is at most
<!-- limit: MAX_SPACING -->64<!-- /limit --> pixels, a grid at most <!-- limit: MAX_GRID_COLUMNS -->12<!-- /limit --> columns wide.

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
  applied. A `set` handler holds at most <!-- limit: MAX_HANDLER_ITEMS -->16<!-- /limit --> assignments and a `sandbox` handler at most
  <!-- limit: MAX_HANDLER_ITEMS -->16<!-- /limit --> inputs and <!-- limit: MAX_HANDLER_ITEMS -->16<!-- /limit --> outputs, at least one output (the result is a patch: a call that may write nothing gives
  the user no feedback, and a handler that only has side effects can write a status entry). `debounce_ms` (0 to <!-- limit: MAX_DEBOUNCE_MS -->10,000<!-- /limit -->) waits that long after the last event before calling; 0
  calls at once in the spec, but S20's runtime applies a minimum debounce floor to a sandbox
  handler driven by `on_change` of a slider or a spin button (I9: slider-driven handlers are
  debounced), and coalesces events that arrive while a call is in flight. At most one call per tool instance runs at a time (I9). The spec holds no code and
  does not say whether the module exists: the tool package does (S23). There is no field for source,
  a command, a path, an environment, the network or a timeout; the broker's policy decides those.
- **`agent`** sends the event and a snapshot of the state to the agent as untrusted input (S12).
  `note` is the author's hint about what the event means, and is data too.

Handlers fire only when a user acts on a widget. A change of state from anywhere else (a
computed entry updating, a `set` or sandbox result, `patch_state`, the model) never fires a
handler, so handlers cannot trigger one another. The runtime tracks where every change came from
(`tumbleweed.ui.runtime.Origin`) and the widgets tell an edit by the user from the runtime putting a
value into them, so that the second is never taken for the first. An `on_change` handler fires after
the edit has been written, and does not fire if the write was held back ([evaluation](#evaluation))
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
  ([section 8](#8-components)), and a `text_view` with `on_change` is `editable`;
- the component tree and the expressions are within their limits, and the text in the defaults of
  all the state is within its budget.

All problems are reported together (up to <!-- limit: MAX_REPORTED_PROBLEMS -->10<!-- /limit -->), each naming the component by its path and
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
| `MAX_OBJECT_MEMBERS` | 4,999 | members of one object in the text of a spec |
| `MAX_VALUE_TEXT_BYTES` | 1,048,576 | UTF-8 bytes of text in one `json`, `table` or `list` value |
| `MAX_PATCH_TEXT_BYTES` | 2,097,152 | UTF-8 bytes of text in all the values of one patch |
| `MAX_STATE_TEXT_BYTES` | 4,194,304 | UTF-8 bytes of text in the whole state of a tool |
| `MAX_SAFE_INT` | 9,007,199,254,740,991 | magnitude of an integer in an `int` position; width of a widget's range |
<!-- /generated -->

## 12. Canonical form and compatibility

The tool store (S23) hashes a canonical serialisation of the spec (RFC 8785 JSON, with text
normalised to NFC and LF line endings) when a tool is saved, and keeps the saved bytes. The
models are built so that this needs no change to the spec:

- every number follows the one rule of [section 4](#4-names-text-and-numbers): a spec that is valid
  stays valid, with the same meaning, after canonicalisation writes `1e16` as `10000000000000000`,
  `5.0` as `5` or `2**60` as `1152921504606847000`, and no type depends on how a number is
  written (`tumbleweed.core.jcs` is the canonicaliser, checked against the RFC's own vectors);
- the dump of a spec (`model_dump_json`) writes every field, so a default that is written and one
  that is left out dump to the same bytes, and an integer given for a float (a float field, a
  float column of a table, an item of a list of floats) is stored as a float, so `5` and `5.0`
  dump alike;
- two things that are distinct as written and the same after normalisation (NFC, line feeds),
  such as enum options, table column names and the keys of a JSON object, are refused as
  duplicates now rather than after saving;
- a spec holds defaults, never the user's current values.

Text is not required to be NFC. Saving normalises it and validates the result again; a few
characters are longer in NFC, so text exactly at its limit can be refused then.

The models are closed, so an application opens only the `version` it knows, and refuses another with a
clear error. Within `tumbleweed.ui/1` changes keep every spec that was valid valid: new optional
fields with defaults, and new kinds. A change that would make a valid spec invalid, or change what
one means, is `tumbleweed.ui/2`.

## 13. For the steps that use this

- **Runtime (S10, S11).** `tumbleweed.ui.runtime.UiRuntime` holds a spec's state and handlers
  (`StateStore`, `evaluate`, `coerce_for_write`), built on `UiSpec.declarations()`,
  `computed_order()` and `check_value`; the [evaluation rules](#evaluation) each have a test, and
  handlers fire only from user input events (origin tracking). `sandbox` and `agent` handlers go to
  sinks passed to the runtime (`SandboxSink`, `AgentSink`; S20, S12). Equal values assigned to an
  `image` entry (a handler rewriting the same file) refresh the widget: the store notifies on
  assignment, not on change. The state is kept within `MAX_STATE_TEXT_BYTES`. The GTK adapter
  (`tumbleweed.ui.gtk`) creates no popup, dialog, tooltip or drag icon from a spec (a `dropdown` is
  a list in place, ARCHITECTURE I5), and shows an image, table, text view, tabs and expander as a
  placeholder until S11.
- **Agent (S12).** `parse_ui_spec` for text and `validate_ui_spec` for a tool call's arguments;
  return `SpecError.problems` to the model; `UiSpec.validate_patch(patch, current=...)` for
  `patch_state`; the system prompt section is generated from `schemas/ui-spec.v1.json` and this
  page, and must carry the rules the schema cannot (section 4's text and numbers, paths).
- **Sandboxed code (S20).** `validate_patch(result, current=..., only=handler.outputs)` on every
  result; the debounce floor and the one call in flight of [section 9](#9-handlers).
- **Tool store and composition (S23, S25).** `PortType` and `port_type_of`, `ImageRef`, `FileRef`,
  `Table`, and the canonical form above (`tumbleweed.core.jcs.canonicalize`, which refuses an
  integer that stands for no double instead of rounding it).

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
