"""RFC 8785 JSON Canonicalization Scheme, as the tool store (S23) will write it.

Just enough to test that a spec survives canonicalisation: object members sorted by the UTF-16
code units of their names, no insignificant whitespace, strings escaped as `JSON.stringify` does,
and numbers as ECMAScript's `Number.prototype.toString`. Every number is an IEEE-754 double, so
an integer is first converted to one; this refuses an integer that is not exactly a double,
because the real canonicaliser would round it silently.
"""

import math
import struct
from decimal import Decimal

from tumbleweed.core.types import JsonValue

_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def es_number(value: float) -> str:
    """`Number.prototype.toString` of a finite double (RFC 8785 section 3.2.2.3)."""
    if not math.isfinite(value):
        raise ValueError("JSON has no NaN or infinity")
    if value == 0:
        return "0"  # also for -0
    if value < 0:
        return "-" + es_number(-value)
    sign, digit_tuple, exponent = Decimal(repr(value)).as_tuple()
    assert sign == 0
    assert isinstance(exponent, int)
    digits = "".join(map(str, digit_tuple)).rstrip("0")
    exponent += len(digit_tuple) - len(digits)  # the zeros dropped above are part of the exponent
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


def _string(text: str) -> str:
    out = ['"']
    for char in text:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def canonicalize(value: JsonValue) -> str:
    """The canonical JSON text of `value`."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, int):
        number = float(value)
        if int(number) != value:
            raise ValueError(f"{value} is not an IEEE-754 double: JCS would round it")
        return es_number(number)
    if isinstance(value, float):
        return es_number(value)
    if isinstance(value, list):
        return "[" + ",".join(canonicalize(item) for item in value) + "]"
    members = sorted(value.items(), key=lambda member: member[0].encode("utf-16-be"))
    return "{" + ",".join(f"{_string(key)}:{canonicalize(item)}" for key, item in members) + "}"


def double_from_bits(bits: int) -> float:
    """The double with this IEEE-754 bit pattern (the way RFC 8785 appendix B lists them)."""
    (value,) = struct.unpack(">d", struct.pack(">Q", bits))
    assert isinstance(value, float)
    return value
