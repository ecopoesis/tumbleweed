"""What goes wrong with a spec, in a form that is safe to show to the model or to log.

A spec comes from the model, and a rejected one goes back to the model so that it can correct
itself (S12). Both directions need bounded, inert text:

- at most `MAX_REPORTED_PROBLEMS` problems of at most `MAX_PROBLEM_LENGTH` characters each;
- every character that is not printable (controls, format characters such as a bidirectional
  override, line and paragraph separators, lone surrogates) comes out as `\\uXXXX`, so input
  cannot forge a line, move the cursor or reorder text in a log, a terminal or the chat;
- a problem says where it is and what is wrong. The validators of the text types, names and
  references quote at most a short excerpt (64 characters) of the offending value; pydantic's
  own messages quote none (the one that would, for an unknown `kind`, is rewritten); the
  location may name an unknown field, which is cut to `MAX_LOCATION_PART` characters.

Building the report is cheap, not free: pydantic itself collects every error of a validation and
`ValidationError.errors()` turns all of them into dicts, which cannot be limited from outside. What
bounds that work is the input: a spec is at most `MAX_SPEC_BYTES`, an object of more than
`MAX_OBJECT_MEMBERS` members is refused while parsing (so no object yields thousands of
unknown-field errors), and every sequence stops at its first error (`fail_fast`), so the number of
errors is proportional to the size of the spec (`tests/ui/spec/test_resources.py` holds the memory
and the time). From those, `problems_of` looks at the first `SCAN_LIMIT` errors and builds strings
only until the report is full.
"""

from collections.abc import Sequence
from typing import Final, Self, final

from pydantic import ValidationError

from tumbleweed.core.errors import TumbleweedError

MAX_REPORTED_PROBLEMS: Final = 10
MAX_PROBLEM_LENGTH: Final = 300
MAX_LOCATION_PART: Final = 40  # characters of one part of a location, such as a field name
MAX_LOCATION: Final = 120  # characters of a whole location; the middle is cut
SCAN_LIMIT: Final = 200  # errors examined when building a report
MORE_PROBLEMS: Final = "... and more problems"


def printable(text: str) -> str:
    """`text` with every character that is not printable written as `\\uXXXX`."""
    if text.isprintable():
        return text
    return "".join(
        char
        if char.isprintable()
        else f"\\u{ord(char):04x}"
        if ord(char) <= 0xFFFF
        else f"\\U{ord(char):08x}"
        for char in text
    )


def _bounded(problem: str) -> str:
    if len(problem) <= MAX_PROBLEM_LENGTH:
        return problem
    return problem[: MAX_PROBLEM_LENGTH - 3] + "..."


class SpecError(TumbleweedError):
    """A spec, a state value or a state patch was refused. `problems` says why, boundedly."""

    problems: tuple[str, ...]

    def __init__(self, problems: Sequence[str]) -> None:
        if isinstance(problems, str):
            # `for problem in "abc"` would make three problems of one letter each.
            raise TypeError("SpecError takes a sequence of problems, not a single string")
        given = tuple(problems[: MAX_REPORTED_PROBLEMS + 1])
        shown = tuple(_bounded(printable(problem)) for problem in given[:MAX_REPORTED_PROBLEMS])
        self._given = given
        self.problems = shown + ((MORE_PROBLEMS,) if len(given) > MAX_REPORTED_PROBLEMS else ())
        super().__init__("; ".join(self.problems))

    def __reduce__(self) -> tuple[type[Self], tuple[list[str]]]:
        # The default would rebuild from the joined message, which is a single string.
        return (type(self), (list(self._given),))


@final
class SpecTooLargeError(SpecError):
    """The raw spec is longer than `MAX_SPEC_BYTES`, so it was not even parsed."""


def _location(loc: tuple[int | str, ...]) -> str:
    where = ""
    for index, part in enumerate(loc):
        if isinstance(part, int):
            where += f"[{part}]"
            continue
        text = printable(part)
        if len(text) > MAX_LOCATION_PART:
            text = text[:MAX_LOCATION_PART] + "..."
        where += text if index == 0 else f".{text}"
    if len(where) > MAX_LOCATION:
        # Keep both ends: the start says which part of the spec, the end says which field.
        head = MAX_LOCATION // 3
        where = f"{where[:head]}...{where[-(MAX_LOCATION - head - 3) :]}"
    return where


_TAG_MARKER: Final = " does not match any of the expected tags:"


def _message(item_type: str, message: str) -> str:
    message = message.removeprefix("Value error, ")
    if item_type == "union_tag_invalid":
        # "Input tag '<the tag, as written>' found using 'kind' does not match any of the expected
        # tags: ...": say what is expected and do not echo the tag.
        return "unknown kind; expected one of:" + message.rpartition(_TAG_MARKER)[2]
    return message


def problems_of(error: ValidationError) -> list[str]:
    """One line per validation problem: where it is and what is wrong, without the input.

    Pydantic has collected all the errors already (see the module docstring); of those, at most
    `SCAN_LIMIT` are examined and strings are built only until there are more problems than a
    `SpecError` reports. An error that only says a sequence was too short is
    dropped when something inside it already failed. A cross-reference check reports several
    problems at once as one multi-line message of this package's own making that already names
    where each is: only such a message, a `value_error` at the root, is split into lines. Any
    other text keeps its newlines, which `SpecError` writes as `\\n`.
    """
    scanned = error.errors(include_input=False, include_url=False, include_context=False)[
        :SCAN_LIMIT
    ]
    inside_failures = {
        item["loc"][:depth]
        for item in scanned
        if item["type"] != "too_short"
        for depth in range(len(item["loc"]))
    }
    problems: list[str] = []
    for item in scanned:
        if len(problems) > MAX_REPORTED_PROBLEMS:
            break
        loc = item["loc"]
        if item["type"] == "too_short" and loc in inside_failures:
            continue
        where = _location(loc)
        message = _message(item["type"], item["msg"])
        lines = message.split("\n") if item["type"] == "value_error" and not loc else [message]
        problems.extend(f"{where}: {line}" if where else line for line in lines)
    return problems
