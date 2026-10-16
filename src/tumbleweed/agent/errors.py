"""Errors of the agent runtime.

Every `AgentError` carries a message that is safe to show in the chat as it is: fixed text, written
here or by the code that raised it, never a provider's own words (a provider, or a proxy in front
of it, could put anything in them, and an HTTP library's text can name headers). The reply that
fails shows `str(error)`. `kind` is the closed set the invocation log records, so a log line says
what happened without any text.
"""

import enum
import traceback
from typing import ClassVar

from tumbleweed.agent.llm import Usage
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


def describe_unexpected(error: BaseException) -> str:
    """Where an unexpected exception came from and its type, never its message.

    For the log: such a message can hold anything a server or a model said (the API key, if a
    hostile server put it there), so a bug is logged by type and place, not by `logger.exception`.
    """
    frames = traceback.extract_tb(error.__traceback__)
    where = f"{frames[-1].filename.rsplit('/', 1)[-1]}:{frames[-1].lineno}" if frames else "?"
    return f"{type(error).__name__} at {where}"


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
    """The model provider could not give an answer.

    `request_id` is the provider's, and `usage` what the provider had reported when the call
    failed, if they are known: the invocation log records both.
    """

    kind = ErrorKind.PROVIDER

    def __init__(
        self,
        message: str,
        *,
        request_id: ProviderRequestId | None = None,
        usage: Usage | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.usage = usage


class ProviderAuthError(ProviderError):
    kind = ErrorKind.AUTH


class ProviderNetworkError(ProviderError):
    kind = ErrorKind.NETWORK


class ProviderTimeoutError(ProviderError):
    kind = ErrorKind.TIMEOUT


class ProviderRateLimitedError(ProviderError):
    kind = ErrorKind.RATE_LIMITED

    def __init__(
        self,
        message: str,
        *,
        request_id: ProviderRequestId | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, request_id=request_id)
        self.retry_after = retry_after


class ProviderOverloadedError(ProviderError):
    kind = ErrorKind.OVERLOADED


class ProviderRefusedError(ProviderError):
    kind = ErrorKind.REFUSED


class ProviderRequestError(ProviderError):
    """The provider refused the request itself (a bad model name, a request too large)."""

    kind = ErrorKind.BAD_REQUEST
