"""Concrete classes of the package are `@final`; the few bases made for subclassing are listed.

The same rule as `tests/core/test_scalar.py` applies to `tumbleweed.core` (CLAUDE.md typing
rule 6): a class that is not designed for subclassing cannot be subclassed, so nobody can
override a validator or an `__eq__` of a spec model.
"""

import importlib
import inspect
import pkgutil

import tumbleweed.ui.spec
from tumbleweed.ui.spec.errors import SpecError
from tumbleweed.ui.spec.expr import ExprNode
from tumbleweed.ui.spec.model import SpecModel
from tumbleweed.ui.spec.state import _StateBase

DESIGNED_FOR_SUBCLASSING = {SpecModel, ExprNode, _StateBase, SpecError}


def modules() -> list[str]:
    return sorted(m.name for m in pkgutil.iter_modules(tumbleweed.ui.spec.__path__))


def test_the_introspection_sees_every_module_of_the_package() -> None:
    assert modules() == [
        "check",
        "components",
        "errors",
        "expr",
        "handlers",
        "limits",
        "model",
        "names",
        "parse",
        "schema",
        "spec",
        "state",
        "text",
        "values",
    ]


def test_every_concrete_class_is_final() -> None:
    offenders = []
    for name in modules():
        module = importlib.import_module(f"tumbleweed.ui.spec.{name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or cls in DESIGNED_FOR_SUBCLASSING:
                continue
            if not cls.__dict__.get("__final__", False):
                offenders.append(f"{cls.__module__}.{cls.__qualname__}")

    assert not offenders, f"not @final, and not designed for subclassing: {offenders}"


def test_the_bases_made_for_subclassing_are_each_subclassed() -> None:
    # A base nobody extends should not be on the list.
    assert issubclass(tumbleweed.ui.spec.expr.Add, ExprNode)
    assert issubclass(ExprNode, SpecModel)
    assert issubclass(tumbleweed.ui.spec.state.IntState, _StateBase)
    assert issubclass(tumbleweed.ui.spec.errors.SpecTooLargeError, SpecError)
