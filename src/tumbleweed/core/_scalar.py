"""Plumbing shared by the typed scalars in `ids` and `types`.

Private to `tumbleweed.core`: the public vocabulary is `ids` and `types`.
"""

from collections.abc import Callable
from typing import NoReturn, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

from tumbleweed.core.errors import InvalidValueError


def check_subclass(cls: type) -> None:
    """Enforce at runtime what mypy enforces statically, for every scalar class.

    No subclass of a `@final` class: a subclass could override the constructor or `__eq__` and
    forge a validated value. And `__slots__` must be declared (`__slots__ = ()` is enough), so
    that instances have no `__dict__` through which a validated value could be rewritten.
    """
    for base in cls.__bases__:
        if base.__dict__.get("__final__", False):
            raise TypeError(f"{cls.__name__} cannot subclass {base.__name__}: it is final")
    if "__slots__" not in cls.__dict__:
        raise TypeError(f"{cls.__name__} must declare __slots__ (`__slots__ = ()` will do)")


def exact_instance[T](cls: type[T]) -> Callable[[object], T]:
    """A pydantic validator that accepts an instance of exactly `cls`, not of a subclass."""

    def validate(value: object) -> T:
        if type(value) is not cls:
            raise ValueError(f"expected a {cls.__name__}, got {type(value).__name__}")
        return value

    return validate


def scalar_schema[Raw: (int, str)](
    cls: type,
    raw_type: type[Raw],
    raw: core_schema.CoreSchema,
    build: Callable[[Raw], object],
    serialization: core_schema.SerSchema,
) -> core_schema.CoreSchema:
    """Build the pydantic schema for a scalar type that is a validated wrapper of a `raw_type`.

    The raw value must be exactly a `str` or `int` (so `"5"` is never a byte count and `True`
    is never a version, and a `ByteCount` is refused for a `ToolVersion` field even though it
    is an `int` underneath), and is handed to `build`, which validates it. In Python mode an
    instance of exactly `cls` is accepted as is: an instance of a subclass is not. In JSON mode
    the raw schema also applies, which is what a JSON Schema export shows. Dumped to JSON the
    value is its raw form.
    """

    def plain(value: object) -> object:
        if type(value) is not raw_type:
            raise ValueError(f"expected a plain {raw_type.__name__}, got {type(value).__name__}")
        return value

    def from_python(value: object) -> object:
        if type(value) is cls:
            return value
        if type(value) is not raw_type:
            raise ValueError(
                f"expected a {cls.__name__} or a plain {raw_type.__name__}, "
                f"got {type(value).__name__}"
            )
        return build(value)

    return core_schema.json_or_python_schema(
        json_schema=core_schema.no_info_after_validator_function(
            build, core_schema.no_info_before_validator_function(plain, raw)
        ),
        python_schema=core_schema.no_info_plain_validator_function(from_python),
        serialization=serialization,
    )


class FrozenText:
    """Immutable wrapper around one validated string.

    Equality, hashing and ordering are per concrete class: two values of different types are
    never equal, even when their text is. Subclasses implement `_validate` and `_raw_schema`, and
    must declare `__slots__` (see `check_subclass`). Instances cannot be pickled (pickle would
    rebuild one without validating it); `copy` and `deepcopy` return the instance itself.
    """

    __slots__ = ("_text",)
    _text: str

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        check_subclass(cls)

    def __init__(self, text: str) -> None:
        raw: object = text  # the annotation is for the checker; callers can still pass anything
        if type(raw) is not str:
            # Exactly str: a subclass can lie (`partition`, `__str__`, `__eq__`) to a validator
            # while the value that gets stored is something else.
            raise InvalidValueError(
                f"{type(self).__name__} is built from a str, got {type(raw).__name__}"
                + (" (a str subclass is not accepted)" if isinstance(raw, str) else "")
            )
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
        return scalar_schema(cls, str, cls._raw_schema(), cls, core_schema.to_string_ser_schema())

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
