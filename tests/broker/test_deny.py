"""Tests for `tumbleweed.broker.deny`: the static deny rules and the ancestor rule."""

import logging
import os
import posixpath
import pwd as accounts
import sys
import unicodedata
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import ValidationError

from tests.broker.expected_deny import EXPECTED_HOME_DENY, EXPECTED_SYSTEM_DENY
from tests.broker.fsutil import can_search_denied_directories
from tests.broker.support import World, resolve
from tumbleweed.broker import (
    HOME_DENY,
    SYSTEM_DENY,
    BrokerError,
    DenyContext,
    DenyRelation,
    DenyRules,
    ExtraDeny,
    FsIdentity,
    PathError,
    PathFault,
    ResolvedPath,
    StoredPath,
    deny_entries,
)
from tumbleweed.broker.deny import SHARED_DIRECTORY, DenyEntry, DenyForm
from tumbleweed.core.types import CanonicalPath

EQUAL, INSIDE, ANCESTOR = DenyRelation.EQUAL, DenyRelation.INSIDE, DenyRelation.ANCESTOR


def first_entry_for(world: World, path: str) -> str:
    return next(d.label for d in deny_entries(world.context) if d.path == path)


def relation_of(world: World, path: str | Path) -> tuple[str, DenyRelation] | None:
    match = world.rules.check(resolve(path))
    return None if match is None else (match.entry, match.relation)


class TestTheList:
    def test_names_everything_architecture_section_7_names(self, world: World) -> None:
        labels = {definition.label for definition in deny_entries(world.context)}

        for required in (
            "~/.ssh",
            "~/.gnupg",
            "~/.aws",
            "~/.config/gcloud",
            "~/.kube",
            "~/.docker",
            "~/.netrc",
            "~/.local/share/keyrings",  # keyrings
            "~/.mozilla",  # browser profiles
            "~/.config/google-chrome",
            "~/.config/chromium",
            "$XDG_CONFIG_HOME/tumbleweed",  # Tumbleweed's own directories
            "$XDG_DATA_HOME/tumbleweed",
            "$XDG_STATE_HOME/tumbleweed",
            "$XDG_CACHE_HOME/tumbleweed",
            "$XDG_RUNTIME_DIR",
            "/proc",
            "/sys",
            "/dev",
            "/etc/shadow",
        ):
            assert required in labels, required

    def test_home_entries_are_computed_for_each_home_of_the_user(self, tmp_path: Path) -> None:
        context = DenyContext(
            homes=("/home/alice", "/var/home/alice"),
            config_home="/home/alice/.config",
            data_home="/home/alice/.local/share",
            state_home="/home/alice/.local/state",
            cache_home="/home/alice/.cache",
        )

        paths = {definition.path for definition in deny_entries(context)}

        assert {"/home/alice/.ssh", "/var/home/alice/.ssh", "/var/home/alice/.aws"} <= paths
        assert all(path.startswith("/") for path in paths)

    def test_the_xdg_directories_and_the_runtime_directory_come_from_the_context(self) -> None:
        context = DenyContext(
            homes=("/home/alice",),
            config_home="/cfg",
            data_home="/data",
            state_home="/state",
            cache_home="/cache",
            runtime_dir="/run/user/1000",
            extra=(ExtraDeny(label="broker sockets", path="/run/user/1000/tumbleweed"),),
        )

        found = {definition.label: definition.path for definition in deny_entries(context)}

        assert found["$XDG_CONFIG_HOME/tumbleweed"] == "/cfg/tumbleweed"
        assert found["$XDG_DATA_HOME/tumbleweed"] == "/data/tumbleweed"
        assert found["$XDG_STATE_HOME/tumbleweed"] == "/state/tumbleweed"
        assert found["$XDG_CACHE_HOME/tumbleweed"] == "/cache/tumbleweed"
        assert found["$XDG_RUNTIME_DIR"] == "/run/user/1000"
        assert found["broker sockets"] == "/run/user/1000/tumbleweed"

    def test_no_runtime_directory_means_no_entry_for_it(self) -> None:
        context = DenyContext(
            homes=("/h",),
            config_home="/c",
            data_home="/d",
            state_home="/s",
            cache_home="/k",
        )

        assert "$XDG_RUNTIME_DIR" not in {d.label for d in deny_entries(context)}

    def test_every_label_is_short_printable_ascii_and_every_path_absolute(
        self, world: World
    ) -> None:
        for definition in deny_entries(world.context):
            assert definition.label.isascii()
            assert definition.label.isprintable()
            assert 1 <= len(definition.label) <= 64
            assert posixpath.isabs(definition.path)
            assert definition.path == posixpath.normpath(definition.path)

    def test_no_entry_is_the_home_or_the_root(self, world: World) -> None:
        """An entry for the home itself would make everything in it denied."""
        paths = {definition.path for definition in deny_entries(world.context)}

        assert str(world.home) not in paths
        assert "/" not in paths

    def test_a_path_named_twice_is_kept_once_under_its_first_label(self, world: World) -> None:
        """The XDG data directory is `~/.local/share`, so two labels name one path."""
        path = str(world.home / ".local" / "share" / "tumbleweed")
        labels = [d.label for d in deny_entries(world.context) if d.path == path]

        assert labels == ["~/.local/share/tumbleweed", "$XDG_DATA_HOME/tumbleweed"]
        assert "~/.local/share/tumbleweed" in world.rules.labels
        assert "$XDG_DATA_HOME/tumbleweed" not in world.rules.labels

    def test_a_label_that_is_not_printable_ascii_is_refused(self) -> None:
        for label in ("", "a\nb", "café", "x" * 65):
            with pytest.raises(ValidationError):
                ExtraDeny(label=label, path="/x")

    def test_an_extra_path_must_be_absolute_and_normalised(self) -> None:
        for path in ("relative", "/a/../b", "/a/", "/a\0b"):
            with pytest.raises(ValidationError):
                ExtraDeny(label="x", path=path)

    def test_the_context_needs_a_home(self) -> None:
        with pytest.raises(ValidationError):
            DenyContext(
                homes=(), config_home="/c", data_home="/d", state_home="/s", cache_home="/k"
            )


