"""The trusted side of the handler shim's protocol (S20, D27): the command and payload of a run,
and how its answer is read.

This lives next to the shim (`tumbleweed.sandbox.handler_shim`) and not in the agent so that the
broker can build the same run from code it stores itself (S24, I8) without importing the agent.
Nothing here compiles, imports or evaluates the code it carries: it is text in a payload file.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Final, Literal, final

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from tumbleweed.sandbox.policy import MAX_PAYLOAD_BYTES, PAYLOAD_TARGET

INTERPRETER: Final = "python3"
"""Found on the sandbox's own `PATH`, which the broker sets from the runtime (a Nix runtime has no
`/usr/bin/python3`)."""
MAX_DOCUMENT_BYTES: Final = MAX_PAYLOAD_BYTES - 1024

type ShimError = Literal[
    "bad_payload", "no_module", "no_function", "raised", "bad_result", "too_large"
]


@cache
def shim_source() -> str:
    """The text of the in-sandbox shim, read as data: it is never imported here."""
    return resources.files("tumbleweed.sandbox").joinpath("handler_shim.py").read_text("utf-8")


def handler_command() -> tuple[str, ...]:
    """The argv of a handler run: the shim as text, and the payload file it reads."""
    return (INTERPRETER, "-I", "-c", shim_source(), PAYLOAD_TARGET)


def script_command() -> tuple[str, ...]:
    """The argv of `run_code`: the interpreter, isolated, on the payload file (the source)."""
    return (INTERPRETER, "-I", PAYLOAD_TARGET)


def handler_payload(
    module: str, function: str, inputs: Mapping[str, JsonValue], code: Mapping[str, str]
) -> str | None:
    """The JSON document of a handler run, or None if it is over `MAX_DOCUMENT_BYTES` or holds
    text that cannot be encoded (a lone surrogate)."""
    document = json.dumps(
        {"module": module, "function": function, "inputs": dict(inputs), "code": dict(code)},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    try:
        size = len(document.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    return document if size <= MAX_DOCUMENT_BYTES else None


@final
class _Ok(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    ok: Literal[True]
    outputs: dict[str, JsonValue]


@final
class _Bad(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    ok: Literal[False]
    error: ShimError


@final
@dataclass(frozen=True)
class ShimOutcome:
    """What the shim said: outputs to validate, or one of its closed words, or neither (the
    answer was not the protocol)."""

    outputs: Mapping[str, JsonValue] | None = None
    error: ShimError | None = None


def read_shim_line(stdout: bytes, exit_code: int | None) -> ShimOutcome | None:
    """Parse the shim's one line; None if it is not the protocol (including a success line that
    comes with a failing exit status)."""
    try:
        if exit_code == 0:
            return ShimOutcome(outputs=_Ok.model_validate_json(stdout).outputs)
        return ShimOutcome(error=_Bad.model_validate_json(stdout).error)
    except ValidationError:
        return None
