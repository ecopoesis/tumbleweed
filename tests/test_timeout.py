"""No test can hang a run: pytest-timeout is installed, configured, and does fail a hung test."""

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_the_project_sets_a_global_per_test_timeout() -> None:
    options = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]

    assert isinstance(options["timeout"], int)
    assert 30 <= options["timeout"] <= 300  # long enough for a cold mypy, short enough to matter


def test_pytest_timeout_is_a_dev_dependency() -> None:
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"]["dev"]

    assert any(requirement.startswith("pytest-timeout") for requirement in dev)


def test_a_hung_test_is_failed_not_waited_for(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import time

        def test_hangs():
            time.sleep(30)
        """
    )

    result = pytester.runpytest("--timeout=1", "-p", "no:cacheprovider")

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Timeout*1.0s*"])


def test_a_test_can_ask_for_longer(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import time
        import pytest

        @pytest.mark.timeout(10)
        def test_takes_a_moment():
            time.sleep(1.5)
        """
    )

    pytester.runpytest("--timeout=1", "-p", "no:cacheprovider").assert_outcomes(passed=1)
