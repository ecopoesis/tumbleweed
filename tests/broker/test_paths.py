"""Tests for `tumbleweed.broker.paths`: deciding about a path once, then opening it by identity."""

import os
import time
import unicodedata
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.broker.fsutil import can_search_denied_directories, make_fifo, make_socket
from tumbleweed.broker import (
    FsIdentity,
    OpenStrategy,
    PathError,
    PathFault,
    ResolvedPath,
    StoredPath,
    openat2_available,
)
from tumbleweed.broker.fdio import identity_chain
from tumbleweed.broker.paths import components_of, fold_name
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import CanonicalPath

STRATEGIES = [
    pytest.param(OpenStrategy.COMPONENTWISE, id="componentwise"),
    pytest.param(OpenStrategy.AUTO, id="auto"),
    pytest.param(OpenStrategy.OPENAT2, id="openat2", marks=pytest.mark.linux),
]


@pytest.fixture(params=STRATEGIES)
def strategy(request: pytest.FixtureRequest) -> OpenStrategy:
    chosen: OpenStrategy = request.param
    if chosen is OpenStrategy.OPENAT2:
        assert openat2_available(), "a Linux 5.6+ host without a seccomp filter has openat2"
    return chosen


def identity_of_path(path: Path) -> FsIdentity:
    """The identity of a path from `stat` and `statvfs`, independent of the code under test."""
    return FsIdentity(fsid=os.statvfs(path).f_fsid, ino=path.stat().st_ino)


def is_case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("x")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


class TestComponentsOf:
    def test_the_root_has_none(self) -> None:
        assert components_of("/") == ()

    def test_splits_a_normalised_absolute_path(self) -> None:
        assert components_of("/home/user/Photos") == ("home", "user", "Photos")

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "relative/path",
            "./x",
            "//double",
            "/trailing/",
            "/a//b",
            "/a/./b",
            "/a/../b",
            "/..",
            "/a\0b",
            "/a\nb",
            "/a\x1bb",
            "/a\u202eb",  # right-to-left override
            "/a\u200bb",  # zero-width space
            "/a\u2028b",  # line separator
            "/a\udc80b",  # not UTF-8
            "/" + "x" * 256,
            "/" + "/".join(["x" * 255] * 17),
        ],
        ids=lambda text: ascii(text)[:30],
    )
    def test_refuses_what_is_not_a_normalised_path_of_ordinary_names(self, text: str) -> None:
        with pytest.raises(InvalidValueError):
            components_of(text)

    def test_allows_the_joiners_that_real_names_need(self) -> None:
        assert components_of("/home/a\u200db/c\u200cd") == ("home", "a\u200db", "c\u200cd")

    def test_the_limit_is_the_whole_path_not_just_what_follows_the_slash(self) -> None:
        """Names of at most 255 bytes, 16 separators and 4080 bytes of them: 4096 bytes in all."""
        names = ["x" * 255] * 15 + ["y" * 128, "z" * 127]
        fits = "/" + "/".join(names[:-1])  # 4096 - 128 = 3968 bytes
        exact = "/" + "/".join(names)  # 1 + 4096 bytes: one too many
        assert len(exact.encode()) == 4097
        assert len(fits.encode()) <= 4096

        assert components_of(fits) == tuple(names[:-1])
        with pytest.raises(InvalidValueError, match="at most 4096 bytes"):
            components_of(exact)

    def test_the_error_does_not_echo_the_path(self) -> None:
        with pytest.raises(InvalidValueError) as raised:
            components_of("/secret-name/../x")

        assert "secret-name" not in str(raised.value)


