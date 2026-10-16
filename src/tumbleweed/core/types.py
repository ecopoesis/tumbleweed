"""Domain primitives: the typed values (not entity IDs, see `ids`) that cross function and
process boundaries.

Every primitive validates at runtime and is a distinct type to the checker, so a function that
needs a `CanonicalPath` cannot be handed a `str`, and a `ByteCount` cannot be passed as a
`ToolVersion`. Where a value is a number or a string, the type subclasses `int` or `str` so it
can be used with code that only knows those (`len(data) <= limit`, an SDK's `model=` argument);
paths do not, because they must not be usable as file names without going through the I11 safe
open. Durations are plain `datetime.timedelta`.

Validation is strict: `ByteCount` takes `int`, not `"5"` or `True`. In pydantic models these
types accept their raw JSON form, and dump to it in JSON mode.
"""

import re
from os import PathLike, fspath
from pathlib import Path, PurePosixPath
from typing import ClassVar, NoReturn, Self, final

from pydantic import GetCoreSchemaHandler, JsonValue
from pydantic_core import core_schema

from tumbleweed.core._scalar import FrozenText, scalar_schema
from tumbleweed.core.errors import InvalidValueError, excerpt

_MAX_PATH_BYTES = 4096  # Linux PATH_MAX
_MAX_NAME_BYTES = 255  # Linux NAME_MAX


class _BoundedInt(int):
    """An `int` with a runtime lower bound, set per subclass: `class X(_BoundedInt, minimum=0)`."""

    _minimum: ClassVar[int]

    def __init_subclass__(cls, *, minimum: int) -> None:
        super().__init_subclass__()
        cls._minimum = minimum

    def __new__(cls, value: int) -> Self:
        if value < cls._minimum:
            raise InvalidValueError(f"{cls.__name__} must be at least {cls._minimum}, got {value}")
        return super().__new__(cls, value)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: object, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return scalar_schema(
            cls,
            int,
            core_schema.int_schema(strict=True, ge=cls._minimum),
            cls,
            core_schema.simple_ser_schema("int"),
        )


class _PatternStr(str):
    """A `str` that must match a pattern entirely, set per subclass.

    `class X(_PatternStr, pattern=r"[a-z]+", expects="lowercase letters")`
    """

    _pattern: ClassVar[re.Pattern[str]]
    _expects: ClassVar[str]

    def __init_subclass__(cls, *, pattern: str, expects: str) -> None:
        super().__init_subclass__()
        cls._pattern = re.compile(pattern)
        cls._expects = expects

    def __new__(cls, value: str) -> Self:
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


@final
class ToolVersion(_BoundedInt, minimum=1):
    """The version of a saved tool: 1 for the first save, then 2, 3, ..."""


@final
class ContentHash(
    _PatternStr, pattern=r"[0-9a-f]{64}", expects="64 lowercase hex digits (a SHA-256 digest)"
):
    """The SHA-256 digest of a tool package's canonical bytes, as lowercase hex."""


@final
class ModelName(
    _PatternStr,
    pattern=r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}",
    expects="1-128 characters from A-Z a-z 0-9 . _ : / @ + -, starting with a letter or digit",
):
    """The name of an LLM, e.g. `claude-sonnet-5-5`, as the provider (or a proxy) spells it."""


@final
class ProviderRequestId(
    _PatternStr,
    pattern=r"[\x21-\x7e]{1,256}",
    expects="1-256 printable ASCII characters without spaces",
):
    """An opaque request ID issued by an LLM provider. Not one of our IDs, never parsed.

    The character set keeps it harmless to write into logs.
    """


@final
class WorkspaceRelPath(FrozenText):
    """A path relative to a workspace root, validated lexically on construction (I11).

    Rejected: empty, absolute, containing `..`, `.` or empty components (`a//b`, `a/`),
    control characters (NUL included), text that is not valid UTF-8, and anything over Linux's
    PATH_MAX (4096) or NAME_MAX (255). Symlinks are not visible to this check: trusted code
    must still open the path component by component without following them.
    """

    @classmethod
    def _validate(cls, text: str) -> None:
        if not text:
            raise InvalidValueError("WorkspaceRelPath must not be empty")
        if text.startswith("/"):
            raise InvalidValueError(f"WorkspaceRelPath must be relative, got {excerpt(text)}")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
            raise InvalidValueError(
                f"WorkspaceRelPath must not contain control characters, got {excerpt(text)}"
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


@final
class CanonicalPath(FrozenText):
    """An absolute path with every symlink resolved and `.`/`..` removed.

    The only way to get one is `CanonicalPath.resolve`, which asks the filesystem; there is no
    constructor from text, and no pydantic validation from text either. A `CanonicalPath` read
    back from storage or received over IPC would be a claim nobody checked, so those places
    hold a `str` and call `resolve` again (ARCHITECTURE section 7: decide on the canonical path,
    then verify its identity at run time). Models may still hold one and dump it, e.g. for the
    audit log.

    Canonical means symlinks and dots only: case and Unicode normalisation are not applied, and
    a path that does not exist yet keeps its remaining components as written.
    """

    def __init__(self) -> None:
        raise TypeError("CanonicalPath is built by CanonicalPath.resolve(), not called")

    @classmethod
    def resolve(cls, path: str | PathLike[str]) -> Self:
        """Canonicalise an absolute path. A relative one is refused, not resolved against cwd."""
        text = fspath(path)
        if "\0" in text:
            raise InvalidValueError(f"path contains a NUL byte: {excerpt(text)}")
        if not PurePosixPath(text).is_absolute():
            raise InvalidValueError(f"path must be absolute, got {excerpt(text)}")
        try:
            return cls._adopt(str(Path(text).resolve()))
        except (OSError, RuntimeError) as exc:  # RuntimeError: symlink loop, before Python 3.13
            raise InvalidValueError(f"cannot canonicalise {excerpt(text)}: {exc}") from exc

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: object, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        def refuse(value: object) -> NoReturn:
            raise ValueError("a CanonicalPath cannot be parsed, call CanonicalPath.resolve()")

        return core_schema.json_or_python_schema(
            json_schema=core_schema.no_info_plain_validator_function(refuse),
            python_schema=core_schema.is_instance_schema(cls),
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
