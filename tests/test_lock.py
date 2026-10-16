"""The dependency cooldown is in force: no locked artifact is younger than its cutoff.

A package version that is only days old can be a compromised release nobody has noticed yet. So
`[tool.uv] exclude-newer` keeps uv from picking anything newer than 7 days, and uv.lock records
that window. These tests check the setting, that the lock records it, and that no artifact in the
lock is younger than its cutoff when the lock was written.

"When the lock was written" is the time of the last git commit that touched uv.lock (now, if it has
uncommitted changes), not the file's modification time: that is the moment of the checkout, and
would make the check pass vacuously as time goes by. Where there is no git history (the Linux
image has none) the age check is skipped with that reason and the setting checks still run; CI runs
the age check on the macOS jobs, which fetch the full history.

A package can have its own, later cutoff through `[tool.uv] exclude-newer-package` (the README's
way to take a security fix early); such a package is checked against that cutoff instead.
"""

import datetime as dt
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import NoReturn

import pytest

from tests.plugins import host

ROOT = Path(__file__).resolve().parents[1]
MINIMUM_COOLDOWN = dt.timedelta(days=7)

type Toml = dict[str, object]


def table(value: object) -> Toml:
    assert isinstance(value, dict)
    return value


def rows(value: object) -> list[Toml]:
    assert isinstance(value, list)
    return [table(row) for row in value]


def cooldown_of(setting: str) -> dt.timedelta:
    """The span of a relative `exclude-newer` such as "7 days" or "2 weeks"."""
    match = re.fullmatch(r"(\d+) (day|week)s?", setting)
    assert match is not None, f"not a relative duration like '7 days': {setting!r}"
    return dt.timedelta(**{f"{match[2]}s": int(match[1])})


def span_of(iso: str) -> dt.timedelta:
    """The span of the ISO 8601 duration uv writes into the lock, e.g. P7D."""
    match = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?", iso)
    assert match is not None, f"unsupported exclude-newer-span in uv.lock: {iso!r}"
    return dt.timedelta(weeks=int(match[1] or 0), days=int(match[2] or 0))


