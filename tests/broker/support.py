"""Shared builders for the broker tests: a fake user's home, and the deny rules built for it."""

from dataclasses import dataclass
from pathlib import Path

from tumbleweed.broker import DenyContext, DenyRules, ResolvedPath

# Directories that exist in the fake home; every other deny entry is left missing, so that both
# kinds (matched by identity and by text alone) are exercised.
EXISTING_PROTECTED = (".ssh", ".gnupg", ".aws", ".config/gcloud", ".local/share/tumbleweed")
EXISTING_PROTECTED_FILES = (".netrc",)
OTHER_DIRS = ("Documents", "Photos", "Photos/2026", "Projects/app")


@dataclass(frozen=True)
class World:
    """A temporary file system laid out like a user's, with the deny rules built for it."""

    base: Path
    home: Path
    context: DenyContext
    rules: DenyRules


def make_world(base: Path) -> World:
    """Lay out `base/home` and build the rules for it. `base` must already be canonical."""
    home = base / "home"
    for relative in (*EXISTING_PROTECTED, *OTHER_DIRS):
        (home / relative).mkdir(parents=True)
    for relative in EXISTING_PROTECTED_FILES:
        (home / relative).write_text("secret")
    (home / "Photos" / "a.jpg").write_text("jpeg")
    (home / "Documents" / "notes.txt").write_text("notes")
    runtime = base / "runtime"
    runtime.mkdir()
    context = DenyContext(
        homes=(str(home),),
        config_home=str(home / ".config"),
        data_home=str(home / ".local" / "share"),
        state_home=str(home / ".local" / "state"),
        cache_home=str(home / ".cache"),
        runtime_dir=str(runtime),
    )
    return World(base=base, home=home, context=context, rules=DenyRules.build(context))


def resolve(path: Path | str) -> ResolvedPath:
    return ResolvedPath.resolve(str(path))
