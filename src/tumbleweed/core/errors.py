"""Domain exception hierarchy shared by every package.

Each package derives its own errors from `TumbleweedError`, so callers can catch "anything we
raised on purpose" without also swallowing bugs (`TypeError`, `KeyError`, ...).
"""

_EXCERPT_LIMIT = 64


class TumbleweedError(Exception):
    """Base class of every exception Tumbleweed code raises on purpose."""


class InvalidValueError(TumbleweedError, ValueError):
    """Raw text or a raw number failed validation as a domain type.

    A `ValueError` as well, so pydantic reports it as a validation error instead of crashing.
    """


def excerpt(value: object) -> str:
    """Return a short `repr` of untrusted input, safe to put in an error message or a log.

    Input is bounded only by whatever limit its source enforces, and error messages end up in
    logs and in chat, so they must not echo it whole.
    """
    shown = value[:_EXCERPT_LIMIT] if isinstance(value, str) else value
    text = repr(shown)
    if isinstance(value, str) and len(value) > _EXCERPT_LIMIT:
        return f"{text}... ({len(value)} chars)"
    return text
