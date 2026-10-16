"""Nothing from the "Secrets" section of .gitignore may reach the Docker build context.

`scripts/test-linux` builds the image from the repository root and `COPY`s the context into it,
so a key file in the working tree would be baked into an image layer. `.dockerignore` is an
allowlist for that reason. These tests check it against every secret pattern, in the root and in
every directory the image takes, with a model of Docker's matching rules (runs anywhere) and,
where a Docker daemon is available, against Docker itself.
"""

import posixpath
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.plugins import host

ROOT = Path(__file__).resolve().parents[1]
# Directories the image takes (`!dir/` lines in .dockerignore), and some it does not.
ALLOWED_DIRS = ["src", "src/tumbleweed", "tests", "tests/core", "scripts", "docs"]
OTHER_DIRS = [".github", ".git", ".venv", "build", "notes"]


def secret_patterns() -> list[str]:
    """The lines of the "Secrets" block of .gitignore."""
    lines = (ROOT / ".gitignore").read_text().splitlines()
    start = lines.index("# Secrets (never commit)") + 1
    block = []
    for line in lines[start:]:
        if not line.strip() or line.startswith("#"):
            break
        block.append(line.strip())
    return block


def dockerignore_patterns() -> list[str]:
    return [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


def _to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a .dockerignore pattern like Docker's pattern matcher does."""
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i + 1 : i + 2] == "*":
                while pattern[i + 1 : i + 2] == "*":
                    i += 1
                if pattern[i + 1 : i + 2] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[" and (end := pattern.find("]", i + 2)) != -1:
            out.append("[" + pattern[i + 1 : end].replace("\\", "\\\\") + "]")
            i = end
        elif char == "\\" and i + 1 < len(pattern):
            i += 1
            out.append(re.escape(pattern[i]))
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("".join(out))


def clean_pattern(raw: str) -> tuple[bool, str]:
    """A .dockerignore line as Docker reads it: (is it a `!` exception, the cleaned pattern).

    Docker runs `filepath.Clean` on it, so `./src/`, `src//a` and `scripts/../scripts/` mean what
    they would in a shell, and drops a leading `/`. (posixpath keeps two leading slashes, Go
    does not.)
    """
    negated = raw.startswith("!")
    cleaned = posixpath.normpath(raw.removeprefix("!").strip())
    if cleaned.startswith("//"):
        cleaned = "/" + cleaned.lstrip("/")
    if len(cleaned) > 1 and cleaned.startswith("/"):
        cleaned = cleaned[1:]
    return negated, cleaned


def is_excluded(path: str, patterns: list[str]) -> bool:
    """Whether Docker leaves `path` (relative to the context root) out of the build context.

    As in Docker: a pattern matches a path if it matches the path or any parent directory, and
    the last pattern that matches decides; `!` re-includes.
    """
    parents = [path.rsplit("/", i)[0] for i in range(1, path.count("/") + 1)]
    excluded = False
    for raw in patterns:
        negated, cleaned = clean_pattern(raw)
        regex = _to_regex(cleaned)
        if regex.fullmatch(path) or any(regex.fullmatch(parent) for parent in parents):
            excluded = not negated
    return excluded


def concrete(pattern: str) -> str:
    """A file name matching a .gitignore pattern: `*.key` becomes `x.key`, `.env*` `.env.local`."""
    return pattern.replace("*", "x") if not pattern.endswith("*") else pattern[:-1] + ".local"


def alternating_case(text: str) -> str:
    return "".join(char.upper() if i % 2 == 0 else char.lower() for i, char in enumerate(text))


# git here has core.ignorecase=true (macOS, Windows), so `src/x.KEY` is a secret to git, and the
# context must exclude it too. Each spelling goes in a directory of its own: on a
# case-insensitive filesystem two spellings in one directory would be one file.
SPELLINGS: dict[str, Callable[[str], str]] = {
    "": str,
    "upper": str.upper,
    "mixed": alternating_case,
}