def normalise(name: str) -> str:
    """A package name as uv compares them (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def cutoff_of(setting: str, written: dt.datetime) -> dt.datetime:
    """The cutoff an `exclude-newer(-package)` value means for a lock written at `written`.

    A relative duration counts back from then; anything else is an absolute date or timestamp.
    """
    if re.fullmatch(r"\d+ (day|week)s?", setting):
        return written - cooldown_of(setting)
    parsed = dt.datetime.fromisoformat(setting.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def too_young(
    lock: Toml, global_cutoff: dt.datetime, overrides: dict[str, dt.datetime]
) -> list[str]:
    """The artifacts of a parsed uv.lock uploaded after the cutoff that applies to their package."""
    found = []
    for package in rows(lock["package"]):
        name = normalise(str(package["name"]))
        cutoff = overrides.get(name, global_cutoff)
        artifacts = [
            *rows(package.get("wheels", [])),
            *([table(package["sdist"])] if "sdist" in package else []),
        ]
        for artifact in artifacts:
            stamp = artifact.get("upload-time")
            if isinstance(stamp, str):
                uploaded = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if uploaded > cutoff:
                    found.append(f"{name} {package['version']} uploaded {uploaded:%Y-%m-%d %H:%M}")
    return sorted(set(found))


def overrides_of(uv_settings: Toml, written: dt.datetime) -> dict[str, dt.datetime]:
    per_package = uv_settings.get("exclude-newer-package", {})
    return {
        normalise(name): cutoff_of(str(value), written)
        for name, value in table(per_package).items()
    }


class NoLockHistory(Exception):  # noqa: N818  (not an error: the age check skips on it)
    """uv.lock cannot be dated from git here; the message says why."""


# So that a stray user or system config cannot change what git answers, and so that `git status`
# never writes `.git/index` (an optional lock for refreshing stat data) in a checkout we only read.
GIT_ISOLATION = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_OPTIONAL_LOCKS": "0",
}


def git_env(home: Path) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(home), **GIT_ISOLATION}


def git_output(git: str, root: Path, *args: str) -> str | None:
    result = subprocess.run(  # noqa: S603  (argv list, no shell)
        [git, "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env=git_env(root),
    )
    return result.stdout if result.returncode == 0 else None


def skip_unless_history_required(why: NoLockHistory) -> NoReturn:
    """Skip the age check, or fail it where CI says the git history must be there."""
    if host.read_flag(host.REQUIRE_GIT_ENV_VAR):
        pytest.fail(f"{host.REQUIRE_GIT_ENV_VAR}=1 but uv.lock cannot be dated from git: {why}")
    pytest.skip(f"uv.lock cannot be dated from git here ({why}): the macOS CI jobs run this")


def lock_written_at(root: Path) -> dt.datetime:
    """When uv.lock was last written: now if uncommitted, else the time of its last commit.

    Raises `NoLockHistory` when git cannot say: no git, no `.git` of its own in `root` (a copy, or
    a checkout nested inside another repository, whose history git would otherwise use), a
    shallow clone (its history is cut off, so HEAD would stand in for the lock's commit), or no
    commit of the lock yet.
    """
    git = shutil.which("git")
    if git is None:
        raise NoLockHistory("the git program is not installed")
    if not (root / ".git").exists():
        raise NoLockHistory("there is no .git here (a copy, or a checkout nested in a repository)")
    shallow = git_output(git, root, "rev-parse", "--is-shallow-repository")
    if shallow is None:
        raise NoLockHistory("git cannot read this repository")
    if shallow.strip() == "true":
        raise NoLockHistory("this is a shallow clone: the lock's last commit may be cut off")
    status = git_output(git, root, "status", "--porcelain", "--", "uv.lock")
    if status is None:
        raise NoLockHistory("git status failed")
    if status.strip():
        return dt.datetime.now(dt.UTC)
    stamp = git_output(git, root, "log", "-1", "--format=%ct", "--", "uv.lock")
    if stamp is None or not stamp.strip():
        raise NoLockHistory("uv.lock has no commit")
    return dt.datetime.fromtimestamp(int(stamp), dt.UTC)


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


def test_no_locked_artifact_is_younger_than_its_cutoff_when_the_lock_was_written(
    pyproject: Toml, lock: Toml
) -> None:
    try:
        written = lock_written_at(ROOT)
    except NoLockHistory as why:
        skip_unless_history_required(why)
    uv_settings = table(pyproject["uv"])
    setting = uv_settings["exclude-newer"]
    assert isinstance(setting, str)

    found = too_young(lock, cutoff_of(setting, written), overrides_of(uv_settings, written))

    assert not found, f"inside the cooldown window when the lock was written: {found}"


def test_the_lock_is_not_empty_of_upload_times() -> None:
    """Guards the age check: with no upload times it would prove nothing."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text())

    assert any(
        isinstance(artifact.get("upload-time"), str)
        for package in rows(lock["package"])
        for artifact in rows(package.get("wheels", []))
    )


class TestTooYoung:
    NOW = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.UTC)
    CUTOFF = NOW - dt.timedelta(days=7)

    @staticmethod
    def lock_of(*packages: Toml) -> Toml:
        return {"package": list(packages), "options": {}}

    def test_a_package_inside_the_window_is_noticed(self) -> None:
        lock = self.lock_of(
            {"name": "old", "version": "1", "sdist": {"upload-time": "2026-01-01T00:00:00Z"}},
            {
                "name": "fresh",
                "version": "2",
                "wheels": [{"upload-time": "2026-10-15T12:00:00.5Z"}, {"url": "no time"}],
            },
            {"name": "project", "version": "0"},
        )

        assert too_young(lock, self.CUTOFF, {}) == ["fresh 2 uploaded 2026-10-15 12:00"]

    def test_the_cutoff_is_exclusive_of_exactly_that_instant(self) -> None:
        lock = self.lock_of(
            {"name": "edge", "version": "1", "sdist": {"upload-time": "2026-10-09T12:00:00Z"}}
        )

        assert too_young(lock, self.CUTOFF, {}) == []

    def test_a_package_with_its_own_later_cutoff_is_checked_against_that_one(self) -> None:
        lock = self.lock_of(
            {"name": "Fixed_Pkg", "version": "3", "sdist": {"upload-time": "2026-10-15T00:00:00Z"}},
            {"name": "other", "version": "1", "sdist": {"upload-time": "2026-10-15T00:00:00Z"}},
        )
        overrides = {normalise("fixed.pkg"): self.NOW}  # the README's early-fix escape hatch

        assert too_young(lock, self.CUTOFF, overrides) == ["other 1 uploaded 2026-10-15 00:00"]

    def test_an_override_is_not_a_blanket_for_newer_than_itself(self) -> None:
        lock = self.lock_of(
            {"name": "pkg", "version": "3", "sdist": {"upload-time": "2026-10-16T08:00:00Z"}}
        )

        assert too_young(lock, self.CUTOFF, {"pkg": self.NOW - dt.timedelta(days=2)}) != []

    def test_overrides_are_read_from_pyproject_as_timestamps_dates_or_spans(self) -> None:
        uv_settings: Toml = {
            "exclude-newer-package": {
                "Some_Package": "2026-10-15T00:00:00Z",
                "dated": "2026-10-01",
                "relative": "2 days",
            }
        }

        assert overrides_of(uv_settings, self.NOW) == {
            "some-package": dt.datetime(2026, 10, 15, tzinfo=dt.UTC),
            "dated": dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
            "relative": self.NOW - dt.timedelta(days=2),
        }

    def test_no_overrides_is_normal(self) -> None:
        assert overrides_of({"exclude-newer": "7 days"}, self.NOW) == {}

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Foo_Bar", "foo-bar"),
            ("foo.bar", "foo-bar"),
            ("FOO--bar", "foo-bar"),
            ("pytest", "pytest"),
        ],
    )
    def test_names_are_compared_the_way_uv_does(self, name: str, expected: str) -> None:
        assert normalise(name) == expected