class TestFoldName:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (".ssh", ".SSH"),
            (".ssh", ".Ssh"),
            ("\u00e9", "e\u0301"),  # precomposed and decomposed
            ("\u00c9", "e\u0301"),
            (".ssh", ".s\u200dsh"),  # zero-width joiner
            (".ssh", ".s\u200csh"),
            (".ssh", "\uff0e\uff53\uff53\uff48"),  # full-width ".ssh"
            ("\u00df", "SS"),  # German sharp s
            ("\ufb01le", "FILE"),  # the fi ligature
            ("Stra\u00dfe", "STRASSE"),
        ],
    )
    def test_names_a_file_system_may_equate_fold_alike(self, left: str, right: str) -> None:
        assert fold_name(left) == fold_name(right)

    @pytest.mark.parametrize(
        ("left", "right"),
        [(".ssh", ".ssh2"), (".ssh", "ssh"), ("a", "b"), ("Photos", "Photo"), ("", "x")],
    )
    def test_different_names_stay_different(self, left: str, right: str) -> None:
        assert fold_name(left) != fold_name(right)

    def test_is_idempotent_and_normal(self) -> None:
        for name in (".SSH", "\u00dcn\u00ef\u0301code", "\ufb01le", "\uff21\uff22"):
            folded = fold_name(name)
            assert fold_name(folded) == folded
            assert unicodedata.is_normalized("NFKD", folded)


