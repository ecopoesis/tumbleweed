"""The base of every model in a spec: strict, closed and immutable (CLAUDE.md typing rule 5)."""

from pydantic import BaseModel, ConfigDict


class SpecModel(BaseModel):
    """Strict types, no unknown fields, no assignment after validation.

    Sequences are tuples (see `text.as_tuple`), so a validated spec cannot be edited behind the
    validators' backs either. The one exception is the content of a `json` state value, which is
    arbitrary JSON and so holds lists and dicts.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
