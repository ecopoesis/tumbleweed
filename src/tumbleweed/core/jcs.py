"""RFC 8785 JSON Canonicalization Scheme (JCS): one byte sequence for one JSON value.

The tool store (S23) hashes the canonical JSON of what it saves and keeps those bytes, and the
UI spec (`tumbleweed.ui.spec`) states its number rules so that what validates still validates
after this canonicalisation. Both need the same formatter, so it lives here, in `core`, which
neither package has to import the other for. It is not specific to UI specs.

Object members are sorted by the UTF-16 code units of their names, there is no insignificant
whitespace, strings are escaped as `JSON.stringify` does, and every number is an IEEE-754 double
written as ECMAScript's `Number.prototype.toString` writes it. JSON text has no integer type, so
an integer in a value must stand for a double (`double_of_int`): the canonical form of a double
above 2**53 is its shortest digits padded with zeros (`2**60` is `1152921504606847000`), which
reads back as an integer that is no longer exactly the double, and a canonicaliser that rounded
such an integer silently would give two values one hash.

Canonicalising is iterative and has a budget of values and of output bytes (`canonicalize`), so
a hostile value costs a bounded amount of time and memory and cannot reach the recursion limit
(#62). That bounds the work, not what is valid: callers still validate a value (a tool package is
a strict model, `tumbleweed.tools`) and canonicalise only what passed.

In a `json` value, an int and a float of the same value are one value here (`5.0` is written
`5`), and the bytes are stable; a consumer must not depend on which of the two it had.
"""

import math
from typing import Final

# JCS writes a double of magnitude 1e21 or more in exponent form (`1e+21`), never as digits, so
# no integer from here up is the way JCS spells a double (an exact one, such as 2**70, is still
# the double it equals).
_DIGITS_LIMIT: Final = 10**21
_MAX_DEPTH: Final = 200  # what the JSON parser of pydantic accepts, too
MAX_NODES: Final = 1_000_000
"""The default budget of values (containers, scalars and object keys) in one canonicalisation."""
MAX_BYTES: Final = 16 * 1024 * 1024
"""The default budget of output bytes (UTF-8) of one canonicalisation."""

_ESCAPES: Final = {
    ord('"'): '\\"',
    ord("\\"): "\\\\",
    ord("\b"): "\\b",
    ord("\f"): "\\f",
    ord("\n"): "\\n",
    ord("\r"): "\\r",
    ord("\t"): "\\t",
} | {
    code: f"\\u{code:04x}"
    for code in range(0x20)
    if code not in {ord("\b"), ord("\f"), ord("\n"), ord("\r"), ord("\t")}
}


def es_number(value: float) -> str:
    """`Number.prototype.toString` of a finite double (RFC 8785 section 3.2.2.3)."""
    if not math.isfinite(value):
        raise ValueError("JSON has no NaN or infinity")
    if value == 0:
        return "0"  # also for -0
    if value < 0:
        return "-" + es_number(-value)
    # The shortest digits that identify the double, which is what `repr` gives, as `d.ddde+xx`.
    mantissa, _, shown_exponent = repr(value).partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = (whole + fraction).lstrip("0")
    exponent = int(shown_exponent or 0) - len(fraction)
    trimmed = digits.rstrip("0")
    exponent += len(digits) - len(trimmed)  # the zeros dropped here are part of the exponent
    digits = trimmed
    count = len(digits)
    point = count + exponent  # the value is 0.<digits> * 10**point
    if count <= point <= 21:
        return digits + "0" * (point - count)
    if 0 < point <= 21:
        return digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return "0." + "0" * -point + digits
    shown = point - 1
    mantissa = digits if count == 1 else digits[0] + "." + digits[1:]
    return f"{mantissa}e{'+' if shown >= 0 else '-'}{abs(shown)}"


