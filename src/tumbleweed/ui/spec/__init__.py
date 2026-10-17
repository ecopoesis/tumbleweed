"""UI spec v1 (`tumbleweed.ui/1`): the contract between the model and the renderer.

The model emits a `UiSpec` as JSON; a fixed renderer trusts only what validates here. Pure
Python, no GTK (enforced by an import-linter contract). The reference is `docs/ui-spec.md`, the
JSON Schema is `schemas/ui-spec.v1.json`, and the limits are in `limits`.

- Entry points: `parse_ui_spec` (JSON text), `validate_ui_spec` (parsed JSON), both raising
  `SpecError`; `UiSpec.validate_patch` for state patches.
- Parts: `state` (declarations and `check_value`), `components`, `handlers`, `expr` (the
  expression AST), `check` (its type checker), `values` (tables, refs, port types).
"""

from tumbleweed.ui.spec.check import ExprType, check_expr, computed_order, expr_type_of, references
from tumbleweed.ui.spec.errors import SpecError, SpecTooLargeError
from tumbleweed.ui.spec.parse import parse_ui_spec, validate_ui_spec
from tumbleweed.ui.spec.spec import SPEC_VERSION, SPIN_MIN_STEP, UiSpec, default_float_step
from tumbleweed.ui.spec.state import StateDecl, StateValue, check_value, port_type_of
from tumbleweed.ui.spec.values import (
    PORT_TYPES,
    FileRef,
    ImageRef,
    PortType,
    Table,
)

__all__ = [
    "PORT_TYPES",
    "SPEC_VERSION",
    "SPIN_MIN_STEP",
    "ExprType",
    "FileRef",
    "ImageRef",
    "PortType",
    "SpecError",
    "SpecTooLargeError",
    "StateDecl",
    "StateValue",
    "Table",
    "UiSpec",
    "check_expr",
    "check_value",
    "computed_order",
    "default_float_step",
    "expr_type_of",
    "parse_ui_spec",
    "port_type_of",
    "references",
    "validate_ui_spec",
]
