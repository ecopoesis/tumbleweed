"""What goes wrong with a spec, in a form that is safe to show to the model or to log.

A spec comes from the model, and a rejected one goes back to the model so that it can correct
itself (S12). Both directions need bounded text: the problems are counted and cut to length, and
they name where the problem is and what is wrong, never the offending value.
"""

from collections.abc import Sequence

from pydantic import ValidationError

from tumbleweed.core.errors import TumbleweedError

MAX_REPORTED_PROBLEMS = 10
MAX_PROBLEM_LENGTH = 300


def _bounded(problem: str) -> str:
    if len(problem) <= MAX_PROBLEM_LENGTH:
        return problem
    return problem[: MAX_PROBLEM_LENGTH - 3] + "..."


class SpecError(TumbleweedError):
    """A spec, a state value or a state patch was refused. `problems` says why, boundedly."""

    problems: tuple[str, ...]

    def __init__(self, problems: Sequence[str]) -> None:
        shown = tuple(_bounded(problem) for problem in problems[:MAX_REPORTED_PROBLEMS])
        omitted = len(problems) - len(shown)
        self.problems = shown + ((f"... and {omitted} more problems",) if omitted > 0 else ())
        super().__init__("; ".join(self.problems))


class SpecTooLargeError(SpecError):
    """The raw spec is longer than `MAX_SPEC_BYTES`, so it was not even parsed."""


def problems_of(error: ValidationError) -> list[str]:
    """One line per validation problem: where it is and what is wrong, without the input.

    A cross-reference check reports several problems at once as one multi-line message that
    already names where each is, so each of its lines becomes a problem of its own.
    """
    problems: list[str] = []
    for item in error.errors(include_input=False, include_url=False, include_context=False):
        where = "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" if i else str(part)
            for i, part in enumerate(item["loc"])
        )
        prefix = f"{where}: " if where else ""
        message = item["msg"].removeprefix("Value error, ")
        problems.extend(f"{prefix}{line}" for line in message.splitlines())
    return problems
