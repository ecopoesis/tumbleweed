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
    """Return a short, safe description of untrusted input for an error message or a log.

    Input is bounded only by whatever limit its source enforces, and error messages end up in
    logs and in chat, so they must not echo it whole, nor trust it to describe itself. A `str`
    is shown by its `repr` (which escapes control and format characters) cut to 64 characters,
    with its length. A `bool`, `None` or an `int` of at most 64 bits is shown by its `repr`. Any
    other value, a subclass of `str` or `int` included, is shown by its type's name alone: its
    `repr` could be huge, raise, or lie.
    """
    kind = type(value)
    if isinstance(value, str) and kind is str:
        text = repr(value[:_EXCERPT_LIMIT])
        return f"{text}... ({len(value)} chars)" if len(value) > _EXCERPT_LIMIT else text
    if value is None or kind is bool:
        return repr(value)
    if isinstance(value, int) and kind is int:
        # str() of an int with more than 4300 digits raises ValueError: say how big it is.
        return repr(value) if value.bit_length() <= 64 else f"an int of {value.bit_length()} bits"
    return f"<{kind.__name__[:_EXCERPT_LIMIT]}>"