ALL_ENTRIES = [
    pytest.param(d.path, id=d.label)
    for d in deny_entries(
        DenyContext(
            homes=("/home/alice",),
            config_home="/home/alice/.config",
            data_home="/home/alice/.local/share",
            state_home="/home/alice/.local/state",
            cache_home="/home/alice/.cache",
            runtime_dir="/run/user/1000",
        )
    )
]


class TestEveryEntry:
    """Each entry is tested three ways: the path itself, below it, and above it.

    The fake home is `base/home`, so each home entry is tested at its real place there; the
    system entries are absolute. Some exist (matched by identity as well as text) and some do not.
    """

    @pytest.mark.parametrize("relative", HOME_DENY)
    def test_a_home_entry_is_refused_inside_and_above(
        self, shared_world: World, relative: str
    ) -> None:
        path = shared_world.home / relative
        label = first_entry_for(shared_world, str(path))

        assert relation_of(shared_world, path) == (label, EQUAL)
        assert relation_of(shared_world, path / "child" / "grandchild") == (label, INSIDE)
        above = shared_world.rules.check(resolve(path.parent))
        assert above is not None
        assert above.relation is ANCESTOR

    @pytest.mark.parametrize("path", SYSTEM_DENY)
    def test_a_system_entry_is_refused_inside_and_above(
        self, shared_world: World, path: str
    ) -> None:
        # Where the entry is a symlink on this host (`/var/run/docker.sock` is one on a Mac
        # running Docker Desktop), what is protected is where it leads, so test there.
        entry = resolve(path)
        below = f"{entry.text}/child/grandchild"

        equal = shared_world.rules.check(entry)
        above = shared_world.rules.check(resolve(posixpath.dirname(entry.text)))
        above_text = shared_world.rules.check_text(
            StoredPath(path=posixpath.dirname(entry.text), identity=FsIdentity(fsid=1, ino=2))
        )
        stored = StoredPath(path=below, identity=FsIdentity(fsid=1, ino=2))
        inside_text = shared_world.rules.check_text(stored)

        assert equal is not None
        assert equal.relation is EQUAL
        assert above is not None  # (for /tmp/.X11-unix it is /tmp: a shared directory, by itself)
        assert above_text is not None
        assert above_text.relation is ANCESTOR
        assert inside_text is not None
        assert inside_text.relation is INSIDE
        if Path(entry.text).is_dir() and not os.access(entry.text, os.X_OK):
            # A directory this user cannot search (`/root`, `/etc/ssl/private` for a user other
            # than root) cannot be resolved into, which is a refusal of its own.
            with pytest.raises(PathError) as raised:
                resolve(below)
            assert raised.value.fault is PathFault.INVALID
        else:
            inside = shared_world.rules.check(resolve(below))
            assert inside is not None
            assert inside.relation is INSIDE

    def test_the_xdg_and_runtime_entries(self, shared_world: World) -> None:
        runtime = str(shared_world.base / "runtime")

        assert relation_of(shared_world, runtime) == ("$XDG_RUNTIME_DIR", EQUAL)
        assert relation_of(shared_world, f"{runtime}/tumbleweed/broker.sock") == (
            "$XDG_RUNTIME_DIR",
            INSIDE,
        )
        for relative in (".config", ".local/state", ".cache"):
            path = shared_world.home / relative / "tumbleweed"
            assert relation_of(shared_world, path) == (f"~/{relative}/tumbleweed", EQUAL)