class TestResolve:
    def test_the_identity_is_that_of_the_target(self, tmp_path: Path) -> None:
        (tmp_path / "a" / "b").mkdir(parents=True)

        resolved = ResolvedPath.resolve(str(tmp_path / "a" / "b"))

        assert resolved.text == str(tmp_path / "a" / "b")
        assert resolved.identity == identity_of_path(tmp_path / "a" / "b")

    def test_the_chain_has_the_identity_of_every_directory_on_the_way(self, tmp_path: Path) -> None:
        (tmp_path / "a" / "b").mkdir(parents=True)
        target = tmp_path / "a" / "b"

        resolved = ResolvedPath.resolve(str(target))

        expected = [Path("/"), *reversed(target.parents[:-1]), target]
        assert resolved.chain == tuple(identity_of_path(prefix) for prefix in expected)
        assert resolved.components == target.parts[1:]

    def test_a_missing_path_has_no_identity_but_its_directories_do(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()

        resolved = ResolvedPath.resolve(str(tmp_path / "a" / "missing" / "deeper"))

        assert resolved.identity is None
        assert resolved.chain[-1] == identity_of_path(tmp_path / "a")
        assert resolved.text == str(tmp_path / "a" / "missing" / "deeper")

    def test_dots_and_symlinks_are_resolved_before_the_walk(self, tmp_path: Path) -> None:
        (tmp_path / "real" / "inner").mkdir(parents=True)
        (tmp_path / "link").symlink_to("real")

        resolved = ResolvedPath.resolve(f"{tmp_path}/link/inner/../inner/./")

        assert resolved.text == str(tmp_path / "real" / "inner")
        assert resolved.identity == identity_of_path(tmp_path / "real" / "inner")

    def test_a_very_deep_path_is_resolved_in_bounded_time(self, tmp_path: Path) -> None:
        """The walk is one step per component and a path has at most PATH_MAX of them."""
        limit = os.pathconf("/", "PC_PATH_MAX")
        depth = (min(limit, 4096) - len(str(tmp_path)) - 16) // 2
        deep = tmp_path
        for _ in range(depth):  # one step at a time: `mkdir(parents=True)` recurses per level
            deep = deep / "d"
            deep.mkdir()

        resolved = ResolvedPath.resolve(str(deep))
        started = time.monotonic()
        chain = identity_chain(resolved.components)
        elapsed = time.monotonic() - started

        assert resolved.identity == identity_of_path(deep)
        assert chain == resolved.chain
        assert len(chain) == len(deep.parts)
        # The walk of this module is linear. (`CanonicalPath.resolve`, S01, is not: it joins a
        # growing path once per component, which is why only the walk is timed.)
        assert elapsed < 2

    def test_the_root(self) -> None:
        resolved = ResolvedPath.resolve("/")

        assert resolved.text == "/"
        assert resolved.components == ()
        assert resolved.chain == (identity_of_path(Path("/")),)
        assert resolved.identity == resolved.chain[0]

    @pytest.mark.timeout(10)
    def test_a_fifo_socket_or_device_has_no_identity_and_is_not_opened(
        self, tmp_path: Path
    ) -> None:
        make_fifo(tmp_path / "fifo")
        make_socket(tmp_path / "sock")

        for target in (tmp_path / "fifo", tmp_path / "sock", Path("/dev/null")):
            resolved = ResolvedPath.resolve(str(target))
            assert resolved.identity is None
            assert resolved.chain[-1] == identity_of_path(target.parent)

    @pytest.mark.skipif(not can_search_denied_directories(), reason="root ignores permission bits")
    def test_what_cannot_be_opened_has_no_identity(self, tmp_path: Path) -> None:
        """A directory that can be searched but not read: its children exist, yet it cannot be
        opened, so the chain stops above it."""
        (tmp_path / "locked" / "inner").mkdir(parents=True)
        (tmp_path / "locked").chmod(0o111)
        try:
            resolved = ResolvedPath.resolve(str(tmp_path / "locked" / "inner"))
        finally:
            (tmp_path / "locked").chmod(0o755)

        assert resolved.text == str(tmp_path / "locked" / "inner")
        assert resolved.identity is None
        assert resolved.chain[-1] == identity_of_path(tmp_path)

    @pytest.mark.skipif(not can_search_denied_directories(), reason="root ignores permission bits")
    def test_a_directory_that_cannot_be_searched_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "locked" / "inner").mkdir(parents=True)
        (tmp_path / "locked").chmod(0)
        try:
            with pytest.raises(PathError) as raised:
                ResolvedPath.resolve(str(tmp_path / "locked" / "inner"))
        finally:
            (tmp_path / "locked").chmod(0o755)

        assert raised.value.fault is PathFault.INVALID

    @pytest.mark.parametrize(
        "text",
        [
            "relative",
            "",
            "/a\0b",
            "/a\nb",
            "/a\u202eb",
            "/" + "x" * 5000,
            "/a\udc80",
            "/" + "x" * 256,
        ],
        ids=lambda text: ascii(text)[:20],
    )
    def test_refuses_text_that_is_not_an_acceptable_path(self, text: str) -> None:
        with pytest.raises(PathError) as raised:
            ResolvedPath.resolve(text)

        assert raised.value.fault is PathFault.INVALID
        assert "xxxx" not in str(raised.value)

    @pytest.mark.parametrize("bad", [b"/x", 5, None, Path("/x")])
    def test_refuses_what_is_not_a_str(self, bad: object) -> None:
        with pytest.raises(PathError) as raised:
            ResolvedPath.resolve(bad)  # type: ignore[arg-type]  # the point of the test

        assert raised.value.fault is PathFault.INVALID

    def test_a_symlink_that_slips_in_after_canonicalising_is_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Canonicalise says "no symlink", then one is planted before the walk."""
        (tmp_path / "dir").mkdir()
        (tmp_path / "other").mkdir()
        canonical = CanonicalPath.resolve(str(tmp_path / "dir"))
        (tmp_path / "dir").rmdir()
        (tmp_path / "dir").symlink_to(tmp_path / "other")
        monkeypatch.setattr(CanonicalPath, "resolve", classmethod(lambda cls, text: canonical))

        with pytest.raises(PathError) as raised:
            ResolvedPath.resolve(str(tmp_path / "dir"))

        assert raised.value.fault is PathFault.SYMLINK

    def test_a_loop_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "a").symlink_to("b")
        (tmp_path / "b").symlink_to("a")

        with pytest.raises(PathError) as raised:
            ResolvedPath.resolve(str(tmp_path / "a" / "x"))

        assert raised.value.fault is PathFault.INVALID

    def test_equal_and_hashable_by_value(self, tmp_path: Path) -> None:
        first = ResolvedPath.resolve(str(tmp_path))
        second = ResolvedPath.resolve(str(tmp_path))

        assert first == second
        assert hash(first) == hash(second)
        assert first != ResolvedPath.resolve(str(tmp_path.parent))

    def test_equality_ignores_what_has_already_been_worked_out(self, tmp_path: Path) -> None:
        """The folded text, components and identity set are computed on first use and kept."""
        warm = ResolvedPath.resolve(str(tmp_path))
        cold = ResolvedPath.resolve(str(tmp_path))
        assert len(warm.folded) == len(warm.components) == len(warm.chain_set) - 1

        assert warm == cold
        assert hash(warm) == hash(cold)

    def test_the_constructor_holds_a_canonical_path_only(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            ResolvedPath.model_validate({"canonical": str(tmp_path), "chain": []})
        with pytest.raises(ValidationError):
            ResolvedPath.model_validate_json('{"canonical": "/tmp", "chain": []}')

    def test_a_case_variant_has_the_identity_of_the_original(self, tmp_path: Path) -> None:
        """S01 review (#16): resolve('/Applications') != resolve('/applications'), yet the same
        directory. Text cannot tell; identity can. Runs where the volume is case-insensitive
        (macOS by default, so in the macOS CI job); the Linux container's is case-sensitive."""
        if not is_case_insensitive(tmp_path):
            pytest.skip("this volume is case-sensitive; macOS (APFS default) is where this matters")
        (tmp_path / "Photos").mkdir()

        upper = ResolvedPath.resolve(str(tmp_path / "Photos"))
        lower = ResolvedPath.resolve(str(tmp_path / "photos"))

        assert upper.text != lower.text
        assert upper.identity == lower.identity
        assert lower.below(StoredPath.record(upper)) == ()


class TestBelow:
    def test_a_path_in_the_directory_is_below_it(self, tmp_path: Path) -> None:
        (tmp_path / "Photos" / "2026").mkdir(parents=True)
        root = StoredPath.record(ResolvedPath.resolve(str(tmp_path / "Photos")))

        inside = ResolvedPath.resolve(str(tmp_path / "Photos" / "2026" / "a.jpg"))

        assert inside.below(root) == ("2026", "a.jpg")

    def test_the_root_itself_is_below_with_no_names(self, tmp_path: Path) -> None:
        (tmp_path / "Photos").mkdir()
        resolved = ResolvedPath.resolve(str(tmp_path / "Photos"))

        assert resolved.below(StoredPath.record(resolved)) == ()

    def test_a_sibling_and_a_parent_are_not_below(self, tmp_path: Path) -> None:
        (tmp_path / "Photos").mkdir()
        (tmp_path / "Photos2").mkdir()
        root = StoredPath.record(ResolvedPath.resolve(str(tmp_path / "Photos")))

        assert ResolvedPath.resolve(str(tmp_path / "Photos2")).below(root) is None
        assert ResolvedPath.resolve(str(tmp_path)).below(root) is None

    def test_a_missing_path_below_a_directory_is_below_it(self, tmp_path: Path) -> None:
        (tmp_path / "Photos").mkdir()
        root = StoredPath.record(ResolvedPath.resolve(str(tmp_path / "Photos")))

        assert ResolvedPath.resolve(str(tmp_path / "Photos" / "new.jpg")).below(root) == (
            "new.jpg",
        )

    def test_a_directory_replaced_by_another_of_the_same_name_is_not_the_one_granted(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "Photos").mkdir()
        root = StoredPath.record(ResolvedPath.resolve(str(tmp_path / "Photos")))
        (tmp_path / "Photos").rename(tmp_path / "moved")
        (tmp_path / "Photos").mkdir()
        (tmp_path / "Photos" / "a.jpg").write_text("x")

        replaced = ResolvedPath.resolve(str(tmp_path / "Photos" / "a.jpg"))
        moved = ResolvedPath.resolve(str(tmp_path / "moved"))

        assert replaced.below(root) is None  # same name, not the granted directory
        assert moved.below(root) == ()  # the granted directory, under its new name

    def test_a_hard_link_to_a_granted_file_is_the_granted_file(self, tmp_path: Path) -> None:
        (tmp_path / "doc").write_text("x")
        os.link(tmp_path / "doc", tmp_path / "alias")
        root = StoredPath.record(ResolvedPath.resolve(str(tmp_path / "doc")))

        assert ResolvedPath.resolve(str(tmp_path / "alias")).below(root) == ()


class TestStoredPath:
    def test_records_the_text_and_identity_of_a_decided_path(self, tmp_path: Path) -> None:
        (tmp_path / "Photos").mkdir()

        stored = StoredPath.record(ResolvedPath.resolve(str(tmp_path / "Photos")))

        assert stored.path == str(tmp_path / "Photos")
        assert stored.identity == identity_of_path(tmp_path / "Photos")
        assert stored.components == (tmp_path / "Photos").parts[1:]

    def test_a_target_that_does_not_exist_cannot_be_recorded(self, tmp_path: Path) -> None:
        with pytest.raises(PathError) as raised:
            StoredPath.record(ResolvedPath.resolve(str(tmp_path / "missing")))

        assert raised.value.fault is PathFault.MISSING

    def test_survives_a_json_round_trip(self, tmp_path: Path) -> None:
        stored = StoredPath.record(ResolvedPath.resolve(str(tmp_path)))

        assert StoredPath.model_validate_json(stored.model_dump_json()) == stored

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "relative",
            "/a/../b",
            "/a//b",
            "/a/",
            "/a/./b",
            "/a\0b",
            "/a\u202eb",
            "/" + "x" * 5000,
        ],
        ids=lambda text: ascii(text)[:20],
    )
    def test_refuses_text_that_is_not_a_normalised_absolute_path(self, text: str) -> None:
        with pytest.raises(ValidationError):
            StoredPath(path=text, identity=FsIdentity(fsid=1, ino=2))

    def test_is_strict_closed_and_frozen(self) -> None:
        identity = FsIdentity(fsid=1, ino=2)
        stored = StoredPath(path="/a", identity=identity)

        with pytest.raises(ValidationError):
            StoredPath.model_validate({"path": "/a", "identity": identity, "extra": 1})
        with pytest.raises(ValidationError):
            StoredPath.model_validate({"path": b"/a", "identity": identity})
        with pytest.raises(ValidationError):
            StoredPath.model_validate_json('{"path": "/a", "identity": {"fsid": -1, "ino": 2}}')
        with pytest.raises(ValidationError):
            StoredPath.model_validate_json(
                '{"path": "/a", "identity": {"fsid": 1, "ino": 18446744073709551616}}'
            )
        with pytest.raises(ValidationError):
            stored.path = "/b"  # type: ignore[misc]  # the point of the test

    def test_identity_holds_unsigned_64_bit_values(self) -> None:
        big = FsIdentity(fsid=2**64 - 1, ino=2**63)

        assert (
            StoredPath.model_validate_json(
                StoredPath(path="/a", identity=big).model_dump_json()
            ).identity
            == big
        )


