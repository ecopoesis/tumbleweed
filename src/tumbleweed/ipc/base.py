"""The base classes of every message body: strict, frozen, closed."""

from pydantic import BaseModel, ConfigDict


class IpcMessage(BaseModel):
    """Base of every model that crosses the IPC boundary: strict, frozen, no extra fields.

    Floats must be finite: `NaN` and `Infinity` are not JSON, and a peer must not be able to
    smuggle one into a field that does arithmetic.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)


class Request(IpcMessage):
    """Base of request bodies: what a peer asks the other peer to do."""


class Response(IpcMessage):
    """Base of response bodies: what a peer answers a request with."""
