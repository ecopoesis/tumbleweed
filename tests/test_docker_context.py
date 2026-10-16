"""Nothing from the "Secrets" section of .gitignore may reach the Docker build context.

`scripts/test-linux` builds the image from the repository root and `COPY`s the context into it,
so a key file in the working tree would be baked into an image layer. `.dockerignore` is an
allowlist for that reason. These tests check it against every secret pattern, in the root and in
every directory the image takes, with a model of Docker's matching rules (runs anywhere) and,
where a Docker daemon is available, against Docker itself.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

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


def is_excluded(path: str, patterns: list[str]) -> bool:
    """Whether Docker leaves `path` (relative to the context root) out of the build context.

    As in Docker: a pattern matches a path if it matches the path or any parent directory, and
    the last pattern that matches decides; `!` re-includes.
    """
    parents = [path.rsplit("/", i)[0] for i in range(1, path.count("/") + 1)]
    excluded = False
    for raw in patterns:
        negated = raw.startswith("!")
        cleaned = raw.removeprefix("!").strip("/")
        regex = _to_regex(cleaned)
        if regex.fullmatch(path) or any(regex.fullmatch(parent) for parent in parents):
            excluded = not negated
    return excluded


def concrete(pattern: str) -> str:
    """A file name matching a .gitignore pattern: `*.key` becomes `x.key`, `.env*` `.env.local`."""
    return pattern.replace("*", "x") if not pattern.endswith("*") else pattern[:-1] + ".local"


def secret_paths() -> list[str]:
    names = [concrete(pattern) for pattern in secret_patterns()]
    return [
        name if not d else f"{d}/{name}" for name in names for d in ["", *ALLOWED_DIRS, *OTHER_DIRS]
    ]


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


def test_the_model_agrees_with_what_docker_really_sends(tmp_path: Path) -> None:
    """Build a scratch context holding every candidate file and see what Docker copies."""
    if not docker_daemon_available():
        pytest.skip("no Docker daemon available to check the model against")
    candidates = {
        *secret_paths(),
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "README.md",
        "CLAUDE.md",
        "src/tumbleweed/__init__.py",
        "src/tumbleweed/__pycache__/x.pyc",
        "tests/conftest.py",
        "scripts/linux-checks",
        "docs/ARCHITECTURE.md",
        ".git/config",
        ".env",
        "notes.txt",
    }
    context = tmp_path / "context"
    for relative in candidates:
        target = context / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    (context / ".dockerignore").write_text((ROOT / ".dockerignore").read_text())
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
        timeout=300,
    )

    sent = {str(p.relative_to(exported)) for p in exported.rglob("*") if p.is_file()}
    predicted = {c for c in candidates if not is_excluded(c, dockerignore_patterns())}
    assert sent == predicted
    assert not any(re.search(r"(\.key|\.pem|\.anthropic-key|(^|/)\.env[^/]*)$", s) for s in sent)
