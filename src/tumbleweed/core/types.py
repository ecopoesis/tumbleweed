"""Domain primitives: the typed values (not entity IDs, see `ids`) that cross function and
process boundaries.

Every primitive validates at runtime and is a distinct type to the checker, so a function that
needs a `CanonicalPath` cannot be handed a `str`, and a `ByteCount` cannot be passed as a
`ToolVersion`. Where a value is a number or a string, the type subclasses `int` or `str` so it
can be used with code that only knows those (`len(data) <= limit`, an SDK's `model=` argument);
paths do not, because they must not be usable as file names without going through the I11 safe
open. Durations are plain `datetime.timedelta`.

Validation is strict: `ByteCount` takes an `int`, not `"5"`, `True`, a float or a `Decimal`,
and an int outside 0..2**63-1 (for `ToolVersion`: 1..2**63-1) is refused. In pydantic models
these types accept their raw JSON form, and dump to it in JSON mode.

Two limits of building the numeric and string primitives on `int` and `str`:

- They are distinct types to the type checker, not at runtime: `ByteCount(5) == ToolVersion(5)`
  is true, as is `ContentHash(h) == str(h)`. Entity IDs, which are not ints or strings, compare
  unequal across types.
- A `dict[ByteCount, ...]` can be dumped to JSON (keys become strings) but not validated back:
  the strict int schema refuses the string key `"5"`, and loosening it would also let `"5"` in
  as a value. Use a list of pairs, or `str` keys converted at the edge.
"""

import re
import stat
import unicodedata
from os import PathLike, fspath
from pathlib import Path, PurePosixPath
from typing import ClassVar, NoReturn, Self, final

from pydantic import GetCoreSchemaHandler, JsonValue
from pydantic_core import core_schema

from tumbleweed.core._scalar import FrozenText, check_subclass, exact_instance, scalar_schema
from tumbleweed.core.errors import InvalidValueError, excerpt

_INT64_MAX = 2**63 - 1
_MAX_PATH_BYTES = 4096  # Linux PATH_MAX
_MAX_NAME_BYTES = 255  # Linux NAME_MAX
# Unicode categories no workspace path may contain: control characters (C0, DEL and C1, so NUL,
# newline, U+009B CSI, U+0085 NEL), format characters (U+202E right-to-left override, zero-width
# characters, byte-order marks) and the line and paragraph separators U+2028 and U+2029. They
# make a name look like something else, or end a line, in a log, a terminal or the UI.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _describe_int(value: int) -> str:
    # str() of an int with more than 4300 digits raises ValueError, and error messages must be
    # short anyway: say how big it is instead.
    return str(value) if value.bit_length() <= 64 else f"an int of {value.bit_length()} bits"


