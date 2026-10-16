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
- `NaN` and `Infinity` are refused (the models would refuse them anyway; this says why).

`UiSpec.model_validate_json` and `UiSpec.model_validate` do none of this: no size limit, no
repeated-key check. Use them only on text that came from `model_dump_json`, never on input from
the model, a file or another process.
"""

import json

from pydantic import ValidationError

from tumbleweed.ui.spec.errors import SpecError, SpecTooLargeError, problems_of
from tumbleweed.ui.spec.limits import MAX_SPEC_BYTES
from tumbleweed.ui.spec.spec import UiSpec


def _no_repeated_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"an object repeats the key {key[:40]!r}")
        seen.add(key)
    return dict(pairs)


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
        json.loads(decoded, object_pairs_hook=_no_repeated_keys, parse_constant=_no_constants)
    except RecursionError:
        raise SpecError(["the spec is nested too deeply"]) from None
    except ValueError as error:  # includes json.JSONDecodeError
        raise SpecError([f"not valid JSON: {str(error)[:200]}"]) from None
    try:
        return UiSpec.model_validate_json(decoded)
    except ValidationError as error:
        raise SpecError(problems_of(error)) from None


def _written_out(data: object) -> str:
    """The JSON text of parsed JSON, stopping as soon as it is over `MAX_SPEC_BYTES`."""
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
    except (TypeError, ValueError) as error:  # not JSON: bytes, a set, NaN, a cycle, a huge int
        raise SpecError([f"not JSON data: {str(error)[:200]}"]) from None
    return "".join(chunks)


def validate_ui_spec(data: object) -> UiSpec:
    """Validate a spec that is already parsed JSON (dicts, lists, strings, numbers).

    Exactly as strict as `parse_ui_spec` of the same data written out as JSON: the same size
    limit, the same refusals. An object that is not JSON data (bytes, a set, a model instance, a
    cyclic structure, a value that is not finite) is a `SpecError`.
    """
    return parse_ui_spec(_written_out(data))