class TestTheAncestorRule:
    def test_the_home_is_never_grantable(self, world: World) -> None:
        match = world.rules.check(resolve(world.home))

        assert match is not None
        assert match.relation is ANCESTOR

    def test_nor_is_any_directory_above_it_up_to_the_root(self, world: World) -> None:
        for ancestor in (world.home, *world.home.parents):
            match = world.rules.check(resolve(ancestor))
            assert match is not None, ancestor
            # (a shared directory such as /tmp, where the test world lives on Linux, is refused
            # for being one, by itself)
            assert match.relation is ANCESTOR or match.entry == SHARED_DIRECTORY, ancestor

    @pytest.mark.parametrize(
        "relative", [".config", ".local", ".local/share", ".cache", ".local/state", ".var"]
    )
    def test_nor_is_a_directory_that_holds_a_protected_one(
        self, world: World, relative: str
    ) -> None:
        assert relation_of(world, world.home / relative) is not None

    def test_the_root(self, world: World) -> None:
        match = world.rules.check(resolve("/"))

        assert match is not None
        assert match.relation is ANCESTOR

    def test_a_directory_with_nothing_protected_in_it_is_fine(self, world: World) -> None:
        for relative in ("Documents", "Photos", "Photos/2026", "Projects/app", "Projects"):
            assert world.rules.check(resolve(world.home / relative)) is None, relative
        assert world.rules.check(resolve(world.home / "Photos" / "a.jpg")) is None

    def test_a_sibling_whose_name_starts_like_an_entry_is_fine(self, world: World) -> None:
        for name in (".ssh2", ".sshx", ".ssh-backup", ".awsome", ".config/gcloud2"):
            path = world.home / name
            path.mkdir(parents=True)
            assert world.rules.check(resolve(path)) is None, name

    def test_a_path_outside_the_home_that_holds_nothing_protected_is_fine(
        self, world: World
    ) -> None:
        (world.base / "elsewhere").mkdir()

        assert world.rules.check(resolve(world.base / "elsewhere")) is None


ZWJ = chr(0x200D)  # zero-width joiner
ZWNJ = chr(0x200C)  # zero-width non-joiner


def fullwidth(text: str) -> str:
    """The full-width forms of printable ASCII (U+FF01 to U+FF5E), which Unicode folds to ASCII."""
    return "".join(chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char for char in text)


def spellings(name: str) -> list[str]:
    """Other ways to write `name` that a file system, or Unicode, may count as the same."""
    return [
        name.upper(),
        name.title(),
        "".join(c.upper() if i % 2 else c for i, c in enumerate(name)),
        fullwidth(name),
        name[:2] + ZWJ + name[2:],
        name[:2] + ZWNJ + name[2:],
        fullwidth(name.upper()),
    ]


class TestSpellings:
    """A name is not a path's identity: case and Unicode variants of an entry are that entry."""

    @pytest.mark.parametrize("name", [".ssh", ".kube"], ids=["exists", "missing"])
    def test_variants_of_a_protected_directory(self, world: World, name: str) -> None:
        # `.ssh` exists in the world and `.kube` does not: matched by identity where the volume
        # equates the spellings (macOS) and by folded text everywhere, which is what is checked.
        for variant in spellings(name):
            match = world.rules.check(resolve(f"{world.home}/{variant}"))

            assert match is not None, ascii(variant)
            assert match.entry == f"~/{name}"
            assert match.relation is EQUAL

    def test_variants_below_and_above_an_entry_in_a_nested_place(self, world: World) -> None:
        inside = world.rules.check(resolve(f"{world.home}/.CONFIG/GCLOUD/credentials.db"))
        spelled = world.rules.check(
            resolve(f"{world.home}/{fullwidth('.config')}/{fullwidth('gcloud')}")
        )

        assert inside is not None
        assert inside.relation is INSIDE
        assert spelled is not None
        assert spelled.relation is EQUAL

    def test_a_variant_of_the_home_itself(self, tmp_path: Path) -> None:
        """The request spells the home directory differently from the deny list."""
        composed = "h" + chr(0xE9) + "me"
        (tmp_path / composed).mkdir()
        context = DenyContext(
            homes=(str(tmp_path / composed),),
            config_home=str(tmp_path / composed / ".config"),
            data_home=str(tmp_path / composed / ".local" / "share"),
            state_home=str(tmp_path / composed / ".local" / "state"),
            cache_home=str(tmp_path / composed / ".cache"),
        )
        rules = DenyRules.build(context)
        decomposed = unicodedata.normalize("NFD", composed)
        assert decomposed != composed

        match = rules.check(resolve(f"{tmp_path}/{decomposed}/.ssh"))

        assert match is not None
        assert match.entry == "~/.ssh"
        assert match.relation is EQUAL

    def test_stored_paths_are_compared_the_same_way(self, world: World) -> None:
        identity = FsIdentity(fsid=1, ino=2)

        for variant in (*spellings(".ssh"), ".Ssh/id_rsa"):
            stored = StoredPath(path=f"{world.home}/{variant}", identity=identity)
            assert world.rules.check_text(stored) is not None, ascii(variant)

    def test_different_names_are_not_confused(self, world: World) -> None:
        for name in (".sshd", ".s-sh", "ssh", ".ssh" + chr(0xE9), ".sh"):
            assert world.rules.check(resolve(f"{world.home}/{name}")) is None, name


