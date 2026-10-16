"""Plumbing shared by the typed scalars in `ids` and `types`.

Private to `tumbleweed.core`: the public vocabulary is `ids` and `types`.
"""

from collections.abc import Callable
from typing import NoReturn, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema


def scalar_schema[Raw](
    cls: type,
    raw: core_schema.CoreSchema,
    build: Callable[[Raw], object],
    serialization: core_schema.SerSchema,
) -> core_schema.CoreSchema:
    """Build the pydantic schema for a scalar type that is a validated wrapper of `Raw`.

    The raw value (`str` or `int`) is validated strictly, so `"5"` is never a byte count and
    `True` is never a version, then handed to `build`. An instance of `cls` is accepted as is;
    an instance of any other type is not. Dumped to JSON the value is its raw form.
    """
    from_raw = core_schema.no_info_after_validator_function(build, raw)
    return core_schema.json_or_python_schema(
        json_schema=from_raw,
        python_schema=core_schema.union_schema([core_schema.is_instance_schema(cls), from_raw]),
        serialization=serialization,
    )


class FrozenText:
    """Immutable wrapper around one validated string.

    Equality, hashing and ordering are per concrete class: two values of different types are
    never equal, even when their text is. Subclasses implement `_validate` and `_raw_schema`.
    Instances cannot be pickled (pickle would rebuild one without validating it); `copy` and
    `deepcopy` return the instance itself.
    """

    _text: str

    def __init__(self, text: str) -> None:
        type(self)._validate(text)
        object.__setattr__(self, "_text", text)

    @classmethod
    def _adopt(cls, text: str) -> Self:
        """Wrap text the caller has already validated or produced itself."""
        instance = object.__new__(cls)
        object.__setattr__(instance, "_text", text)
        return instance

    @classmethod
    def _validate(cls, text: str) -> None:
        """Raise `InvalidValueError` unless `text` is a valid value of this type."""
        raise NotImplementedError

    @classmethod
    def _raw_schema(cls) -> core_schema.CoreSchema:
        """The pydantic schema of the serialized text (used for validation and JSON Schema)."""
        raise NotImplementedError

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: object, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return scalar_schema(cls, cls._raw_schema(), cls, core_schema.to_string_ser_schema())

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FrozenText) or type(other) is not type(self):
            return NotImplemented
        return self._text == other._text

    # Ordering is spelled out rather than derived with functools.total_ordering, which mypy
    # cannot follow for `Self`-typed operands. A mix of types is a TypeError, as for `1 < "a"`.
    def __lt__(self, other: Self) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._text < other._text

    def __le__(self, other: Self) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._text <= other._text

    def __gt__(self, other: Self) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._text > other._text

    def __ge__(self, other: Self) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._text >= other._text

    def __hash__(self) -> int:
        return hash((type(self), self._text))

    def __str__(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._text!r})"

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        return self

    def __reduce__(self) -> NoReturn:
        raise TypeError(f"{type(self).__name__} cannot be pickled; serialize it with str()")
