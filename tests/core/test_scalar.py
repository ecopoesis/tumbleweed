"""Tests for the plumbing behind the typed scalars: runtime enforcement of what mypy enforces.

mypy rejects subclassing a `@final` class, building a ByteCount from a `str`, and so on. These
tests cover the code mypy does not see: anything built dynamically, and values that arrive from
outside as arbitrary objects.
"""

import importlib
import inspect
from typing import final

import pytest
from pydantic import TypeAdapter, ValidationError
from pydantic_core import core_schema

from tumbleweed.core import _scalar, errors, ids, types
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
        for module in (ids, types, errors, _scalar):
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

        assert modules == {"_scalar", "errors", "ids", "types"}
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
