"""Errors of the agent runtime.

Every `AgentError` carries a message that is safe to show in the chat as it is: fixed text, written
here or by the code that raised it, never a provider's own words (a provider, or a proxy in front
of it, could put anything in them, and an HTTP library's text can name headers). The reply that
fails shows `str(error)`. `kind` is the closed set the invocation log records, so a log line says
what happened without any text.
"""

import enum
from typing import ClassVar

from tumbleweed.core.errors import TumbleweedError
from tumbleweed.core.types import ProviderRequestId


class ErrorKind(enum.StrEnum):
    """Why a call or a reply failed, as the invocation log records it."""

    NO_KEY = "no_key"
    CONFIG = "config"
    AUTH = "auth"
    NETWORK = "network"
    RATE_LIMITED = "rate_limited"
    OVERLOADED = "overloaded"
    TIMEOUT = "timeout"
    REFUSED = "refused"
    BAD_REQUEST = "bad_request"
    PROVIDER = "provider"
    LIMIT = "limit"
    BUSY = "busy"
    INTERNAL = "internal"


class AgentError(TumbleweedError):
    """Base of the agent's expected failures; `str(error)` is safe to show in chat."""

    kind: ClassVar[ErrorKind] = ErrorKind.INTERNAL


class ConfigError(AgentError):
    """The configuration or the key file cannot be used."""

    kind = ErrorKind.CONFIG


class MissingKeyError(AgentError):
    """No API key is configured."""

    kind = ErrorKind.NO_KEY


class AgentBusyError(AgentError):
    """A reply is already being produced, or the agent has been shut down."""

    kind = ErrorKind.BUSY


class LimitError(AgentError):
    """A per-turn limit (iterations, tokens, size, time) was reached (I9)."""

    kind = ErrorKind.LIMIT


class ReplyTimeoutError(LimitError):
    """The reply did not finish within the reply timeout."""

    kind = ErrorKind.TIMEOUT


class ProviderError(AgentError):
    """The model provider could not give an answer. `request_id` is the provider's, if known."""

    kind = ErrorKind.PROVIDER

    def __init__(self, message: str, *, request_id: ProviderRequestId | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


class ProviderAuthError(ProviderError):
    kind = ErrorKind.AUTH


class ProviderNetworkError(ProviderError):
    kind = ErrorKind.NETWORK


class ProviderTimeoutError(ProviderError):
    kind = ErrorKind.TIMEOUT


class ProviderRateLimitedError(ProviderError):
    kind = ErrorKind.RATE_LIMITED


class ProviderOverloadedError(ProviderError):
    kind = ErrorKind.OVERLOADED


class ProviderRefusedError(ProviderError):
    kind = ErrorKind.REFUSED


class ProviderRequestError(ProviderError):
    """The provider refused the request itself (a bad model name, a request too large)."""

    kind = ErrorKind.BAD_REQUEST
