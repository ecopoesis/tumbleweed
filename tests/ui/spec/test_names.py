"""The names of a spec: state keys, handler, module and function names."""

import inspect

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import PatternStr
from tumbleweed.ui.spec import names
from tumbleweed.ui.spec.limits import MAX_NAME_LENGTH

# Every name type of the module, found by what it is rather than listed by hand.
NAME_TYPES: list[type[PatternStr]] = [
    cls
    for _, cls in inspect.getmembers(names, inspect.isclass)
    if issubclass(cls, PatternStr) and cls.__module__ == names.__name__
]
STRICT = ConfigDict(strict=True)

VALID = ["a", "tip", "tip_percent", "x1", "a_", "a__b", "a" * MAX_NAME_LENGTH]
INVALID = [
    "",
    "A",
    "Tip",
    "1a",
    "_a",
    "__init__",
    "a-b",
    "a b",
    "a.b",
    "a/b",
    "a\n",
    "\na",
    "a\0",
    " a",
    "a ",
    "é",
    "\uff41",  # fullwidth a
    "\u0430",  # Cyrillic a
    "a" * (MAX_NAME_LENGTH + 1),
]


class LyingStr(str):
    """Answers every question a validator asks as if it were a valid name."""

    __slots__ = ()

    def __len__(self) -> int:
        return 1


def test_the_module_defines_the_four_kinds_of_name() -> None:
    assert {cls.__name__ for cls in NAME_TYPES} == {
        "StateKey",
        "HandlerName",
        "ModuleName",
        "FunctionName",
    }


@pytest.mark.parametrize("name_type", NAME_TYPES, ids=lambda t: t.__name__)
class TestEveryNameType:
    @pytest.mark.parametrize("text", VALID)
    def test_accepts_lowercase_snake_case_up_to_the_limit(
        self, name_type: type[PatternStr], text: str
    ) -> None:
        assert str(name_type(text)) == text

    @pytest.mark.parametrize("text", INVALID, ids=lambda t: repr(t)[:20])
    def test_refuses_everything_else(self, name_type: type[PatternStr], text: str) -> None:
        with pytest.raises(InvalidValueError, match=name_type.__name__):
            name_type(text)

    @pytest.mark.parametrize("bad", [None, 5, b"a", ["a"], LyingStr("a")])
    def test_takes_exactly_a_str(self, name_type: type[PatternStr], bad: object) -> None:
        with pytest.raises(InvalidValueError, match="must be a str"):
            name_type(bad)  # type: ignore[arg-type]  # the point of the test

    def test_a_pydantic_field_takes_the_json_string(self, name_type: type[PatternStr]) -> None:
        adapter = TypeAdapter(name_type, config=STRICT)

        assert type(adapter.validate_json('"tip"')) is name_type
        assert adapter.dump_json(name_type("tip")) == b'"tip"'
        with pytest.raises(ValidationError):
            adapter.validate_json('"Tip"')

    def test_a_field_refuses_another_kind_of_name_instance(
        self, name_type: type[PatternStr]
    ) -> None:
        other = next(cls for cls in NAME_TYPES if cls is not name_type)
        adapter = TypeAdapter(name_type, config=STRICT)

        with pytest.raises(ValidationError, match="plain str"):
            adapter.validate_python(other("tip"))

    def test_is_final_and_has_no_instance_dict(self, name_type: type[PatternStr]) -> None:
        assert name_type.__dict__.get("__final__", False)
        assert not hasattr(name_type("a"), "__dict__")
        with pytest.raises(TypeError, match="final"):
            type("Forged", (name_type,), {"__slots__": ()}, pattern=".*", expects="anything")

    def test_the_schema_carries_the_pattern(self, name_type: type[PatternStr]) -> None:
        class Holder(BaseModel):
            model_config = STRICT
            name: name_type  # type: ignore[valid-type]  # chosen per test

        pattern = Holder.model_json_schema()["properties"]["name"]["pattern"]

        assert pattern == f"^(?:[a-z][a-z0-9_]{{0,{MAX_NAME_LENGTH - 1}}})$"