def secret_paths() -> list[str]:
    paths = []
    for pattern in secret_patterns():
        name = concrete(pattern)
        for label, respell in SPELLINGS.items():
            for directory in ["", *ALLOWED_DIRS, *OTHER_DIRS]:
                parts = [part for part in (directory, label) if part]
                paths.append("/".join([*parts, respell(name)]))
    return paths


def test_the_secrets_block_of_gitignore_is_what_we_think_it_is() -> None:
    patterns = secret_patterns()

    assert {".anthropic-key", "*.key", "*.pem", ".env*"} <= set(patterns)


@pytest.mark.parametrize("path", secret_paths())
def test_a_secret_file_is_excluded_from_the_context(path: str) -> None:
    assert is_excluded(path, dockerignore_patterns()), f"{path} would be sent to the Docker daemon"


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "README.md",
        "src/tumbleweed/__init__.py",
        "src/tumbleweed/core/ids.py",
        "tests/conftest.py",
        "tests/core/test_ids.py",
        "scripts/linux-checks",
        "docs/ARCHITECTURE.md",
        ".gitignore",  # the tests of the context itself read these three
        ".dockerignore",
        "Dockerfile",
    ],
)
def test_what_the_image_needs_is_in_the_context(path: str) -> None:
    assert not is_excluded(path, dockerignore_patterns())


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        ".venv/bin/python",
        ".worktrees/other/pyproject.toml",
        ".omc/state.json",
        ".claude/settings.local.json",
        ".env",
        ".github/workflows/ci.yml",
        "notes.txt",
        "src/tumbleweed/__pycache__/ids.cpython-312.pyc",
        "tests/core/__pycache__/x.pyc",
        "src/tumbleweed/stray.pyc",
        "coverage.xml",
        ".coverage",
    ],
)
def test_everything_else_is_excluded(path: str) -> None:
    assert is_excluded(path, dockerignore_patterns())


def test_the_files_the_dockerfile_copies_by_name_are_allowed() -> None:
    copied: list[str] = []
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        if line.startswith("COPY --chown"):
            sources = line.split()[2:-1]  # COPY --chown=u <sources...> <destination>
            copied.extend(source for source in sources if source != ".")

    assert copied, "the Dockerfile no longer copies files by name: update this test"
    for source in copied:
        assert not is_excluded(source, dockerignore_patterns()), source


def docker_daemon_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    probe = subprocess.run(  # noqa: S603  (argv list, no shell)
        [docker, "info"], capture_output=True, check=False, timeout=60
    )
    return probe.returncode == 0


def require_docker() -> None:
    """Skip when there is no Docker daemon, or fail if CI says this run must have one.

    Inside the Linux image there is no Docker, so the cross-check runs in a CI step on the runner
    itself, with TUMBLEWEED_REQUIRE_DOCKER=1, where a missing daemon must not pass quietly.
    """
    if docker_daemon_available():
        return
    if host.read_flag(host.REQUIRE_DOCKER_ENV_VAR):
        pytest.fail("TUMBLEWEED_REQUIRE_DOCKER=1 but there is no Docker daemon to ask")
    pytest.skip("no Docker daemon available to check the model against")


def docker_sends(tmp_path: Path, files: set[str], dockerignore: str) -> set[str]:
    """Build a scratch context of `files` with this .dockerignore; return what Docker copies."""
    context = tmp_path / "context"
    for relative in files:
        target = context / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    (context / ".dockerignore").write_text(dockerignore)
    (context / "Dockerfile").write_text("FROM scratch\nCOPY . /\n")
    exported = tmp_path / "exported"

    subprocess.run(  # noqa: S603  (argv list, no shell)
        [
            shutil.which("docker") or "docker",
            "build",
            "--no-cache",
            "--output",
            f"type=local,dest={exported}",
            str(context),
        ],
        capture_output=True,
        check=True,
        timeout=200,
    )

    return {str(p.relative_to(exported)) for p in exported.rglob("*") if p.is_file()}