def double_of_int(value: int) -> float | None:
    """The double an integer in JSON text stands for, or None if it stands for none.

    An integer stands for a double in two cases: it is exactly that double (`2**53`, `2**70`),
    or it is the way `es_number` writes the nearest double (`1152921504606847000` for `2**60`).
    Any other integer (`2**53 + 1`, `2**60 + 1`, `10**400`) is not the text of any double:
    reading it as one would round it, so it is refused rather than rounded.
    """
    try:
        number = float(value)
    except OverflowError:
        return None
    if int(number) == value:
        return number
    if abs(value) < _DIGITS_LIMIT and es_number(number) == str(value):
        return number
    return None


def _string(text: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("a lone surrogate is not text in JSON") from None
    return '"' + text.translate(_ESCAPES) + '"'


def _text_bytes(text: str) -> int:
    return len(text) if text.isascii() else len(text.encode("utf-8"))


def _check_key(key: object) -> str:
    if type(key) is not str:
        raise ValueError(f"a key of an object must be a string, got {type(key).__name__}")
    return key


def canonicalize(value: object, *, max_nodes: int = MAX_NODES, max_bytes: int = MAX_BYTES) -> str:
    """The canonical JSON text (RFC 8785) of a JSON value; `ValueError` if it is not one.

    Only exact `dict`, `list`, `str`, `int`, `float`, `bool` and `None` are JSON values here
    (a tuple, a set or a subclass is not), object keys must be strings, text must be well
    formed (no lone surrogate), a float must be finite, an integer must stand for a double
    (`double_of_int`) and nesting is at most 200 deep. Nothing is ever rounded silently.

    The walk is iterative, so no depth can exhaust the interpreter's stack, and it is bounded:
    at most `max_nodes` values (every container, scalar and object key counts, so a list of a
    million empty lists is refused as it is read, not after it is built) and `max_bytes` bytes
    of output, in UTF-8. A hostile value that is over either is a `ValueError` after at most
    that much work. (A container's size is checked before its members are pushed, which bounds the
    memory of the walk, not its outcome: without that check the budget is still refused, later.)
    """
    # Entries are a value with its depth, or a piece of output already decided (a string).
    stack: list[tuple[object, int] | str] = [(value, 1)]
    out: list[str] = []
    nodes = 0
    size = 0
    while stack:
        entry = stack.pop()
        if isinstance(entry, str):
            piece = entry
        else:
            item, depth = entry
            nodes += 1
            if nodes > max_nodes:
                raise ValueError(f"more than {max_nodes} values")
            if depth > _MAX_DEPTH:
                raise ValueError(f"nested deeper than {_MAX_DEPTH}")
            kind = type(item)
            if item is None:
                piece = "null"
            elif kind is bool:
                piece = "true" if item else "false"
            elif isinstance(item, str) and kind is str:
                piece = _string(item)
            elif isinstance(item, int) and kind is int:
                number = double_of_int(item)
                if number is None:
                    raise ValueError(
                        "an integer that is not an IEEE-754 double: JCS would round it"
                    )
                piece = es_number(number)
            elif isinstance(item, float) and kind is float:
                piece = es_number(item)
            elif isinstance(item, list) and kind is list:
                if nodes + len(item) > max_nodes:
                    raise ValueError(f"more than {max_nodes} values")
                piece = "["
                stack.append("]")
                for position in range(len(item) - 1, -1, -1):
                    stack.append((item[position], depth + 1))
                    if position:
                        stack.append(",")
            elif isinstance(item, dict) and kind is dict:
                if nodes + 2 * len(item) > max_nodes:
                    raise ValueError(f"more than {max_nodes} values")
                keys = [_check_key(key) for key in item]
                keys.sort(key=lambda key: key.encode("utf-16-be", "strict"))
                piece = "{"
                stack.append("}")
                for position in range(len(keys) - 1, -1, -1):
                    key = keys[position]
                    stack.append((item[key], depth + 1))
                    stack.append(_string(key) + ":")
                    if position:
                        stack.append(",")
                nodes += len(keys)  # the keys; the values count when they are visited
            else:
                raise ValueError(f"not a JSON value: {kind.__name__}")
        size += _text_bytes(piece)
        if size > max_bytes:
            raise ValueError(f"more than {max_bytes} bytes of output")
        out.append(piece)
    return "".join(out)
