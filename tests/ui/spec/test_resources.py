"""A hostile spec of the largest size costs a bounded amount of memory and time to refuse.

pydantic keeps validating the items of a sequence after one fails, and describes every failure
(four of them for each item that matches no member of a union), so 256 KiB of `[]` used to cost
more than 500 MiB before the report was cut to ten lines. Every variadic sequence now stops at
its first error (`fail_fast`), the report is built from a bounded number of errors, and these
tests hold the cost to a stated budget by measuring the peak resident memory of a fresh
interpreter, which sees what the Rust half of pydantic allocates too.
"""

import json
import subprocess
import sys

import pytest

from tumbleweed.ui.spec.limits import MAX_SPEC_BYTES

MEMORY_BUDGET = 64 * 2**20  # bytes of growth over the interpreter that has imported everything
TIME_BUDGET = 5.0  # seconds, to validate and refuse (it takes tens of milliseconds)

PREAMBLE = '{"version":"tumbleweed.ui/1","title":"t","root":{"kind":"box"'
EMPTY_LISTS = ("[]," * 87_000)[:-1]  # 255 KiB of cells that are not cells
EMPTY_OBJECTS = ("{}," * 80_000)[:-1]
ZEROS = ("0," * 120_000)[:-1]


def spec_with(root_extra: str = "", state: str = "") -> str:
    return PREAMBLE + root_extra + '},"state":[' + state + "]}"


SHAPES = {
    "a table row of 87k things that are not cells": spec_with(
        state='{"kind":"table","key":"t","default":{"columns":[{"name":"a","type":"int"}],'
        '"rows":[[' + EMPTY_LISTS + "]]}}"
    ),
    "a list default of 87k things that are not items": spec_with(
        state='{"kind":"list","key":"l","item_type":"int","default":[' + EMPTY_LISTS + "]}"
    ),
    "a list default of 120k valid items, far over its limit": spec_with(
        state='{"kind":"list","key":"l","item_type":"int","max_items":10,"default":[' + ZEROS + "]}"
    ),
    "a table row of 120k valid cells, far over the columns": spec_with(
        state='{"kind":"table","key":"t","default":{"columns":[{"name":"a","type":"int"}],'
        '"rows":[[' + ZEROS + "]]}}"
    ),
    "a table of 120k valid rows": spec_with(
        state='{"kind":"table","key":"t","default":{"columns":[{"name":"a","type":"int"}],'
        '"rows":[' + ("[0]," * 60_000)[:-1] + "]}}"
    ),
    "80k children that are not components": PREAMBLE + ',"children":[' + EMPTY_OBJECTS + "]}}",
    "80k state entries that are not entries": spec_with(state=EMPTY_OBJECTS),
    "80k operands that are not expressions": spec_with(
        state='{"kind":"int","key":"x","default":0,"computed":{"kind":"min","args":['
        + EMPTY_OBJECTS
        + "]}}"
    ),
    "80k handlers that are not handlers": spec_with()[:-1] + ',"handlers":[' + EMPTY_OBJECTS + "]}",
    "80k enum options that are not options": spec_with(
        state='{"kind":"enum","key":"e","default":"a","options":[' + EMPTY_LISTS + "]}"
    ),
    "20k fields that do not exist": PREAMBLE
    + ","
    + ",".join(f'"u{i}":0' for i in range(20_000))
    + "}}",
}

MEASURE = r"""
import json, resource, sys, time
from tumbleweed.ui.spec import SpecError, parse_ui_spec

def peak():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024  # bytes there, kilobytes here

text = sys.stdin.read()
before = peak()
started = time.perf_counter()
try:
    parse_ui_spec(text)
    problems = -1
except SpecError as error:
    problems = len(error.problems)
print(json.dumps({"growth": peak() - before, "seconds": time.perf_counter() - started,
                  "problems": problems, "size": len(text.encode())}))
"""


def measure(text: str) -> dict[str, float]:
    result = subprocess.run(  # noqa: S603  (argv list, no shell; our own interpreter)
        [sys.executable, "-I", "-c", MEASURE],
        input=text,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    measured = json.loads(result.stdout)
    assert isinstance(measured, dict)
    return measured


@pytest.mark.parametrize("shape", SHAPES, ids=lambda name: name[:50])
def test_a_hostile_spec_of_the_largest_size_is_refused_within_the_budget(shape: str) -> None:
    measured = measure(SHAPES[shape])

    assert measured["size"] <= MAX_SPEC_BYTES, "the shape must get past the size limit"
    assert measured["problems"] >= 1, "the spec must be refused for its content, not accepted"
    assert measured["growth"] < MEMORY_BUDGET, (
        f"{measured['growth'] / 2**20:.0f} MiB of growth, the budget is "
        f"{MEMORY_BUDGET / 2**20:.0f} MiB"
    )
    assert measured["seconds"] < TIME_BUDGET


def test_the_shapes_really_are_that_large_and_refused_for_their_content() -> None:
    sizes = {name: len(text.encode()) for name, text in SHAPES.items()}

    assert max(sizes.values()) <= MAX_SPEC_BYTES
    assert min(sizes.values()) > 100_000
