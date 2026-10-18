"""Tests for the plumbing behind the typed scalars: runtime enforcement of what mypy enforces.

mypy rejects subclassing a `@final` class, building a ByteCount from a `str`, and so on. These
tests cover the code mypy does not see: anything built dynamically, and values that arrive from
outside as arbitrary objects.
"""

import importlib
import inspect
from typing import SupportsIndex, final

import pytest
from pydantic import TypeAdapter, ValidationError
from pydantic_core import core_schema

from tumbleweed.core import _scalar, canonical, errors, ids, jcs, types, unicode15
from tumbleweed.core._scalar import FrozenText
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import EntityId, TaskId
from tumbleweed.core.types import (
    ByteCount,
    ContentHash,
    ModelName,
    ToolVersion,
    WorkspaceRelPath,
    _BoundedInt,
    _PatternStr,
)


class Dummy(FrozenText):
    """A scalar that is not final, so that the tests can subclass it."""

    __slots__ = ()

    @classmethod
    def _validate(cls, text: str) -> None:
        if not text.startswith("d:"):
            raise InvalidValueError(f"not a Dummy: {text!r}")

    @classmethod
    def _raw_schema(cls) -> core_schema.CoreSchema:
        return core_schema.str_schema(strict=True)


class SubDummy(Dummy):
    __slots__ = ()


@final
class Leaf(FrozenText):
    __slots__ = ()

    @classmethod
    def _validate(cls, text: str) -> None:
        return None

    @classmethod
    def _raw_schema(cls) -> core_schema.CoreSchema:
        return core_schema.str_schema(strict=True)


class TestExactTypeInPythonMode:
    """pydantic python mode accepts an instance of exactly the class, never of a subclass."""

    def test_the_class_itself_passes_unchanged(self) -> None:
        dummy = Dummy("d:1")

        assert TypeAdapter(Dummy).validate_python(dummy) is dummy

    def test_an_instance_of_a_subclass_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="expected a Dummy or a plain str, got SubDummy"):
            TypeAdapter(Dummy).validate_python(SubDummy("d:1"))

    def test_a_parent_instance_is_refused_for_a_subclass_field(self) -> None:
        with pytest.raises(ValidationError, match="expected a SubDummy or a plain str, got Dummy"):
            TypeAdapter(SubDummy).validate_python(Dummy("d:1"))

    def test_raw_text_is_validated_and_wrapped(self) -> None:
        wrapped = TypeAdapter(Dummy).validate_python("d:1")

        assert type(wrapped) is Dummy
        with pytest.raises(ValidationError, match="not a Dummy"):
            TypeAdapter(Dummy).validate_python("x:1")

    def test_a_str_subclass_is_not_plain_text(self) -> None:
        class Fancy(str):
            __slots__ = ()

        with pytest.raises(ValidationError, match="plain str, got Fancy"):
            TypeAdapter(Dummy).validate_python(Fancy("d:1"))

    def test_an_int_primitive_takes_an_int_or_itself_and_nothing_else(self) -> None:
        adapter = TypeAdapter(ByteCount)
        count = ByteCount(5)

        assert adapter.validate_python(count) is count
        assert type(adapter.validate_python(5)) is ByteCount
        for bad in (True, 5.0, "5", ToolVersion(5), None):
            with pytest.raises(ValidationError):
                adapter.validate_python(bad)

    def test_a_str_primitive_takes_a_str_or_itself_and_nothing_else(self) -> None:
        adapter = TypeAdapter(ModelName)
        name = ModelName("claude-sonnet-5-5")

        assert adapter.validate_python(name) is name
        assert type(adapter.validate_python("claude-sonnet-5-5")) is ModelName
        for bad in (ContentHash("0" * 64), b"claude", 5, None, TaskId.new()):
            with pytest.raises(ValidationError):
                adapter.validate_python(bad)