class _BoundedInt(int):
    """An `int` bounded at runtime, set per subclass: `class X(_BoundedInt, minimum=0)`.

    The upper bound defaults to the largest int64, the largest integer SQLite stores. Only an
    `int` is accepted: not a `bool`, a `float`, a `Decimal` or a string.
    """

    __slots__ = ()
    _minimum: ClassVar[int]
    _maximum: ClassVar[int]

    def __init_subclass__(cls, *, minimum: int, maximum: int = _INT64_MAX) -> None:
        super().__init_subclass__()
        check_subclass(cls)
        cls._minimum = minimum
        cls._maximum = maximum

    def __new__(cls, value: int) -> Self:
        raw: object = value  # the annotation is for the checker; callers can still pass anything
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise InvalidValueError(f"{cls.__name__} must be an int, got {type(raw).__name__}")
        if not cls._minimum <= value <= cls._maximum:
            raise InvalidValueError(
                f"{cls.__name__} must be from {cls._minimum} to {cls._maximum}, "
                f"got {_describe_int(value)}"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: object, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return scalar_schema(
            cls,
            int,
            core_schema.int_schema(strict=True, ge=cls._minimum, le=cls._maximum),
            cls,
            core_schema.simple_ser_schema("int"),
        )


class _PatternStr(str):
    """A `str` that must match a pattern entirely, set per subclass.

    `class X(_PatternStr, pattern=r"[a-z]+", expects="lowercase letters")`
    """

    __slots__ = ()
    _pattern: ClassVar[re.Pattern[str]]
    _expects: ClassVar[str]

    def __init_subclass__(cls, *, pattern: str, expects: str) -> None:
        super().__init_subclass__()
        check_subclass(cls)
        cls._pattern = re.compile(pattern)
        cls._expects = expects

    def __new__(cls, value: str) -> Self:
        raw: object = value  # the annotation is for the checker; callers can still pass anything
        if not isinstance(raw, str):
            raise InvalidValueError(f"{cls.__name__} must be a str, got {type(raw).__name__}")
        if cls._pattern.fullmatch(value) is None:
            raise InvalidValueError(
                f"invalid {cls.__name__}: expected {cls._expects}, got {excerpt(value)}"
            )
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: object, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return scalar_schema(
            cls,
            str,
            core_schema.str_schema(strict=True, pattern=f"^(?:{cls._pattern.pattern})$"),
            cls,
            core_schema.simple_ser_schema("str"),
        )


@final
class ByteCount(_BoundedInt, minimum=0):
    """A number of bytes: never negative."""

    __slots__ = ()


@final
class ToolVersion(_BoundedInt, minimum=1):
    """The version of a saved tool: 1 for the first save, then 2, 3, ..."""

    __slots__ = ()


@final
class ContentHash(
    _PatternStr, pattern=r"[0-9a-f]{64}", expects="64 lowercase hex digits (a SHA-256 digest)"
):
    """The SHA-256 digest of a tool package's canonical bytes, as lowercase hex."""

    __slots__ = ()


@final
class ModelName(
    _PatternStr,
    pattern=r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}",
    expects="1-128 characters from A-Z a-z 0-9 . _ : / @ + -, starting with a letter or digit",
):
    """The name of an LLM, e.g. `claude-sonnet-5-5`, as the provider (or a proxy) spells it."""

    __slots__ = ()


@final
class ProviderRequestId(
    _PatternStr,
    pattern=r"[\x21-\x7e]{1,256}",
    expects="1-256 printable ASCII characters without spaces",
):
    """An opaque request ID issued by an LLM provider. Not one of our IDs, never parsed.

    The character set keeps it harmless to write into logs.
    """

    __slots__ = ()


@final
class WorkspaceRelPath(FrozenText):
    """A path relative to a workspace root, validated lexically on construction (I11).

    Rejected: empty, absolute, containing `..`, `.` or empty components (`a//b`, `a/`), any
    character of Unicode category Cc (NUL, C0 and C1 controls such as U+009B), Cf (format
    characters such as U+202E, U+200B, U+FEFF), Zl or Zp (U+2028, U+2029), text that is not
    valid UTF-8, and anything over Linux's PATH_MAX (4096) or NAME_MAX (255). Symlinks are not
    visible to this check: trusted code must still open the path component by component
    without following them.
    """

    __slots__ = ()

    @classmethod
    def _validate(cls, text: str) -> None:
        if not text:
            raise InvalidValueError("WorkspaceRelPath must not be empty")
        if text.startswith("/"):
            raise InvalidValueError(f"WorkspaceRelPath must be relative, got {excerpt(text)}")
        if any(unicodedata.category(char) in _FORBIDDEN_CATEGORIES for char in text):
            raise InvalidValueError(
                "WorkspaceRelPath must not contain control, format or line-separator "
                f"characters, got {excerpt(text)}"
            )
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidValueError(
                f"WorkspaceRelPath must be valid UTF-8, got {excerpt(text)}"
            ) from None
        if len(encoded) > _MAX_PATH_BYTES:
            raise InvalidValueError(f"WorkspaceRelPath is over {_MAX_PATH_BYTES} bytes")
        for part in text.split("/"):
            if part in {"", ".", ".."}:
                raise InvalidValueError(
                    "WorkspaceRelPath must not contain empty, '.' or '..' components, "
                    f"got {excerpt(text)}"
                )
            if len(part.encode("utf-8")) > _MAX_NAME_BYTES:
                raise InvalidValueError(
                    f"WorkspaceRelPath component is over {_MAX_NAME_BYTES} bytes: {excerpt(part)}"
                )

    @classmethod
    def _raw_schema(cls) -> core_schema.CoreSchema:
        return core_schema.str_schema(strict=True, min_length=1, max_length=_MAX_PATH_BYTES)


