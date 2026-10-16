"""The base of every boundary model in the broker: strict, closed and immutable."""

from pydantic import BaseModel, ConfigDict


class PolicyModel(BaseModel):
    """Strict types, no unknown fields, no assignment after validation (CLAUDE.md typing rule 5).

    Sequences are tuples and sets are frozensets, so a validated model cannot be edited behind
    the validators' backs either.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