class TestFinalAtRuntime:
    def test_a_final_scalar_cannot_be_subclassed(self) -> None:
        with pytest.raises(TypeError, match="cannot subclass Leaf: it is final"):
            type("Forged", (Leaf,), {"__slots__": ()})

    def test_a_non_final_scalar_can(self) -> None:
        assert issubclass(SubDummy, Dummy)

    def test_the_numeric_and_string_primitives_cannot_be_subclassed(self) -> None:
        with pytest.raises(TypeError, match="cannot subclass ByteCount"):
            type("Forged", (ByteCount,), {"__slots__": ()}, minimum=-(2**63))
        with pytest.raises(TypeError, match="cannot subclass ToolVersion"):
            type("Forged", (ToolVersion,), {"__slots__": ()}, minimum=-1)
        with pytest.raises(TypeError, match="cannot subclass ModelName"):
            type("Forged", (ModelName,), {"__slots__": ()}, pattern=".*", expects="anything")
        with pytest.raises(TypeError, match="cannot subclass ContentHash"):
            type("Forged", (ContentHash,), {"__slots__": ()}, pattern=".*", expects="anything")

    def test_the_path_types_cannot_be_subclassed(self) -> None:
        with pytest.raises(TypeError, match="cannot subclass WorkspaceRelPath"):
            type("Forged", (WorkspaceRelPath,), {"__slots__": ()})
        with pytest.raises(TypeError, match="cannot subclass CanonicalPath"):
            type("Forged", (types.CanonicalPath,), {"__slots__": ()})

    def test_every_concrete_class_in_core_is_final(self) -> None:
        """A new class that forgets @final fails here. Bases designed for subclassing are listed."""
        designed_for_subclassing = {
            EntityId,
            FrozenText,
            _BoundedInt,
            _PatternStr,
            errors.TumbleweedError,
            errors.InvalidValueError,
        }
        offenders = []
        for module in (ids, types, errors, _scalar, jcs, canonical, unicode15):
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if cls.__module__ != module.__name__ or cls in designed_for_subclassing:
                    continue
                if not cls.__dict__.get("__final__", False):
                    offenders.append(f"{cls.__module__}.{cls.__qualname__}")
        assert not offenders, f"not @final, and not designed for subclassing: {offenders}"

    def test_the_introspection_sees_every_core_module(self) -> None:
        """Guards the test above: a module added to core must be added to its list."""
        import pkgutil

        import tumbleweed.core

        modules = {m.name for m in pkgutil.iter_modules(tumbleweed.core.__path__)}

        assert modules == {"_scalar", "canonical", "errors", "ids", "jcs", "types", "unicode15"}
        for name in modules:
            importlib.import_module(f"tumbleweed.core.{name}")


class TestSlots:
    def test_a_subclass_must_declare_slots(self) -> None:
        with pytest.raises(TypeError, match="NoSlots must declare __slots__"):

            class NoSlots(FrozenText):
                pass

    def test_the_numeric_and_string_bases_need_slots_too(self) -> None:
        with pytest.raises(TypeError, match="must declare __slots__"):
            type("NoSlots", (_BoundedInt,), {}, minimum=0)
        with pytest.raises(TypeError, match="must declare __slots__"):
            type("NoSlots", (_PatternStr,), {}, pattern="a", expects="a")

    @pytest.mark.parametrize(
        "value",
        [
            TaskId.new(),
            ByteCount(1),
            ToolVersion(1),
            ContentHash("0" * 64),
            ModelName("m"),
            WorkspaceRelPath("a"),
            types.CanonicalPath.resolve("/"),
            Dummy("d:1"),
        ],
        ids=lambda v: type(v).__name__,
    )
    def test_no_scalar_has_an_instance_dict(self, value: object) -> None:
        assert not hasattr(value, "__dict__")
        with pytest.raises(TypeError):
            vars(value)

    def test_text_scalars_cannot_have_their_value_rewritten_through_a_dict(self) -> None:
        task = TaskId.new()
        before = str(task)

        with pytest.raises((TypeError, AttributeError)):
            vars(task)["_text"] = "tsk_forged"
        with pytest.raises((TypeError, AttributeError)):
            task.__dict__["_text"] = "tsk_forged"

        assert str(task) == before


VALID_ID_TEXT = "tsk_01j9z3c7q4v8s2k5m6n7p8r9t0"


