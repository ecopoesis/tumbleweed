"""Tests for `tumbleweed.broker.errors`: fixed messages that repeat nothing they were given."""

import copy
import errno
import pickle

import pytest

from tumbleweed.broker import BrokerError, GrantError, PathError, PathFault
from tumbleweed.broker.errors import describe_fault
from tumbleweed.core.errors import TumbleweedError


def test_the_hierarchy() -> None:
    assert issubclass(BrokerError, TumbleweedError)
    assert issubclass(PathError, BrokerError)
    assert issubclass(GrantError, BrokerError)


@pytest.mark.parametrize("fault", list(PathFault))
def test_every_fault_has_a_short_fixed_message(fault: PathFault) -> None:
    text = describe_fault(fault)

    assert text
    assert text.isprintable()
    assert len(text) < 80
    assert str(PathError(fault)) == f"{fault.value}: {text}"


def test_the_operating_system_error_name_is_added_not_its_message() -> None:
    error = PathError(PathFault.DENIED, errno.EACCES)

    assert error.errno_name == "EACCES"
    assert str(error) == "denied: permission denied (EACCES)"


def test_an_unknown_error_number_has_no_name() -> None:
    error = PathError(PathFault.IO, 99999)

    assert error.errno_name is None
    assert "(" not in str(error)


@pytest.mark.parametrize("copier", [pickle.loads, copy.deepcopy], ids=["pickle", "deepcopy"])
def test_survives_a_copy_with_its_fault(copier: object) -> None:
    error = PathError(PathFault.SYMLINK, errno.ELOOP)

    copied = (
        pickle.loads(pickle.dumps(error))  # noqa: S301  (our own object, in the test)
        if copier is pickle.loads
        else copy.deepcopy(error)
    )

    assert isinstance(copied, PathError)
    assert (copied.fault, copied.errno_code, str(copied)) == (
        error.fault,
        error.errno_code,
        str(error),
    )
