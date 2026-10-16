"""The entry points: JSON text or parsed JSON in, a `UiSpec` or a bounded `SpecError` out.

`parse_ui_spec` is for text (a file, a tool package, an IPC frame) and `validate_ui_spec` for
a spec already parsed from JSON (the input of a model tool call). Both return a validated
`UiSpec` or raise `SpecError`, whose message lists the problems without echoing the input, and
both go through the same checks, because `validate_ui_spec` writes the data out as JSON text and
hands that to `parse_ui_spec`:

- a size limit (`MAX_SPEC_BYTES`), checked before anything is parsed (and, for parsed data,
  while it is written out, which stops at the limit);
- the text must be UTF-8 (no byte-order mark) and valid JSON;
- an object that repeats a key is refused. Parsers keep the last value and drop the others
  without a word, so `{"root": A, "root": B}` would mean B to us and A to a reader of the text;
- an object of more than `MAX_OBJECT_MEMBERS` members is refused while parsing, so that no
  validator is asked to report on thousands of unknown fields;
- `NaN` and `Infinity` are refused (the models would refuse them anyway; this says why).

Parsed data must be what `json.loads` would have produced: `dict` with `str` keys, `list`,
`str`, `int`, `float`, `bool` and `None`, exactly. A tuple, a set, `bytes`, an enum member, a
subclass of any of these, or a key that is not a string is refused, not converted: `json.dumps`
would quietly write `{1: "a"}` as `{"1": "a"}` and a tuple as a list, and the data would then
mean something other than what the caller holds.

`UiSpec.model_validate_json` and `UiSpec.model_validate` do none of this: no size limit, no
repeated-key check. Use them only on text that came from `model_dump_json`, never on input from
the model, a file or another process.
"""

import json
from typing import Final

from pydantic import ValidationError

from tumbleweed.ui.spec.errors import SpecError, SpecTooLargeError, problems_of
from tumbleweed.ui.spec.limits import MAX_OBJECT_MEMBERS, MAX_SPEC_BYTES
from tumbleweed.ui.spec.spec import UiSpec

_MAX_NESTING: Final = 200  # the depth that pydantic's JSON parser accepts
_SCALARS: Final = frozenset({str, int, float, bool, type(None)})


def _no_repeated_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"an object repeats the key {key[:40]!r}")
        seen.add(key)
    return dict(pairs)


def _object_of(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """The `object_pairs_hook`: no object wider than any model or value allows, no repeated key."""
    if len(pairs) > MAX_OBJECT_MEMBERS:
        raise ValueError(f"an object has more than {MAX_OBJECT_MEMBERS} members")
    return _no_repeated_keys(pairs)


def _no_constants(name: str) -> object:
    raise ValueError(f"{name} is not valid JSON")


def parse_ui_spec(text: str | bytes) -> UiSpec:
    """Parse and validate the JSON text of a spec. Raises `SpecError` (never anything else)."""
    if type(text) is bytes:
        data = text
    elif type(text) is str:
        if len(text) > MAX_SPEC_BYTES:  # a character is at least one byte
            raise SpecTooLargeError([f"the spec is over {MAX_SPEC_BYTES} bytes"])
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError:
            raise SpecError(["the spec is not valid UTF-8 (it has a lone surrogate)"]) from None
    else:
        raise SpecError([f"expected str or bytes, got {type(text).__name__}"])
    if len(data) > MAX_SPEC_BYTES:
        raise SpecTooLargeError([f"the spec is over {MAX_SPEC_BYTES} bytes"])
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SpecError(["the spec is not valid UTF-8"]) from None
    try:
        json.loads(decoded, object_pairs_hook=_object_of, parse_constant=_no_constants)
    except RecursionError:
        raise SpecError(["the spec is nested too deeply"]) from None
    except ValueError as error:  # includes json.JSONDecodeError
        raise SpecError([f"not valid JSON: {str(error)[:200]}"]) from None
    try:
        return UiSpec.model_validate_json(decoded)
    except ValidationError as error:
        raise SpecError(problems_of(error)) from None


def _check_is_json(data: object) -> None:
    """Refuse what `json.loads` could never have produced, without recursion and boundedly.

    Only exact `dict` (with `str` keys), `list`, `str`, `int`, `float`, `bool` and `None` pass.
    Every value is at least a byte of the text it would be written as, so a structure with more
    than `MAX_SPEC_BYTES` of them is too large and the walk stops there, without unpacking a
    list or a dict that cannot fit.
    """
    nodes = 0
    stack: list[tuple[object, int]] = [(data, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > _MAX_NESTING:
            raise SpecError(["the spec is nested too deeply"])
        kind = type(value)
        if isinstance(value, dict) and kind is dict:
            if nodes + len(stack) + len(value) > MAX_SPEC_BYTES:
                raise SpecTooLargeError([f"the spec is over {MAX_SPEC_BYTES} bytes"])
            for key, child in value.items():
                if type(key) is not str:
                    raise SpecError(
                        [f"not JSON data: an object key is a {type(key).__name__[:40]}, not a str"]
                    )
                stack.append((child, depth + 1))
        elif isinstance(value, list) and kind is list:
            if nodes + len(stack) + len(value) > MAX_SPEC_BYTES:
                raise SpecTooLargeError([f"the spec is over {MAX_SPEC_BYTES} bytes"])
            stack.extend((child, depth + 1) for child in value)
        elif kind not in _SCALARS:
            raise SpecError([f"not JSON data: {kind.__name__[:40]} is not a JSON type"])


def _written_out(data: object) -> str:
    """The JSON text of parsed JSON, stopping as soon as it is over `MAX_SPEC_BYTES`."""
    _check_is_json(data)
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False)
    chunks: list[str] = []
    size = 0
    try:
        for chunk in encoder.iterencode(data):
            size += len(chunk)  # a character is at least one byte; parse_ui_spec counts bytes
            if size > MAX_SPEC_BYTES:
                raise SpecTooLargeError([f"the spec is over {MAX_SPEC_BYTES} bytes"])
            chunks.append(chunk)
    except RecursionError:
        raise SpecError(["the spec is nested too deeply"]) from None
    except (TypeError, ValueError) as error:  # NaN or infinity, an integer of over 4300 digits
        raise SpecError([f"not JSON data: {str(error)[:200]}"]) from None
    return "".join(chunks)


def validate_ui_spec(data: object) -> UiSpec:
    """Validate a spec that is already parsed JSON (dicts, lists, strings, numbers).

    Exactly as strict as `parse_ui_spec` of the same data written out as JSON: the same size
    limit, the same refusals. Data that is not what `json.loads` gives (a tuple, a set, bytes, an
    enum member, a model instance, a key that is not a `str`, a cyclic structure, a value that is
    not finite) is a `SpecError`, never converted.
    """
    return parse_ui_spec(_written_out(data))
