"""The names a spec gives to things: state keys, handlers, and the code a handler calls.

Each kind of name is its own type, so a `StateKey` cannot be passed where a `HandlerName` is
expected. All of them are lowercase ASCII snake_case, at most `MAX_NAME_LENGTH` characters,
starting with a letter: no case or Unicode look-alikes, nothing that needs quoting in a log, and
a module or function name that cannot start with an underscore (so no dunder is ever called).
Like the other validated primitives (`tumbleweed.core.types`), they are `str`s at runtime:
`StateKey("a") == "a"`, and also `== HandlerName("a")`; only the type checker tells them apart.
"""

from typing import Final, final

from tumbleweed.core.types import PatternStr
from tumbleweed.ui.spec.limits import MAX_NAME_LENGTH

NAME_PATTERN: Final = f"[a-z][a-z0-9_]{{0,{MAX_NAME_LENGTH - 1}}}"
_EXPECTS: Final = (
    f"1-{MAX_NAME_LENGTH} characters from a-z 0-9 _, starting with a letter (lowercase snake_case)"
)


@final
class StateKey(PatternStr, pattern=NAME_PATTERN, expects=_EXPECTS):
    """The name of one state entry."""

    __slots__ = ()


@final
class HandlerName(PatternStr, pattern=NAME_PATTERN, expects=_EXPECTS):
    """The name of one handler; components refer to handlers by it."""

    __slots__ = ()


@final
class ModuleName(PatternStr, pattern=NAME_PATTERN, expects=_EXPECTS):
    """The name of one of the tool's code modules, which a `sandbox` handler calls into."""

    __slots__ = ()


@final
class FunctionName(PatternStr, pattern=NAME_PATTERN, expects=_EXPECTS):
    """The name of a function in a code module."""

    __slots__ = ()