@pytest.mark.parametrize(("text", "days"), [("7 days", 7), ("1 week", 7), ("2 weeks", 14)])
def test_durations_parse(text: str, days: int) -> None:
    assert cooldown_of(text) == dt.timedelta(days=days)


@pytest.mark.parametrize(("iso", "days"), [("P7D", 7), ("P1W", 7), ("P2W", 14), ("P2W1D", 15)])
def test_iso_spans_parse(iso: str, days: int) -> None:
    assert span_of(iso) == dt.timedelta(days=days)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
class TestLockWrittenAt:
    """The reference time comes from git, not from the file's modification time."""

    @staticmethod
    def commit(repo: Path, message: str, when: dt.datetime) -> None:
        git = shutil.which("git") or "git"
        stamp = when.isoformat()
        env = {**git_env(repo), "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        config = [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.org",
            "-c",
            "commit.gpgsign=false",
        ]
        for args in (["add", "-A"], [*config, "commit", "-q", "-m", message]):
            subprocess.run([git, "-C", str(repo), *args], check=True, env=env, capture_output=True)  # noqa: S603

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        git = shutil.which("git") or "git"
        subprocess.run([git, "init", "-q", str(tmp_path)], check=True, capture_output=True)  # noqa: S603
        (tmp_path / "uv.lock").write_text("lock v1\n")
        (tmp_path / "other.txt").write_text("1\n")
        self.commit(tmp_path, "lock", dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC))
        return tmp_path

    def test_it_is_the_time_of_the_last_commit_that_touched_the_lock(self, repo: Path) -> None:
        (repo / "other.txt").write_text("2\n")
        self.commit(repo, "unrelated", dt.datetime(2026, 6, 1, tzinfo=dt.UTC))

        assert lock_written_at(repo) == dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)

    def test_a_fresh_file_modification_time_does_not_move_it(self, repo: Path) -> None:
        """What a new checkout does to every file: the reason mtime is not used."""
        os.utime(repo / "uv.lock", None)

        assert lock_written_at(repo) == dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)

    def test_a_later_change_to_the_lock_is_the_new_reference(self, repo: Path) -> None:
        (repo / "uv.lock").write_text("lock v2\n")
        self.commit(repo, "relock", dt.datetime(2026, 9, 9, tzinfo=dt.UTC))

        assert lock_written_at(repo) == dt.datetime(2026, 9, 9, tzinfo=dt.UTC)

    def test_an_uncommitted_change_means_now(self, repo: Path) -> None:
        (repo / "uv.lock").write_text("lock v2, not committed\n")

        written = lock_written_at(repo)

        assert written is not None
        assert abs(dt.datetime.now(dt.UTC) - written) < dt.timedelta(minutes=1)

    def test_without_git_history_it_is_unknown(self, tmp_path: Path) -> None:
        (tmp_path / "uv.lock").write_text("lock\n")

        with pytest.raises(NoLockHistory, match=r"no \.git here"):
            lock_written_at(tmp_path)

    def test_without_the_git_program_it_is_unknown(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)

        with pytest.raises(NoLockHistory, match="not installed"):
            lock_written_at(repo)

    def test_a_repository_with_no_commit_of_the_lock_is_unknown(self, tmp_path: Path) -> None:
        git = shutil.which("git") or "git"
        subprocess.run([git, "init", "-q", str(tmp_path)], check=True, capture_output=True)  # noqa: S603
        (tmp_path / "other.txt").write_text("1\n")
        self.commit(tmp_path, "no lock yet", dt.datetime(2026, 1, 1, tzinfo=dt.UTC))

        with pytest.raises(NoLockHistory, match="no commit"):
            lock_written_at(tmp_path)

    def test_an_untracked_lock_is_new_so_it_means_now(self, tmp_path: Path) -> None:
        git = shutil.which("git") or "git"
        subprocess.run([git, "init", "-q", str(tmp_path)], check=True, capture_output=True)  # noqa: S603
        (tmp_path / "other.txt").write_text("1\n")
        self.commit(tmp_path, "no lock yet", dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
        (tmp_path / "uv.lock").write_text("a new lock\n")

        written = lock_written_at(tmp_path)

        assert abs(dt.datetime.now(dt.UTC) - written) < dt.timedelta(minutes=1)

    def test_a_shallow_clone_is_unknown_not_dated_by_head(
        self, repo: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        (repo / "other.txt").write_text("2\n")
        self.commit(
            repo,
            "a newer commit that does not touch the lock",
            dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        )
        clone = tmp_path_factory.mktemp("shallow") / "clone"
        git = shutil.which("git") or "git"
        subprocess.run(  # noqa: S603  (argv list, no shell)
            [git, "clone", "-q", "--depth", "1", f"file://{repo}", str(clone)],
            check=True,
            capture_output=True,
            env=git_env(clone.parent),
        )

        with pytest.raises(NoLockHistory, match="shallow"):
            lock_written_at(clone)

    def test_a_checkout_nested_in_another_repository_is_unknown(self, repo: Path) -> None:
        """Without the `.git` check, git would walk up and date the lock by the outer history."""
        nested = repo / "vendor" / "checkout"
        nested.mkdir(parents=True)
        (nested / "uv.lock").write_text("a copy of some other lock\n")
        self.commit(repo, "vendor a copy", dt.datetime(2026, 7, 7, tzinfo=dt.UTC))

        with pytest.raises(NoLockHistory, match=r"no \.git here"):
            lock_written_at(nested)

    def test_git_is_run_isolated_from_stray_config_and_optional_locks(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[dict[str, str]] = []
        real_run = subprocess.run

        def spying_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            env = kwargs.get("env")
            assert isinstance(env, dict)
            seen.append(env)
            # The spy takes `object` arguments so that it can stand in for any call, and
            # `subprocess.run` is overloaded on its arguments: mypy cannot pick an overload for
            # `object` (call-overload) and so types the result as Any (no-any-return). The spy only
            # forwards the call unchanged.
            return real_run(*args, **kwargs)  # type: ignore[no-any-return,call-overload]

        monkeypatch.setattr(subprocess, "run", spying_run)

        lock_written_at(repo)

        assert seen, "git was never run"
        for env in seen:
            assert env["GIT_CONFIG_GLOBAL"] == os.devnull
            assert env["GIT_CONFIG_NOSYSTEM"] == "1"
            assert env["GIT_OPTIONAL_LOCKS"] == "0"

    def test_reading_the_lock_date_does_not_write_the_index(self, repo: Path) -> None:
        """`git status` normally refreshes stat data in .git/index after a touch."""
        os.utime(repo / "uv.lock", (1, 1))  # makes the index's stat data stale
        index = repo / ".git" / "index"
        before = (index.read_bytes(), index.stat().st_mtime_ns)

        lock_written_at(repo)

        assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_a_missing_git_history_skips_unless_the_run_requires_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(host.REQUIRE_GIT_ENV_VAR, raising=False)
    with pytest.raises(pytest.skip.Exception, match="cannot be dated from git"):
        skip_unless_history_required(NoLockHistory("shallow"))

    monkeypatch.setenv(host.REQUIRE_GIT_ENV_VAR, "1")
    with pytest.raises(pytest.fail.Exception, match="TUMBLEWEED_REQUIRE_GIT=1"):
        skip_unless_history_required(NoLockHistory("shallow"))


@pytest.mark.parametrize("value", ["true", "yes", "2", " 1"])
def test_a_misspelt_require_git_flag_is_an_error_not_a_quiet_skip(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(host.REQUIRE_GIT_ENV_VAR, value)

    with pytest.raises(pytest.UsageError, match="TUMBLEWEED_REQUIRE_GIT must be 0 or 1"):
        skip_unless_history_required(NoLockHistory("shallow"))