@pytest.mark.timeout(300)  # a Docker build, which has its own limit
def test_the_model_agrees_with_what_docker_really_sends(tmp_path: Path) -> None:
    """The real .dockerignore, against every candidate file, secrets in all their spellings."""
    require_docker()
    candidates = {
        *secret_paths(),
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "README.md",
        "CLAUDE.md",
        "AGENTS.md",
        "src/tumbleweed/__init__.py",
        "src/tumbleweed/__pycache__/x.pyc",
        "tests/conftest.py",
        "scripts/linux-checks",
        "docs/ARCHITECTURE.md",
        ".gitignore",
        ".git/config",
        ".env",
        "notes.txt",
    }
    dockerignore = (ROOT / ".dockerignore").read_text()

    sent = docker_sends(tmp_path, candidates, dockerignore)

    # The Dockerfile and .dockerignore are allowed in, so they are copied; excluded they would not.
    predicted = {
        c
        for c in {*candidates, ".dockerignore", "Dockerfile"}
        if not is_excluded(c, dockerignore_patterns())
    }
    assert sent == predicted
    assert not any(
        re.search(r"(\.key|\.pem|\.anthropic-key|(^|/)\.env[^/]*)$", s, re.I) for s in sent
    )


UNCLEAN = [
    "*",
    "!./src/",
    "!tests//",
    "!/docs",
    "!scripts/../scripts/",
    "!//README.md",
    "**/*.[kK][eE][yY]",
]
UNCLEAN_FILES = {
    "src/a.py": False,
    "src/x.KEY": True,
    "tests/b.py": False,
    "docs/c.md": False,
    "scripts/d.sh": False,
    "README.md": False,
    "other.txt": True,
    "pyproject.toml": True,
}


@pytest.mark.parametrize(("path", "excluded"), UNCLEAN_FILES.items())
def test_patterns_are_cleaned_like_docker_does(path: str, excluded: bool) -> None:
    assert is_excluded(path, UNCLEAN) is excluded


@pytest.mark.parametrize(
    ("raw", "negated", "cleaned"),
    [
        ("*", False, "*"),
        ("!src/", True, "src"),
        ("!./src/", True, "src"),
        ("!tests//", True, "tests"),
        ("!/docs", True, "docs"),
        ("!scripts/../scripts/", True, "scripts"),
        ("!//README.md", True, "README.md"),
        ("  **/*.key  ", False, "**/*.key"),
        ("!  spaced", True, "spaced"),
        ("/", False, "/"),
    ],
)
def test_clean_pattern(raw: str, negated: bool, cleaned: str) -> None:
    assert clean_pattern(raw) == (negated, cleaned)


@pytest.mark.timeout(300)  # a Docker build, which has its own limit
def test_the_model_agrees_with_docker_on_unclean_patterns(tmp_path: Path) -> None:
    require_docker()

    sent = docker_sends(tmp_path, set(UNCLEAN_FILES), "\n".join(UNCLEAN) + "\n")

    predicted = {path for path, excluded in UNCLEAN_FILES.items() if not excluded}
    assert sent == predicted


def test_a_missing_docker_skips_unless_the_run_requires_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "docker_daemon_available", lambda: False)

    monkeypatch.delenv("TUMBLEWEED_REQUIRE_DOCKER", raising=False)
    with pytest.raises(pytest.skip.Exception, match="no Docker daemon"):
        require_docker()

    monkeypatch.setenv("TUMBLEWEED_REQUIRE_DOCKER", "1")
    with pytest.raises(pytest.fail.Exception, match="TUMBLEWEED_REQUIRE_DOCKER=1"):
        require_docker()


def test_a_present_docker_is_used_whether_or_not_it_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "docker_daemon_available", lambda: True)

    monkeypatch.setenv("TUMBLEWEED_REQUIRE_DOCKER", "1")
    require_docker()
    monkeypatch.delenv("TUMBLEWEED_REQUIRE_DOCKER")
    require_docker()


@pytest.mark.parametrize("value", ["true", "yes", "2", " 1"])
def test_a_misspelt_require_docker_flag_is_an_error_not_a_quiet_skip(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "docker_daemon_available", lambda: False)
    monkeypatch.setenv("TUMBLEWEED_REQUIRE_DOCKER", value)

    with pytest.raises(pytest.UsageError, match="TUMBLEWEED_REQUIRE_DOCKER must be 0 or 1"):
        require_docker()