class LyingStr(str):
    """A str that answers every question a validator might ask as if it were valid text."""

    __slots__ = ()

    def partition(self, sep: str, /) -> tuple[str, str, str]:
        return ("tsk", "_", "01j9z3c7q4v8s2k5m6n7p8r9t0")

    def split(self, sep: str | None = None, maxsplit: SupportsIndex = -1) -> list[str]:
        return ["a"]

    def startswith(self, *args: object, **kwargs: object) -> bool:
        return True

    def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
        return b"fine"

    def __str__(self) -> str:
        return VALID_ID_TEXT

    def __len__(self) -> int:
        return 5

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 0


class LyingInt(int):
    """An int whose comparisons and conversions lie about the value it really holds."""

    __slots__ = ()

    def __ge__(self, other: int) -> bool:
        return True

    def __le__(self, other: int) -> bool:
        return True

    def __gt__(self, other: int) -> bool:
        return True

    def __lt__(self, other: int) -> bool:
        return False

    def __index__(self) -> int:
        return 7

    def __int__(self) -> int:
        return 7

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 0


class TestLyingSubclasses:
    """A subclass can lie to a validator, so the constructors normalise before they validate."""

    def test_the_liars_really_lie(self) -> None:
        liar = LyingStr("not valid at all")
        assert str(liar) == VALID_ID_TEXT
        assert liar.partition("_")[0] == "tsk"
        assert liar.startswith("anything")
        assert LyingInt(-5) >= 0
        assert LyingInt(-5) <= 10
        assert int.__index__(LyingInt(-5)) == -5  # the one question it cannot lie about

    @pytest.mark.parametrize("id_type", ids.id_types(), ids=lambda t: t.__name__)
    def test_an_id_is_not_built_from_a_lying_str(self, id_type: type[EntityId]) -> None:
        with pytest.raises(InvalidValueError, match="a str subclass is not accepted"):
            id_type(LyingStr("not an id"))

    @pytest.mark.parametrize("cls", [ContentHash, ModelName, types.ProviderRequestId])
    def test_a_string_primitive_is_not_built_from_a_lying_str(
        self, cls: type[ContentHash | ModelName | types.ProviderRequestId]
    ) -> None:
        with pytest.raises(InvalidValueError, match="a str subclass is not accepted"):
            cls(LyingStr("!!! not valid !!!"))

    def test_a_workspace_path_is_not_built_from_a_lying_str(self) -> None:
        with pytest.raises(InvalidValueError, match="a str subclass is not accepted"):
            WorkspaceRelPath(LyingStr("../../etc/passwd"))

    def test_an_honest_str_subclass_is_refused_too(self) -> None:
        class Plain(str):
            __slots__ = ()

        with pytest.raises(InvalidValueError, match="a str subclass is not accepted"):
            TaskId(Plain(VALID_ID_TEXT))
        with pytest.raises(InvalidValueError, match="a str subclass is not accepted"):
            ModelName(Plain("claude-sonnet-5-5"))

    @pytest.mark.parametrize("cls", [ByteCount, ToolVersion])
    def test_a_lying_int_is_checked_by_what_it_really_holds(
        self, cls: type[ByteCount | ToolVersion]
    ) -> None:
        for held in (-5, -1, 0 if cls is ToolVersion else -2, 2**70, -(2**70)):
            with pytest.raises(InvalidValueError, match="must be from"):
                cls(LyingInt(held))

    @pytest.mark.parametrize("cls", [ByteCount, ToolVersion])
    def test_a_valid_lying_int_stores_the_real_value_as_an_exact_int(
        self, cls: type[ByteCount | ToolVersion]
    ) -> None:
        stored = cls(LyingInt(3))  # __index__ and __int__ claim 7

        assert int.__index__(stored) == 3
        assert type(stored) is cls
        assert LyingInt not in type(stored).__mro__  # not the liar's class, but an exact int
        assert int(stored) == 3

    def test_a_core_int_scalar_is_accepted_and_normalised(self) -> None:
        converted = ToolVersion(ByteCount(5))

        assert type(converted) is ToolVersion
        assert converted == 5
        with pytest.raises(InvalidValueError, match="must be from 1"):
            ToolVersion(ByteCount(0))

    def test_a_bool_is_still_refused(self) -> None:
        with pytest.raises(InvalidValueError, match="must be an int"):
            ByteCount(True)