def _reason(exc: Exception) -> str:
    """What went wrong, without the path that `str(exc)` would repeat in full."""
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return type(exc).__name__


def _require_no_symlinks(resolved: str, original: str) -> None:
    """Refuse `resolved` if any component that exists is still a symlink, or cannot be inspected.

    `Path.resolve` is not trusted to have resolved everything: on Python 3.12 it quietly
    skips a component it may not look at (EACCES on the parent), and from 3.13 it returns
    symlink loops unresolved. So walk the result with lstat. A component that does not exist
    ends the walk: nothing below it can be a symlink, and the rest is only text.
    """
    current = Path("/")
    for part in PurePosixPath(resolved).parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            return
        except (OSError, ValueError) as exc:
            raise InvalidValueError(
                f"cannot canonicalise {excerpt(original)}: cannot inspect "
                f"{excerpt(part)}: {_reason(exc)}"
            ) from None
        if stat.S_ISLNK(mode):
            raise InvalidValueError(
                f"cannot canonicalise {excerpt(original)}: {excerpt(part)} is a symlink "
                "that could not be resolved (a loop, or a directory that cannot be searched)"
            )


@final
class CanonicalPath(FrozenText):
    """An absolute path with every symlink resolved and `.`/`..` removed.

    The only way to get one is `CanonicalPath.resolve`, which asks the filesystem and then
    checks that no symlink is left; there is no constructor from text, and no pydantic
    validation from text either. A `CanonicalPath` read back from storage or received over IPC
    would be a claim nobody checked. So storage and IPC hold the text, and `resolve` is for making
    a new decision about a path. A decision already made is used by opening the stored path
    component by component with `O_NOFOLLOW` and verifying its identity, never by resolving it
    again (ARCHITECTURE section 7; the typed stored-path record is S16's). Models may hold a
    `CanonicalPath` and dump it, e.g. for the audit log.

    Canonical means symlinks and dots only: case and Unicode normalisation are not applied, and
    a path that does not exist yet keeps its remaining components as written.
    """

    __slots__ = ()

    def __init__(self) -> None:
        raise TypeError("CanonicalPath is built by CanonicalPath.resolve(), not called")

    @classmethod
    def resolve(cls, path: str | PathLike[str]) -> Self:
        """Canonicalise an absolute path. A relative one is refused, not resolved against cwd.

        Raises `InvalidValueError` for a relative path, a NUL byte or lone surrogate, a symlink
        loop, and a path whose existing components cannot all be inspected (EACCES, ...).
        """
        text = fspath(path)
        if "\0" in text:
            raise InvalidValueError(f"path contains a NUL byte: {excerpt(text)}")
        if not PurePosixPath(text).is_absolute():
            raise InvalidValueError(f"path must be absolute, got {excerpt(text)}")
        try:
            resolved = str(Path(text).resolve())
        # RuntimeError: a symlink loop before Python 3.13. ValueError: includes the
        # UnicodeEncodeError for a lone surrogate, which no filesystem call can take.
        except (OSError, RuntimeError, ValueError) as exc:
            raise InvalidValueError(
                f"cannot canonicalise {excerpt(text)}: {_reason(exc)}"
            ) from None
        _require_no_symlinks(resolved, text)
        return cls._adopt(resolved)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: object, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        def refuse(value: object) -> NoReturn:
            raise ValueError("a CanonicalPath cannot be parsed, call CanonicalPath.resolve()")

        return core_schema.json_or_python_schema(
            json_schema=core_schema.no_info_plain_validator_function(refuse),
            python_schema=core_schema.no_info_plain_validator_function(exact_instance(cls)),
            serialization=core_schema.to_string_ser_schema(),
        )


__all__ = [
    "ByteCount",
    "CanonicalPath",
    "ContentHash",
    "JsonValue",
    "ModelName",
    "ProviderRequestId",
    "ToolVersion",
    "WorkspaceRelPath",
]
