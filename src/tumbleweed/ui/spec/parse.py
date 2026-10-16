"""The entry points: JSON text or parsed JSON in, a `UiSpec` or a bounded `SpecError` out.

`parse_ui_spec` is for text (a file, a tool package, an IPC frame) and `validate_ui_spec` for
a spec already parsed from JSON (the input of a model tool call). Both return a validated
`UiSpec` or raise `SpecError`, whose message lists the problems without echoing the input.

`parse_ui_spec` adds what pydantic's JSON parser does not do:

- a size limit (`MAX_SPEC_BYTES`), checked before anything is parsed;
- the text must be UTF-8 (no byte-order mark) and valid JSON;
- an object that repeats a key is refused. Parsers keep the last value and drop the others
  without a word, so `{"root": A, "root": B}` would mean B to us and A to a reader of the text;
- `NaN` and `Infinity` are refused (the models would refuse them anyway; this says why).
`UiSpec.model_validate_json` alone does none of these, so use `parse_ui_spec` for any text that
did not come from `model_dump_json`.
"""

import json

from pydantic import ValidationError

from tumbleweed.ui.spec.errors import SpecError, SpecTooLargeError, problems_of
from tumbleweed.ui.spec.limits import MAX_SPEC_BYTES
from tumbleweed.ui.spec.spec import UiSpec


def _no_repeated_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        repeated = next(key for key in keys if keys.count(key) > 1)
        raise ValueError(f"an object repeats the key {repeated[:40]!r}")
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


def validate_ui_spec(data: object) -> UiSpec:
    """Validate a spec that is already parsed JSON (dicts, lists, strings, numbers)."""
    try:
        return UiSpec.model_validate(data)
    except ValidationError as error:
        raise SpecError(problems_of(error)) from None