class TestOpen:
    @pytest.fixture
    def stored(self, tmp_path: Path) -> StoredPath:
        (tmp_path / "grant" / "inner").mkdir(parents=True)
        (tmp_path / "grant" / "file.txt").write_text("hello")
        return StoredPath.record(ResolvedPath.resolve(str(tmp_path / "grant")))

    def test_opens_what_was_granted(self, stored: StoredPath, strategy: OpenStrategy) -> None:
        with stored.open(strategy) as opened:
            assert opened.identity == stored.identity
            assert sorted(entry.name for entry in os.scandir(opened.fd)) == ["file.txt", "inner"]

    def test_opens_a_granted_file(self, tmp_path: Path, strategy: OpenStrategy) -> None:
        (tmp_path / "doc.txt").write_text("hello")
        stored = StoredPath.record(ResolvedPath.resolve(str(tmp_path / "doc.txt")))

        with stored.open(strategy) as opened:
            assert os.pread(opened.fd, 10, 0) == b"hello"

    def test_a_symlink_swapped_in_after_the_grant_makes_the_run_refuse(
        self, tmp_path: Path, stored: StoredPath, strategy: OpenStrategy
    ) -> None:
        """The acceptance test: the user granted `grant`, then `grant` became a symlink to
        somewhere else. Following it would redirect the run; the open refuses instead."""
        attacker = tmp_path / "attacker"
        attacker.mkdir()
        (tmp_path / "grant").rename(tmp_path / "grant-moved")
        (tmp_path / "grant").symlink_to(attacker)

        with pytest.raises(PathError) as raised:
            stored.open(strategy)

        assert raised.value.fault is PathFault.SYMLINK

    def test_a_symlink_back_to_the_very_same_directory_is_refused_too(
        self, tmp_path: Path, stored: StoredPath, strategy: OpenStrategy
    ) -> None:
        """Even where following it would reach the granted object: no symlink is ever followed."""
        (tmp_path / "grant").rename(tmp_path / "grant-moved")
        (tmp_path / "grant").symlink_to(tmp_path / "grant-moved")

        with pytest.raises(PathError) as raised:
            stored.open(strategy)

        assert raised.value.fault is PathFault.SYMLINK

    def test_a_symlink_swapped_in_for_a_parent_is_refused(
        self, tmp_path: Path, strategy: OpenStrategy
    ) -> None:
        (tmp_path / "parent" / "grant").mkdir(parents=True)
        stored = StoredPath.record(ResolvedPath.resolve(str(tmp_path / "parent" / "grant")))
        (tmp_path / "parent").rename(tmp_path / "parent-moved")
        (tmp_path / "parent").symlink_to(tmp_path / "parent-moved")

        with pytest.raises(PathError) as raised:
            stored.open(strategy)

        assert raised.value.fault is PathFault.SYMLINK

    def test_a_directory_replaced_by_another_is_refused_by_identity(
        self, tmp_path: Path, stored: StoredPath, strategy: OpenStrategy
    ) -> None:
        (tmp_path / "grant").rename(tmp_path / "grant-moved")
        (tmp_path / "grant").mkdir()

        with pytest.raises(PathError) as raised:
            stored.open(strategy)

        assert raised.value.fault is PathFault.IDENTITY_MISMATCH

    def test_a_directory_replaced_by_a_file_is_refused(
        self, tmp_path: Path, stored: StoredPath, strategy: OpenStrategy
    ) -> None:
        (tmp_path / "grant").rename(tmp_path / "grant-moved")
        (tmp_path / "grant").write_text("not a directory")

        with pytest.raises(PathError) as raised:
            stored.open(strategy)

        assert raised.value.fault is PathFault.IDENTITY_MISMATCH

    def test_a_deleted_directory_is_refused(
        self, tmp_path: Path, stored: StoredPath, strategy: OpenStrategy
    ) -> None:
        for child in (tmp_path / "grant").rglob("*"):
            if child.is_file():
                child.unlink()
        (tmp_path / "grant" / "inner").rmdir()
        (tmp_path / "grant").rmdir()

        with pytest.raises(PathError) as raised:
            stored.open(strategy)

        assert raised.value.fault is PathFault.MISSING

    def test_the_other_filesystem_is_not_the_one_granted(
        self, stored: StoredPath, strategy: OpenStrategy
    ) -> None:
        elsewhere = StoredPath(
            path=stored.path,
            identity=FsIdentity(fsid=stored.identity.fsid ^ 1, ino=stored.identity.ino),
        )

        with pytest.raises(PathError) as raised:
            elsewhere.open(strategy)

        assert raised.value.fault is PathFault.IDENTITY_MISMATCH

    def test_a_grant_for_a_special_file_never_opens_it(
        self, tmp_path: Path, strategy: OpenStrategy
    ) -> None:
        make_fifo(tmp_path / "fifo")
        forged = StoredPath(path=str(tmp_path / "fifo"), identity=FsIdentity(fsid=1, ino=2))

        with pytest.raises(PathError) as raised:
            forged.open(strategy)

        assert raised.value.fault is PathFault.SPECIAL_FILE

    def test_a_refusal_closes_what_it_opened(
        self, tmp_path: Path, stored: StoredPath, strategy: OpenStrategy
    ) -> None:
        before = len(list(Path("/dev/fd").iterdir()))
        (tmp_path / "grant").rename(tmp_path / "grant-moved")
        (tmp_path / "grant").mkdir()
        for _ in range(20):
            with pytest.raises(PathError):
                stored.open(strategy)

        assert len(list(Path("/dev/fd").iterdir())) == before

    def test_never_resolves_the_stored_text_again(
        self, stored: StoredPath, strategy: OpenStrategy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolving would follow a swapped symlink. Any call that resolves text fails here."""

        def forbidden(*args: object, **kwargs: object) -> str:
            raise AssertionError("the stored path must not be resolved again")

        monkeypatch.setattr(Path, "resolve", forbidden)
        monkeypatch.setattr(os.path, "realpath", forbidden)
        monkeypatch.setattr(os.path, "abspath", forbidden)
        monkeypatch.setattr(os, "readlink", forbidden)
        monkeypatch.setattr(CanonicalPath, "resolve", forbidden)
        monkeypatch.setattr(ResolvedPath, "resolve", forbidden)

        with stored.open(strategy) as opened:
            assert opened.identity == stored.identity

    def test_has_no_way_to_resolve_itself(self) -> None:
        public = {name for name in dir(StoredPath) if not name.startswith("_")}

        assert not {name for name in public if "resolve" in name or "canonical" in name}