class TestIdentity:
    """What a name cannot say, the file system can."""

    def test_a_hard_link_to_a_protected_file_is_that_file(self, world: World) -> None:
        link = world.home / "Documents" / "innocent.txt"
        os.link(world.home / ".netrc", link)

        match = world.rules.check(resolve(link))

        assert match is not None
        assert match.entry == "~/.netrc"
        assert match.relation is EQUAL

    def test_a_symlinked_entry_protects_its_target_and_the_directories_above_it(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        (home / "dotfiles" / "aws").mkdir(parents=True)
        (home / ".aws").symlink_to("dotfiles/aws")
        context = DenyContext(
            homes=(str(home),),
            config_home=str(home / ".config"),
            data_home=str(home / ".local" / "share"),
            state_home=str(home / ".local" / "state"),
            cache_home=str(home / ".cache"),
        )
        rules = DenyRules.build(context)

        target = rules.check(resolve(home / "dotfiles" / "aws" / "credentials"))
        above = rules.check(resolve(home / "dotfiles"))
        through_link = rules.check(resolve(home / ".aws"))

        assert target is not None
        assert target.relation is INSIDE
        assert above is not None
        assert above.relation is ANCESTOR
        assert through_link is not None
        assert through_link.relation is EQUAL

    def test_the_literal_path_stays_protected_if_the_symlink_is_later_replaced(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        (home / "dotfiles" / "aws").mkdir(parents=True)
        (home / ".aws").symlink_to("dotfiles/aws")
        context = DenyContext(
            homes=(str(home),),
            config_home=str(home / ".config"),
            data_home=str(home / ".local" / "share"),
            state_home=str(home / ".local" / "state"),
            cache_home=str(home / ".cache"),
        )
        rules = DenyRules.build(context)
        (home / ".aws").unlink()
        (home / ".aws").mkdir()

        match = rules.check(resolve(home / ".aws" / "credentials"))

        assert match is not None
        assert match.relation is INSIDE

    def test_a_directory_replaced_by_one_of_the_same_name_is_still_protected(
        self, world: World
    ) -> None:
        (world.home / ".ssh").rename(world.home / ".ssh-moved")
        (world.home / ".ssh").mkdir()

        assert relation_of(world, world.home / ".ssh" / "id_rsa") is not None

    def test_the_moved_directory_is_still_protected_under_its_new_name(self, world: World) -> None:
        """The rules were built when `.ssh` was there; the object keeps its identity."""
        (world.home / ".ssh").rename(world.home / "renamed")

        match = world.rules.check(resolve(world.home / "renamed"))

        assert match is not None
        assert match.entry == "~/.ssh"
        assert match.relation is EQUAL

    @pytest.mark.skipif(sys.platform != "darwin", reason="the data volume exists on macOS only")
    def test_the_macos_data_volume_spelling_of_the_home_is_the_home(self, world: World) -> None:
        """`/System/Volumes/Data` holds the user's files under the names they have in `/` (the
        firmlinks are not symlinks, so nothing resolves one to the other): the home reached by
        that name, and the data volume itself, are as ungrantable as the home."""
        data = "/System/Volumes/Data"
        for ancestor in (data, f"{data}/private", f"{data}{world.home}"):
            match = world.rules.check(resolve(ancestor))
            assert match is not None, ancestor
            assert match.relation is ANCESTOR, ancestor
        protected = world.rules.check(resolve(f"{data}{world.home}/.ssh"))
        assert protected is not None
        assert protected.relation is EQUAL
        assert world.rules.check(resolve(f"{data}{world.home}/Documents")) is None
        assert world.rules.check(resolve(f"{data}/Applications")) is None

    def test_checking_does_no_file_system_access(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inside = resolve(world.home / ".ssh" / "id")
        clean = resolve(world.home / "Documents")
        stored = StoredPath(path=str(world.home / ".ssh"), identity=FsIdentity(fsid=1, ino=2))

        def forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("a check must not touch the file system")

        for name in ("stat", "lstat", "open", "fstat", "statvfs", "fstatvfs", "listdir", "scandir"):
            monkeypatch.setattr(os, name, forbidden)

        assert world.rules.check(inside) is not None
        assert world.rules.check(clean) is None
        assert world.rules.check_text(stored) is not None


class TestMatchingByIdentityAlone:
    """The identity checks, isolated from the text checks: the entry and the path are given
    different names and only the identities (file system and inode numbers) agree or not.

    Real directories cannot do this without a bind mount, which needs privilege, so the records
    are made by hand. The entry's name is `/never/matches`; the path's is under `/syn`."""

    ROOT, PROJECTS, ENTRY, OTHER = (FsIdentity(fsid=9, ino=n) for n in (1, 2, 3, 4))

    def rules(self, *, chain: tuple[FsIdentity, ...], target: FsIdentity | None) -> DenyRules:
        form = DenyForm(("never", "matches"), frozenset(chain), target)
        return DenyRules([DenyEntry("synthetic", (form,))])

    def path(self, text: str, chain: tuple[FsIdentity, ...]) -> ResolvedPath:
        return ResolvedPath(canonical=CanonicalPath.resolve(text), chain=chain)

    def test_a_path_whose_directory_chain_passes_through_the_entry_is_inside_it(self) -> None:
        rules = self.rules(chain=(self.ROOT, self.ENTRY), target=self.ENTRY)
        deep = self.path("/syn/a/b/c", (self.ROOT, self.PROJECTS, self.ENTRY, self.OTHER))

        match = rules.check(deep)

        assert match is not None
        assert (match.entry, match.relation) == ("synthetic", INSIDE)

    def test_a_path_that_is_the_entry_by_identity_is_equal_to_it(self) -> None:
        rules = self.rules(chain=(self.ROOT, self.ENTRY), target=self.ENTRY)
        same = self.path("/syn", (self.ROOT, self.ENTRY))

        match = rules.check(same)

        assert match is not None
        assert match.relation is EQUAL

    def test_a_path_that_is_a_directory_above_the_entry_by_identity_is_its_ancestor(self) -> None:
        rules = self.rules(chain=(self.ROOT, self.PROJECTS, self.ENTRY), target=self.ENTRY)
        above = self.path("/syn", (self.ROOT, self.PROJECTS))

        match = rules.check(above)

        assert match is not None
        assert match.relation is ANCESTOR

    def test_the_ancestor_of_an_entry_that_does_not_exist_is_found_by_identity_too(self) -> None:
        rules = self.rules(chain=(self.ROOT, self.PROJECTS), target=None)
        above = self.path("/syn", (self.ROOT, self.PROJECTS))

        match = rules.check(above)

        assert match is not None
        assert match.relation is ANCESTOR

    def test_unrelated_identities_do_not_match(self) -> None:
        rules = self.rules(chain=(self.ROOT, self.PROJECTS, self.ENTRY), target=self.ENTRY)

        assert rules.check(self.path("/syn", (self.ROOT, self.OTHER))) is None
        assert rules.check(self.path("/syn/a", (self.ROOT, self.OTHER, self.OTHER))) is None

    def test_a_path_that_does_not_exist_has_no_identity_to_compare(self) -> None:
        """Its chain stops at the last directory that exists, but that directory is not the
        path: by identity it is neither the entry nor above it, and only its text can say."""
        rules = self.rules(chain=(self.ROOT, self.ENTRY), target=self.ENTRY)
        missing = self.path("/syn/missing", (self.ROOT,))

        assert missing.identity is None
        assert rules.check(missing) is None


class TestUnresolvableEntries:
    @pytest.mark.skipif(not can_search_denied_directories(), reason="root ignores permission bits")
    def test_an_entry_that_cannot_be_resolved_is_still_matched_by_its_text(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "locked" / "inner").mkdir(parents=True)
        (tmp_path / "locked").chmod(0)
        context = DenyContext(
            homes=(str(tmp_path / "home"),),
            config_home=str(tmp_path / "c"),
            data_home=str(tmp_path / "d"),
            state_home=str(tmp_path / "s"),
            cache_home=str(tmp_path / "k"),
            extra=(ExtraDeny(label="locked place", path=str(tmp_path / "locked" / "inner")),),
        )
        try:
            with caplog.at_level(logging.WARNING, logger="tumbleweed.broker.deny"):
                rules = DenyRules.build(context)
        finally:
            (tmp_path / "locked").chmod(0o755)

        stored = StoredPath(
            path=str(tmp_path / "locked" / "inner" / "x"), identity=FsIdentity(fsid=1, ino=2)
        )
        match = rules.check_text(stored)
        assert match is not None
        assert match.entry == "locked place"
        warnings = [r for r in caplog.records if "locked place" in r.getMessage()]
        assert len(warnings) == 1
        assert str(tmp_path) not in warnings[0].getMessage()


class TestForTheCurrentUser:
    def fake_passwd(self, monkeypatch: pytest.MonkeyPatch, home: str) -> None:
        entry = accounts.struct_passwd(("alice", "x", 1000, 1000, "Alice", home, "/bin/sh"))
        monkeypatch.setattr(accounts, "getpwuid", lambda uid: entry)

    def test_the_passwd_home_and_the_usual_xdg_places(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.fake_passwd(monkeypatch, "/home/alice")

        context = DenyContext.for_current_user({})

        assert context.homes == ("/home/alice",)
        assert context.config_home == "/home/alice/.config"
        assert context.data_home == "/home/alice/.local/share"
        assert context.state_home == "/home/alice/.local/state"
        assert context.cache_home == "/home/alice/.cache"
        assert context.runtime_dir is None

    def test_a_different_home_variable_is_protected_as_well(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.fake_passwd(monkeypatch, "/home/alice")

        context = DenyContext.for_current_user({"HOME": "/var/home/alice"})

        assert context.homes == ("/home/alice", "/var/home/alice")

    def test_the_same_home_twice_is_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fake_passwd(monkeypatch, "/home/alice")

        assert DenyContext.for_current_user({"HOME": "/home/alice/"}).homes == ("/home/alice",)

    def test_xdg_variables_are_used_normalised_and_relative_ones_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.fake_passwd(monkeypatch, "/home/alice")
        environ = {
            "XDG_CONFIG_HOME": "/cfg//x/../y/",
            "XDG_DATA_HOME": "relative/data",
            "XDG_STATE_HOME": "",
            "XDG_CACHE_HOME": "//cache",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        }

        context = DenyContext.for_current_user(environ)

        assert context.config_home == "/cfg/y"
        assert context.data_home == "/home/alice/.local/share"
        assert context.state_home == "/home/alice/.local/state"
        assert context.cache_home == "/cache"
        assert context.runtime_dir == "/run/user/1000"

    def test_an_absolute_variable_that_is_unusable_is_an_error_not_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ignoring it would leave the directory it names unprotected."""
        self.fake_passwd(monkeypatch, "/home/alice")

        with pytest.raises(BrokerError) as raised:
            DenyContext.for_current_user({"XDG_DATA_HOME": "/data/a\nb"})

        assert "a\nb" not in str(raised.value)

    def test_no_home_at_all_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(uid: int) -> accounts.struct_passwd:
            raise KeyError(uid)

        monkeypatch.setattr(accounts, "getpwuid", missing)

        with pytest.raises(BrokerError, match="home directory"):
            DenyContext.for_current_user({})

    def test_a_uid_without_a_passwd_entry_falls_back_to_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def missing(uid: int) -> accounts.struct_passwd:
            raise KeyError(uid)

        monkeypatch.setattr(accounts, "getpwuid", missing)

        assert DenyContext.for_current_user({"HOME": "/home/bob"}).homes == ("/home/bob",)

    def test_extra_entries_are_carried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.fake_passwd(monkeypatch, "/home/alice")
        extra = ExtraDeny(label="snapshots", path="/snapshots")

        context = DenyContext.for_current_user({}, extra=[extra])

        assert context.extra == (extra,)

    def test_the_real_user(self) -> None:
        """Without any patching: this process's own passwd entry (the CI container's included)."""
        context = DenyContext.for_current_user()

        assert (
            accounts.getpwuid(os.getuid()).pw_dir.rstrip("/")
            in {home.rstrip("/") for home in context.homes}
            or context.homes
        )
        rules = DenyRules.build(context)
        assert "~/.ssh" in rules.labels


class TestTheListAsLiterals:
    """The lists compared with a copy written out by hand (`expected_deny`), so that deleting or
    misspelling an entry, or changing one, fails here."""

    def test_the_home_entries(self) -> None:
        assert HOME_DENY == EXPECTED_HOME_DENY

    def test_the_system_entries(self) -> None:
        assert SYSTEM_DENY == EXPECTED_SYSTEM_DENY

    @pytest.mark.parametrize(
        "required",
        [
            "/root",
            "/run/user",
            "/etc/shadow",
            ".config/gh",
            ".config/git/credentials",
            ".local/share/fish/fish_history",
            ".terraformrc",
            ".config/hub",
            ".cache/huggingface/token",
            ".huggingface",
            ".claude",
            ".codex",
            ".lima",
            ".colima",
            ".msmtprc",
            ".config/msmtp",
        ],
    )
    def test_names_these_places(self, required: str) -> None:
        assert required in {*HOME_DENY, *SYSTEM_DENY}

    def test_no_duplicates(self) -> None:
        assert len(set(HOME_DENY)) == len(HOME_DENY)
        assert len(set(SYSTEM_DENY)) == len(SYSTEM_DENY)


def context_for(base: Path, **changes: object) -> DenyContext:
    home = base / "home"
    fields: dict[str, object] = {
        "homes": (str(home),),
        "config_home": str(home / ".config"),
        "data_home": str(home / ".local" / "share"),
        "state_home": str(home / ".local" / "state"),
        "cache_home": str(home / ".cache"),
    }
    return DenyContext.model_validate({**fields, **changes})


class TestSharedPlacesUnderTmp:
    """A read-only bind of a directory still lets a sandbox connect() to the sockets in it, and
    tmux, X11 and ssh-agent sockets live under /tmp (I3)."""

    @pytest.mark.parametrize(
        "path",
        ["/tmp/.X11-unix", "/tmp/.ICE-unix", "/tmp/.XIM-unix", "/tmp/.font-unix"],
    )
    def test_the_desktop_socket_directories(self, world: World, path: str) -> None:
        for spelled in (path, f"{path}/X0", f"{path}/a/b"):
            assert world.rules.check_text(
                StoredPath(path=spelled, identity=FsIdentity(fsid=1, ino=2))
            ), spelled
        assert world.rules.check(resolve(path)) is not None
        assert world.rules.check(resolve(f"{path}/X0")) is not None

    def test_tmux_sockets_of_this_uid(self, tmp_path: Path) -> None:
        rules = DenyRules.build(context_for(tmp_path, uid=4242))
        identity = FsIdentity(fsid=1, ino=2)

        mine = rules.check_text(StoredPath(path="/tmp/tmux-4242/default", identity=identity))
        other = rules.check_text(StoredPath(path="/tmp/tmux-4243", identity=identity))

        assert mine is not None
        assert (mine.entry, mine.relation) == ("/tmp/tmux-<uid>", INSIDE)
        assert other is None

    def test_tmux_tmpdir_when_set(self, tmp_path: Path) -> None:
        (tmp_path / "sockets" / "tmux-1").mkdir(parents=True)
        rules = DenyRules.build(context_for(tmp_path, tmux_tmpdir=str(tmp_path / "sockets")))

        inside = rules.check(resolve(tmp_path / "sockets" / "tmux-1"))
        above = rules.check(resolve(tmp_path / "sockets"))

        assert inside is not None
        assert inside.entry == "$TMUX_TMPDIR"
        assert above is not None
        assert above.entry == "$TMUX_TMPDIR"

    def test_tmux_tmpdir_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        entry = accounts.struct_passwd(
            ("alice", "x", 1000, 1000, "Alice", "/home/alice", "/bin/sh")
        )
        monkeypatch.setattr(accounts, "getpwuid", lambda uid: entry)

        context = DenyContext.for_current_user({"TMUX_TMPDIR": "/var/run/tmux-here/"})

        assert context.tmux_tmpdir == "/var/run/tmux-here"
        assert context.uid == os.getuid()
        assert DenyContext.for_current_user({}).tmux_tmpdir is None

    def test_tmp_itself_is_never_grantable(self, world: World) -> None:
        """By the ancestor rule (it holds protected entries) and as a shared directory."""
        for path in ("/tmp", "/var/tmp"):
            match = world.rules.check(resolve(path))
            assert match is not None, path

    def test_var_tmp_and_what_is_in_it(self, world: World) -> None:
        stored = StoredPath(path="/var/tmp/anything/at/all", identity=FsIdentity(fsid=1, ino=2))

        assert world.rules.check_text(stored) is not None
        assert world.rules.check(resolve("/var/tmp")) is not None

    def test_ancestor_rule_makes_tmp_ungrantable_without_the_sticky_rule(self) -> None:
        """Which of the two refuses /tmp: the entries under it do, whatever its mode."""
        rules = DenyRules.build(
            DenyContext(
                homes=("/nonexistent-home",),
                config_home="/nonexistent-home/.config",
                data_home="/nonexistent-home/.local/share",
                state_home="/nonexistent-home/.local/state",
                cache_home="/nonexistent-home/.cache",
            )
        )

        match = rules.check_text(StoredPath(path="/tmp", identity=FsIdentity(fsid=1, ino=2)))

        assert match is not None
        assert match.relation is ANCESTOR


class TestSharedDirectories:
    def make(self, world: World, name: str, mode: int) -> Path:
        directory = world.base / name
        directory.mkdir()
        directory.chmod(mode)
        return directory

    def test_a_world_writable_sticky_directory_is_refused_whatever_its_name(
        self, world: World
    ) -> None:
        shared = self.make(world, "scratch-space", 0o1777)

        match = world.rules.check(resolve(shared))

        assert match is not None
        assert (match.entry, match.relation) == (SHARED_DIRECTORY, EQUAL)

    def test_what_is_inside_it_is_not_refused_for_that_reason(self, world: World) -> None:
        shared = self.make(world, "scratch-space", 0o1777)
        (shared / "mine").mkdir()

        assert world.rules.check(resolve(shared / "mine")) is None

    @pytest.mark.parametrize("mode", [0o1755, 0o777, 0o755, 0o1770])
    def test_one_property_without_the_other_is_not_a_shared_directory(
        self, world: World, mode: int
    ) -> None:
        directory = self.make(world, f"mode-{mode:o}", mode)

        assert world.rules.check(resolve(directory)) is None

    def test_a_file_with_those_bits_is_not_a_directory(self, world: World) -> None:
        file = world.base / "file"
        file.write_text("x")
        file.chmod(0o1777)

        assert world.rules.check(resolve(file)) is None

    def test_a_symlink_to_one_is_judged_by_its_target(self, world: World) -> None:
        shared = self.make(world, "scratch-space", 0o1777)
        (world.base / "link").symlink_to(shared)

        match = world.rules.check(resolve(world.base / "link"))

        assert match is not None
        assert match.entry == SHARED_DIRECTORY

    def test_the_host_tmp_directories_are_shared_directories(self) -> None:
        for path in ("/tmp", "/var/tmp"):
            resolved = resolve(path)
            if Path(path).stat().st_mode & 0o1002 != 0o1002:
                continue  # a host where it is not (a container with a private /tmp)
            assert resolved.shared_directory, path


XDG_PREFIXES = (".config/", ".local/share/", ".local/state/", ".cache/")


class TestEntriesMovedByTheXdgVariables:
    """The credential stores under ~/.config, ~/.local/share, ~/.local/state and ~/.cache are
    protected where XDG_* point instead, as well as at their defaults (B2)."""

    PREFIXES: ClassVar[dict[str, str]] = {
        ".config/": "config_home",
        ".local/share/": "data_home",
        ".local/state/": "state_home",
        ".cache/": "cache_home",
    }

    @pytest.fixture
    def moved(self, tmp_path: Path) -> tuple[Path, DenyRules]:
        context = context_for(
            tmp_path,
            config_home=str(tmp_path / "elsewhere" / "cfg"),
            data_home=str(tmp_path / "elsewhere" / "data"),
            state_home=str(tmp_path / "elsewhere" / "state"),
            cache_home=str(tmp_path / "elsewhere" / "cache"),
        )
        return tmp_path, DenyRules.build(context)

    @pytest.mark.parametrize(
        "relative",
        [
            r
            for r in HOME_DENY
            if r.startswith((".config/", ".local/share/", ".local/state/", ".cache/"))
        ],
    )
    def test_every_such_entry_at_its_new_place(
        self, moved: tuple[Path, DenyRules], relative: str
    ) -> None:
        base, rules = moved
        for prefix, field in self.PREFIXES.items():
            if relative.startswith(prefix):
                root = {
                    "config_home": "cfg",
                    "data_home": "data",
                    "state_home": "state",
                    "cache_home": "cache",
                }[field]
                path = base / "elsewhere" / root / relative.removeprefix(prefix)
                stored = StoredPath(path=str(path), identity=FsIdentity(fsid=1, ino=2))
                inside = StoredPath(path=f"{path}/x", identity=FsIdentity(fsid=1, ino=2))

                assert rules.check_text(stored) is not None, path
                assert rules.check_text(inside) is not None, path

    def test_the_default_places_are_still_protected(self, moved: tuple[Path, DenyRules]) -> None:
        base, rules = moved
        stored = StoredPath(
            path=str(base / "home" / ".config" / "gcloud"), identity=FsIdentity(fsid=1, ino=2)
        )

        assert rules.check_text(stored) is not None

    def test_labels_name_the_variable(self, moved: tuple[Path, DenyRules]) -> None:
        _, rules = moved

        assert {
            "$XDG_CONFIG_HOME/gcloud",
            "$XDG_DATA_HOME/keyrings",
            "$XDG_STATE_HOME/tumbleweed",
            "$XDG_CACHE_HOME/huggingface/token",
        } <= set(rules.labels)

    def test_an_existing_moved_store_is_matched_by_identity_as_well(self, tmp_path: Path) -> None:
        (tmp_path / "cfg" / "gcloud").mkdir(parents=True)
        rules = DenyRules.build(context_for(tmp_path, config_home=str(tmp_path / "cfg")))

        match = rules.check(resolve(tmp_path / "cfg" / "gcloud" / "credentials.db"))
        parent = rules.check(resolve(tmp_path / "cfg"))

        assert match is not None
        assert match.relation is INSIDE
        assert parent is not None
        assert parent.relation is ANCESTOR
