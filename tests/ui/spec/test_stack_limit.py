"""A caller near the recursion limit gets a `SpecError`, never a `RecursionError` (#62).

`parse_ui_spec` is swept against the real limit in `test_parse.py`. `validate_patch` runs the
pydantic validators of a value, which recurse a little, so the mapping is tested by making the
checking itself run out of stack, which is what a caller a few frames from the limit causes.
"""

from unittest import mock

import pytest

from tests.ui.spec.support import build, spec_of
from tumbleweed.ui.spec.errors import SpecError
from tumbleweed.ui.spec.spec import UiSpec


def test_running_out_of_stack_while_checking_a_patch_is_a_spec_error() -> None:
    spec = spec_of(**build(state=[{"kind": "int", "key": "n", "default": 1}]))

    with (
        mock.patch.object(UiSpec, "_validated_patch", side_effect=RecursionError),
        pytest.raises(SpecError, match="too little stack"),
    ):
        spec.validate_patch({"n": 2}, current={})


def test_the_patch_check_still_works_without_the_stack_running_out() -> None:
    spec = spec_of(**build(state=[{"kind": "int", "key": "n", "default": 1}]))

    assert spec.validate_patch({"n": 2}, current={}) == {"n": 2}
