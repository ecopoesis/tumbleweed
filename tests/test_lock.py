"""The dependency cooldown is in force: no locked artifact is younger than `exclude-newer`.

A package version that is only days old can be a compromised release nobody has noticed yet. So
`[tool.uv] exclude-newer` keeps uv from picking anything newer than 7 days, and uv.lock records
that window. These tests check the setting and that the lock honours it.
"""

import datetime as dt
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MINIMUM_COOLDOWN = dt.timedelta(days=7)


def cooldown_of(setting: str) -> dt.timedelta:
    """The span of a relative `exclude-newer` such as "7 days" or "2 weeks"."""
    match = re.fullmatch(r"(\d+) (day|week)s?", setting)
    assert match is not None, (
        f"exclude-newer must be a relative duration like '7 days': {setting!r}"
    )
    return dt.timedelta(**{f"{match[2]}s": int(match[1])})


def span_of(iso: str) -> dt.timedelta:
    """The span of the ISO 8601 duration uv writes into the lock, e.g. P7D."""
    match = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?", iso)
    assert match is not None, f"unsupported exclude-newer-span in uv.lock: {iso!r}"
    return dt.timedelta(weeks=int(match[1] or 0), days=int(match[2] or 0))


type Toml = dict[str, object]


def table(value: object) -> Toml:
    assert isinstance(value, dict)
    return value


def rows(value: object) -> list[Toml]:
    assert isinstance(value, list)
    return [table(row) for row in value]


def youngest_upload(lock: Toml) -> tuple[dt.datetime, str] | None:
    """The newest upload time of any artifact in a parsed uv.lock, and which package it is."""
    youngest: tuple[dt.datetime, str] | None = None
    for package in rows(lock["package"]):
        artifacts = [
            *rows(package.get("wheels", [])),
            *([table(package["sdist"])] if "sdist" in package else []),
        ]
        for artifact in artifacts:
            stamp = artifact.get("upload-time")
            if isinstance(stamp, str):
                uploaded = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if youngest is None or uploaded > youngest[0]:
                    youngest = (uploaded, f"{package['name']} {package['version']}")
    return youngest


@pytest.fixture(scope="module")
def pyproject() -> Toml:
    return table(tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"])


@pytest.fixture(scope="module")
def lock() -> Toml:
    return tomllib.loads((ROOT / "uv.lock").read_text())


def test_pyproject_sets_a_relative_cooldown_of_at_least_a_week(pyproject: Toml) -> None:
    setting = table(pyproject["uv"])["exclude-newer"]

    assert isinstance(setting, str)
    assert cooldown_of(setting) >= MINIMUM_COOLDOWN


def test_the_lock_records_the_same_window(pyproject: Toml, lock: Toml) -> None:
    setting = table(pyproject["uv"])["exclude-newer"]
    span = table(lock["options"])["exclude-newer-span"]

    assert isinstance(setting, str)
    assert isinstance(span, str)
    assert span_of(span) == cooldown_of(setting)


def test_no_locked_artifact_is_younger_than_the_cooldown_when_the_lock_was_written(
    pyproject: Toml, lock: Toml
) -> None:
    """The lock was written no earlier than its file time (a checkout only makes it look later)."""
    setting = table(pyproject["uv"])["exclude-newer"]
    assert isinstance(setting, str)
    written = dt.datetime.fromtimestamp((ROOT / "uv.lock").stat().st_mtime, dt.UTC)

    youngest = youngest_upload(lock)

    assert youngest is not None, "no upload times in uv.lock: the check would prove nothing"
    uploaded, package = youngest
    assert uploaded <= written - cooldown_of(setting), (
        f"{package} was uploaded {uploaded:%Y-%m-%d %H:%M}, inside the cooldown window"
    )


def test_the_check_notices_a_package_that_is_too_new() -> None:
    synthetic: Toml = {
        "package": [
            {
                "name": "old",
                "version": "1",
                "sdist": {"upload-time": "2026-01-01T00:00:00Z"},
            },
            {
                "name": "fresh",
                "version": "2",
                "wheels": [{"upload-time": "2026-10-15T12:00:00.5Z"}, {"url": "no time"}],
            },
            {"name": "project", "version": "0"},
        ]
    }

    youngest = youngest_upload(synthetic)

    assert youngest == (dt.datetime(2026, 10, 15, 12, 0, 0, 500000, dt.UTC), "fresh 2")


@pytest.mark.parametrize(("text", "days"), [("7 days", 7), ("1 week", 7), ("2 weeks", 14)])
def test_durations_parse(text: str, days: int) -> None:
    assert cooldown_of(text) == dt.timedelta(days=days)


@pytest.mark.parametrize(("iso", "days"), [("P7D", 7), ("P1W", 7), ("P2W", 14), ("P2W1D", 15)])
def test_iso_spans_parse(iso: str, days: int) -> None:
    assert span_of(iso) == dt.timedelta(days=days)
