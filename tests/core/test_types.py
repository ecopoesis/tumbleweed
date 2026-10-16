"""Tests for the domain primitives in `tumbleweed.core.types`."""

import copy
import errno
import hashlib
import os
import pickle
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, ClassVar

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import TaskId, ToolId
from tumbleweed.core.types import (
    ByteCount,
    CanonicalPath,
    ContentHash,
    JsonValue,
    ModelName,
    ProviderRequestId,
    ToolVersion,
    WorkspaceRelPath,
)

if TYPE_CHECKING:
    from collections.abc import Callable

SHA256_OF_EMPTY = hashlib.sha256(b"").hexdigest()


INT64_MAX = 2**63 - 1

NOT_INTS: list[object] = [
    1.5,
    5.0,
    float("nan"),
    Decimal(5),
    "5",
    b"5",
    None,
    True,
    False,
    [5],
    (5,),
    {5},
    object(),
    complex(5),
]


class TestByteCount:
    @pytest.mark.parametrize("value", [0, 1, 1024, 2**53, INT64_MAX])
    def test_accepts_zero_up_to_int64(self, value: int) -> None:
        assert ByteCount(value) == value
        assert isinstance(ByteCount(value), int)

    @pytest.mark.parametrize("value", [-1, -(2**63), INT64_MAX + 1, 2**64, 10**30])
    def test_rejects_out_of_range(self, value: int) -> None:
        with pytest.raises(
            InvalidValueError, match="ByteCount must be from 0 to 9223372036854775807"
        ):
            ByteCount(value)

    @pytest.mark.parametrize(
        "value",
        [10**5000, -(10**5000), 2**100_000],
        ids=["10**5000", "-(10**5000)", "2**100_000"],
    )
    def test_a_huge_int_is_an_invalid_value_with_a_short_message(self, value: int) -> None:
        """str() of an int this big raises ValueError itself; it must not leak out."""
        with pytest.raises(InvalidValueError) as raised:
            ByteCount(value)

        assert len(str(raised.value)) < 150
        assert "bits" in str(raised.value)

    @pytest.mark.parametrize("bad", NOT_INTS, ids=lambda v: type(v).__name__ + repr(v)[:12])
    def test_only_a_real_int_is_accepted(self, bad: object) -> None:
        with pytest.raises(InvalidValueError, match="ByteCount must be an int"):
            ByteCount(bad)  # type: ignore[arg-type]  # the point of the test

    def test_works_where_an_int_is_expected(self) -> None:
        limit = ByteCount(4)

        assert len(b"abc") <= limit
        assert b"abcdef"[:limit] == b"abcd"
        assert ByteCount(3) + ByteCount(4) == 7  # arithmetic gives a plain int, to be rewrapped

    def test_has_no_instance_dict(self) -> None:
        assert not hasattr(ByteCount(1), "__dict__")


class TestToolVersion:
    @pytest.mark.parametrize("value", [1, 2, 10**9, INT64_MAX])
    def test_accepts_positive_up_to_int64(self, value: int) -> None:
        assert ToolVersion(value) == value

    @pytest.mark.parametrize("value", [0, -1, INT64_MAX + 1, 10**30])
    def test_rejects_zero_negative_and_too_big(self, value: int) -> None:
        with pytest.raises(InvalidValueError, match="ToolVersion must be from 1 to"):
            ToolVersion(value)

    @pytest.mark.parametrize("bad", NOT_INTS, ids=lambda v: type(v).__name__ + repr(v)[:12])
    def test_only_a_real_int_is_accepted(self, bad: object) -> None:
        with pytest.raises(InvalidValueError, match="ToolVersion must be an int"):
            ToolVersion(bad)  # type: ignore[arg-type]  # the point of the test

    def test_orders_like_a_number(self) -> None:
        assert ToolVersion(1) < ToolVersion(2)
        assert sorted([ToolVersion(3), ToolVersion(1)]) == [1, 3]


class TestNumbersAreDistinctOnlyToTheTypeChecker:
    """Documented limit of building the primitives on `int`: unlike entity IDs, they are ints."""

    def test_different_int_primitives_with_the_same_value_compare_equal_at_runtime(self) -> None:
        # Typed as object: mypy itself flags `ByteCount(5) == ToolVersion(5)` as non-overlapping,
        # which is the static half of "distinct"; this is the runtime half.
        count: object = ByteCount(5)
        version: object = ToolVersion(5)

        assert count == version
        assert hash(count) == hash(version)
        assert {count, version} == {5}

    def test_a_primitive_equals_the_plain_value_it_wraps(self) -> None:
        assert ByteCount(5) == 5
        assert ContentHash("0" * 64) == "0" * 64
        assert ModelName("claude-sonnet-5-5") == "claude-sonnet-5-5"

    def test_entity_ids_are_the_contrast(self) -> None:
        assert TaskId.new() != ToolId.new()
        assert TaskId("tsk_01j9z3c7q4v8s2k5m6n7p8r9t0") != ToolId("tool_01j9z3c7q4v8s2k5m6n7p8r9t0")

    def test_a_primitive_can_be_rewrapped_as_another_by_constructing_it_explicitly(self) -> None:
        assert ToolVersion(ByteCount(5)) == 5  # validated again, and a conscious conversion


class TestContentHash:
    def test_accepts_a_sha256_hex_digest(self) -> None:
        assert ContentHash(SHA256_OF_EMPTY) == SHA256_OF_EMPTY
        assert ContentHash("0" * 64) == "0" * 64

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "0" * 63,
            "0" * 65,
            "A" * 64,  # uppercase: one spelling only
            "g" * 64,
            "0" * 63 + "\n",
            "0" * 64 + "\n",
            " " + "0" * 63,
            "sha256:" + "0" * 64,
            "\uff10" * 64,  # fullwidth zeros
        ],
    )
    def test_rejects_anything_else(self, bad: str) -> None:
        with pytest.raises(InvalidValueError, match="64 lowercase hex digits"):
            ContentHash(bad)

    def test_the_error_does_not_echo_a_huge_input(self) -> None:
        with pytest.raises(InvalidValueError) as raised:
            ContentHash("x" * 1_000_000)

        assert len(str(raised.value)) < 300


class TestModelName:
    @pytest.mark.parametrize(
        "name",
        [
            "claude-sonnet-5-5",
            "claude-opus-5",
            "anthropic/claude-3-opus",  # a LiteLLM-style provider prefix
            "llama3.1:8b",
            "gpt-4o@2024-08-06",
            "a",
            "A" + "b" * 127,
        ],
    )
    def test_accepts_real_world_names(self, name: str) -> None:
        assert ModelName(name) == name

    @pytest.mark.parametrize(
        "bad",
        ["", "-leading-dash", ".dot", "has space", "new\nline", "semi;colon", "a" * 129, "mödel"],
    )
    def test_rejects_others(self, bad: str) -> None:
        with pytest.raises(InvalidValueError, match="ModelName"):
            ModelName(bad)

    def test_can_be_given_to_code_that_takes_a_str(self) -> None:
        def shout(text: str) -> str:
            return text.upper()

        assert shout(ModelName("claude-sonnet-5-5")) == "CLAUDE-SONNET-5-5"


class TestProviderRequestId:
    @pytest.mark.parametrize("value", ["req_011CS", "a", "x" * 256, "abc-DEF_123.456:789/~", "!~"])
    def test_accepts_opaque_printable_ascii(self, value: str) -> None:
        assert ProviderRequestId(value) == value

    @pytest.mark.parametrize(
        "bad", ["", "x" * 257, "has space", "tab\t", "new\nline", "\x00", "\x7f", "café"]
    )
    def test_rejects_anything_that_could_forge_a_log_line_or_is_unbounded(self, bad: str) -> None:
        with pytest.raises(InvalidValueError, match="ProviderRequestId"):
            ProviderRequestId(bad)


class TestStringInputTypes:
    """Whatever is passed in, a constructor answers with InvalidValueError and nothing else."""

    BAD: ClassVar[list[object]] = [
        123,
        0,
        1.5,
        True,
        None,
        b"0" * 64,
        bytearray(b"x"),
        ["x"],
        {"x": 1},
        object(),
        TaskId.new(),
        10**5000,
    ]

    @pytest.mark.parametrize("cls", [ContentHash, ModelName, ProviderRequestId])
    def test_a_non_str_is_rejected(
        self, cls: type[ContentHash | ModelName | ProviderRequestId]
    ) -> None:
        for bad in self.BAD:
            with pytest.raises(InvalidValueError, match=f"{cls.__name__} must be a str, got"):
                cls(bad)  # type: ignore[arg-type]  # the point of the test

    @pytest.mark.parametrize("bad", BAD, ids=lambda v: type(v).__name__)
    def test_workspace_rel_path_rejects_a_non_str(self, bad: object) -> None:
        with pytest.raises(InvalidValueError, match="WorkspaceRelPath is built from a str"):
            WorkspaceRelPath(bad)  # type: ignore[arg-type]  # the point of the test


class TestWorkspaceRelPath:
    @pytest.mark.parametrize(
        "text",
        [
            "a",
            "a/b",
            "a/b/c.png",
            "out/result 1.png",
            ".hidden",
            "dir/.hidden/file",
            "...",
            "a..b",
            "..a",
            "a..",
            "~",
            "-rf",
            "café/日本語.txt",
            "a\u00a0b",  # a no-break space is an ordinary space (Zs): allowed
            "a\u3000b",  # an ideographic space too
            "emoji \U0001f600.png",
            # U+200C and U+200D are format characters (Cf) but allowed on purpose:
            "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466.png",  # family
            "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645.txt",  # Persian, ZWNJ
            "\u0915\u094d\u200d\u0937.txt",  # Devanagari, ZWJ
            "a\u200cb",
            "a\u200db",
            "\u0e01\u0e33.txt",  # Thai, with a combining vowel (Mn, Mc): allowed
            "a\\b",  # a backslash is an ordinary file name character on Linux
            "x" * 255,
            "/".join(["d"] * 100),
        ],
    )
    def test_accepts_clean_relative_paths(self, text: str) -> None:
        assert str(WorkspaceRelPath(text)) == text

    @pytest.mark.parametrize(
        ("text", "why"),
        [
            ("", "empty"),
            ("/", "relative"),
            ("/etc/passwd", "relative"),
            ("//host/share", "relative"),
            (".", "empty, '.' or '..'"),
            ("..", "empty, '.' or '..'"),
            ("../x", "empty, '.' or '..'"),
            ("a/..", "empty, '.' or '..'"),
            ("a/../b", "empty, '.' or '..'"),
            ("a/./b", "empty, '.' or '..'"),
            ("./a", "empty, '.' or '..'"),
            ("a//b", "empty, '.' or '..'"),
            ("a/", "empty, '.' or '..'"),
            ("a\x00b", "control, format or line-separator"),
            ("\x00", "control, format or line-separator"),
            ("a\nb", "control, format or line-separator"),
            ("a\tb", "control, format or line-separator"),
            ("a\x1bb", "control, format or line-separator"),
            ("a\x7fb", "control, format or line-separator"),
            ("a\x9bb", "control, format or line-separator"),  # C1: CSI, starts an ANSI sequence
            ("a\x85b", "control, format or line-separator"),  # C1: NEL, a line break
            ("a\x80b", "control, format or line-separator"),
            ("a\x9fb", "control, format or line-separator"),
            ("gnp.\u202eexe", "control, format or line-separator"),  # RLO: reads as exe.png
            ("a\u2066b", "control, format or line-separator"),  # LRI, a bidi isolate
            ("a\u200bb", "control, format or line-separator"),  # zero-width space
            ("a\u2060b", "control, format or line-separator"),  # word joiner
            ("a\u200eb", "control, format or line-separator"),  # left-to-right mark
            ("a\U000e0041b", "control, format or line-separator"),  # tag character
            ("a\ufeffb", "control, format or line-separator"),  # BOM / zero-width no-break
            ("a\u00adb", "control, format or line-separator"),  # soft hyphen
            ("a\u2028b", "control, format or line-separator"),  # line separator (Zl)
            ("a\u2029b", "control, format or line-separator"),  # paragraph separator (Zp)
            ("\u202e", "control, format or line-separator"),
            ("d/a\u202ee/f", "control, format or line-separator"),
            ("\ud800", "UTF-8"),
            ("a/\udfffb", "UTF-8"),
            ("x" * 256, "component is over 255 bytes"),
            ("é" * 128, "component is over 255 bytes"),  # 256 bytes in UTF-8
            ("/".join(["d" * 100] * 41), "over 4096 bytes"),
        ],
    )
    def test_rejects_traversal_absolute_control_and_oversize(self, text: str, why: str) -> None:
        with pytest.raises(InvalidValueError, match=why):
            WorkspaceRelPath(text)

    def test_the_byte_limits_are_inclusive(self) -> None:
        at_name_limit = "\u00e9" * 127 + "x"  # 255 bytes
        assert len(at_name_limit.encode()) == 255
        assert str(WorkspaceRelPath(at_name_limit)) == at_name_limit
        # 40 components of 99 bytes, 39 separators, one more component of the remaining bytes.
        exactly = "/".join(["d" * 99] * 40) + "/" + "d" * (4096 - (40 * 99 + 40))
        assert len(exactly.encode()) == 4096
        assert str(WorkspaceRelPath(exactly)) == exactly
        with pytest.raises(InvalidValueError, match="over 4096 bytes"):
            WorkspaceRelPath(exactly + "d")

    def test_is_not_a_string_or_a_path(self) -> None:
        path: object = WorkspaceRelPath("a/b")

        assert not isinstance(path, str | os.PathLike)
        assert path != "a/b"
        assert path != PurePosixPath("a/b")
        with pytest.raises(TypeError):
            os.fspath(path)  # type: ignore[call-overload]  # the point of the test

    def test_equality_hash_and_order_follow_the_text(self) -> None:
        a, a2, b = WorkspaceRelPath("a"), WorkspaceRelPath("a"), WorkspaceRelPath("b")

        assert a == a2
        assert hash(a) == hash(a2)
        assert a != b
        assert a < b
        assert len({a, a2, b}) == 2

    def test_a_workspace_path_never_equals_a_canonical_path_with_the_same_text(self) -> None:
        relative = WorkspaceRelPath("tmp")
        canonical = CanonicalPath.resolve("/")

        assert relative != canonical
        assert str(relative) != str(canonical)

    def test_is_immutable_copyable_and_not_picklable(self) -> None:
        path = WorkspaceRelPath("a/b")

        with pytest.raises(AttributeError, match="immutable"):
            path.value = "c"
        assert copy.deepcopy(path) is path
        with pytest.raises(TypeError, match="cannot be pickled"):
            pickle.dumps(path)

    def test_repr_shows_the_text(self) -> None:
        assert repr(WorkspaceRelPath("a/b")) == "WorkspaceRelPath('a/b')"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A real directory tree with symlinks, rooted at the canonical form of tmp_path.

    The root is canonical already because macOS puts temp dirs behind a /var symlink.
    """
    root = tmp_path.resolve()
    (root / "real" / "sub").mkdir(parents=True)
    (root / "real" / "file.txt").write_text("x")
    (root / "link").symlink_to(root / "real")
    (root / "rel-link").symlink_to("real/sub")
    (root / "chain1").symlink_to(root / "chain2")
    (root / "chain2").symlink_to(root / "link")
    (root / "loop-a").symlink_to("loop-b")
    (root / "loop-b").symlink_to("loop-a")
    (root / "dangling").symlink_to(root / "does-not-exist")
    return root


class TestCanonicalPath:
    def test_an_already_canonical_path_is_unchanged(self, tree: Path) -> None:
        assert str(CanonicalPath.resolve(tree / "real" / "sub")) == str(tree / "real" / "sub")

    def test_the_root_is_canonical(self) -> None:
        assert str(CanonicalPath.resolve("/")) == "/"

    def test_symlinks_are_resolved(self, tree: Path) -> None:
        assert str(CanonicalPath.resolve(tree / "link")) == str(tree / "real")
        assert str(CanonicalPath.resolve(tree / "link" / "file.txt")) == str(
            tree / "real" / "file.txt"
        )
        assert str(CanonicalPath.resolve(tree / "rel-link")) == str(tree / "real" / "sub")

    def test_chains_of_symlinks_are_followed_to_the_end(self, tree: Path) -> None:
        assert str(CanonicalPath.resolve(tree / "chain1")) == str(tree / "real")

    def test_dot_and_dotdot_and_repeated_slashes_are_normalised(self, tree: Path) -> None:
        messy = f"{tree}//real/./sub/../sub/"

        assert str(CanonicalPath.resolve(messy)) == str(tree / "real" / "sub")

    def test_dotdot_applies_to_the_resolved_target_not_the_link(self, tree: Path) -> None:
        # link -> real, so link/sub/.. is real (and not the directory containing "link").
        assert str(CanonicalPath.resolve(tree / "link" / "sub" / "..")) == str(tree / "real")

    def test_dotdot_cannot_climb_above_the_root(self) -> None:
        assert str(CanonicalPath.resolve("/../../..")) == "/"

    def test_a_path_that_does_not_exist_yet_keeps_its_remaining_components(
        self, tree: Path
    ) -> None:
        assert str(CanonicalPath.resolve(tree / "link" / "new" / "dir")) == str(
            tree / "real" / "new" / "dir"
        )

    def test_a_dangling_symlink_resolves_to_its_target(self, tree: Path) -> None:
        assert str(CanonicalPath.resolve(tree / "dangling")) == str(tree / "does-not-exist")

    def test_resolving_is_idempotent(self, tree: Path) -> None:
        once = CanonicalPath.resolve(tree / "chain1" / "sub" / ".." / "sub")

        assert CanonicalPath.resolve(str(once)) == once

    def test_accepts_str_and_os_pathlike(self, tree: Path) -> None:
        assert CanonicalPath.resolve(str(tree)) == CanonicalPath.resolve(tree)
        assert CanonicalPath.resolve(PurePosixPath(tree)) == CanonicalPath.resolve(tree)

    @pytest.mark.parametrize("relative", ["", ".", "..", "a", "a/b", "./a", "../a", "~", "~/x"])
    def test_a_relative_path_is_refused_never_resolved_against_the_cwd(self, relative: str) -> None:
        with pytest.raises(InvalidValueError, match="must be absolute"):
            CanonicalPath.resolve(relative)

    @pytest.mark.parametrize("text", ["/srv/a\x00b", "\x00", "/\x00"])
    def test_a_nul_byte_is_refused(self, text: str) -> None:
        with pytest.raises(InvalidValueError, match="NUL"):
            CanonicalPath.resolve(text)

    @pytest.mark.parametrize("tail", ["", "/x", "/x/y/z"])
    @pytest.mark.parametrize("loop", ["loop-a", "loop-b"])
    def test_a_symlink_loop_is_refused_on_every_python_version(
        self, tree: Path, loop: str, tail: str
    ) -> None:
        """Python 3.12 raises for a loop; 3.13 and later return it unresolved. Both are refused."""
        with pytest.raises(InvalidValueError, match="cannot canonicalise"):
            CanonicalPath.resolve(f"{tree}/{loop}{tail}")

    def test_a_symlink_left_in_the_result_is_refused(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What Path.resolve does on 3.12 for an unsearchable parent, and on 3.13+ for a loop."""
        monkeypatch.setattr(Path, "resolve", lambda self, strict=False: self)

        with pytest.raises(InvalidValueError, match="'link' is a symlink that could not be"):
            CanonicalPath.resolve(tree / "link" / "file.txt")

    def test_a_symlink_at_the_end_of_the_result_is_refused_too(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "resolve", lambda self, strict=False: self)

        with pytest.raises(InvalidValueError, match="'dangling' is a symlink"):
            CanonicalPath.resolve(tree / "dangling")

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root can search any directory, so there is no EACCES to provoke",
    )
    def test_a_directory_that_cannot_be_searched_is_refused(self, tree: Path) -> None:
        """Python 3.12's resolve() returns the path unresolved when it may not look inside."""
        locked = tree / "locked"
        locked.mkdir()
        (locked / "inner").symlink_to(tree / "real")
        locked.chmod(0)
        try:
            with pytest.raises(
                InvalidValueError, match="cannot inspect 'inner': Permission denied"
            ):
                CanonicalPath.resolve(locked / "inner" / "x")
        finally:
            locked.chmod(0o700)  # so that the temporary directory can be removed

    @pytest.mark.parametrize("code", [errno.EACCES, errno.EIO, errno.ENAMETOOLONG, errno.ELOOP])
    def test_any_lstat_failure_but_absence_is_refused(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        def failing_lstat(self: Path) -> os.stat_result:
            raise OSError(code, os.strerror(code), str(self))

        monkeypatch.setattr(Path, "lstat", failing_lstat)

        with pytest.raises(InvalidValueError, match=os.strerror(code)):
            CanonicalPath.resolve(tree / "real")

    @pytest.mark.parametrize("code", [errno.ENOENT, errno.ENOTDIR])
    def test_absence_is_not_a_failure(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        def absent(self: Path) -> os.stat_result:
            raise OSError(code, os.strerror(code), str(self))

        monkeypatch.setattr(Path, "lstat", absent)

        assert str(CanonicalPath.resolve(tree / "real")) == str(tree / "real")

    @pytest.mark.parametrize(
        "surrogate",
        # Lone surrogates, and U+DC80..U+DCFF, which is how Python spells a file name byte that
        # is not UTF-8 (os.fsdecode(b"\xff") is chr(0xDCFF)).
        [chr(0xD800), chr(0xDBFF), chr(0xDC80), chr(0xDCFF), chr(0xDFFF)],
        ids=lambda c: hex(ord(c)),
    )
    def test_a_surrogate_is_refused_not_a_crash(self, tree: Path, surrogate: str) -> None:
        for text in (f"/{surrogate}", f"{tree}/real/a{surrogate}b", f"{tree}/{surrogate}/x"):
            with pytest.raises(InvalidValueError, match="not valid UTF-8") as raised:
                CanonicalPath.resolve(text)
            assert len(str(raised.value)) < 300

    def test_a_file_name_byte_that_is_not_utf8_is_refused(self, tree: Path) -> None:
        text = f"{tree}/real/" + os.fsdecode(b"bad-\xff-name")  # surrogate-escaped on this host

        with pytest.raises(InvalidValueError, match="not valid UTF-8"):
            CanonicalPath.resolve(text)

    def test_a_result_that_is_not_utf8_is_refused_too(
        self, tree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A symlink target can hold bytes that are not UTF-8: refuse what resolve() returns."""
        monkeypatch.setattr(Path, "resolve", lambda self, strict=False: Path("/srv/\udcff"))

        with pytest.raises(InvalidValueError, match="not valid UTF-8"):
            CanonicalPath.resolve(tree / "real")

    def test_whatever_resolve_returns_can_be_dumped_to_json(self, tree: Path) -> None:
        adapter = TypeAdapter(CanonicalPath)

        for given in (tree, tree / "link", tree / "not" / "yet", Path("/")):
            canonical = CanonicalPath.resolve(given)
            assert adapter.dump_json(canonical) == f'"{canonical}"'.encode()
            str(canonical).encode("utf-8")  # and is valid UTF-8, with no surrogate

    def test_a_too_long_name_is_refused_deterministically(self, tree: Path) -> None:
        with pytest.raises(InvalidValueError, match="File name too long"):
            CanonicalPath.resolve(tree / ("x" * 5000))

    def test_error_messages_never_carry_the_whole_path(self, tree: Path) -> None:
        tail = "y" * 300
        for text in (
            f"{tree}/loop-a/{tail}",
            f"{tree}/{tail}\x00",
            f"{tree}/{tail}/\ud800",
            f"{tail}/relative",
        ):
            with pytest.raises(InvalidValueError) as raised:
                CanonicalPath.resolve(text)
            assert tail not in str(raised.value)
            assert len(str(raised.value)) < 400
            # The original OSError, or whatever was caught, repeats the full path: not chained.
            assert raised.value.__cause__ is None
            assert raised.value.__context__ is None or raised.value.__suppress_context__

    def test_no_symlink_survives_in_anything_resolve_returns(self, tree: Path) -> None:
        inputs = [
            tree / "link",
            tree / "link" / "sub" / "..",
            tree / "rel-link" / ".." / "file.txt",
            tree / "chain1" / "sub",
            tree / "dangling",
            tree / "link" / "new" / "dir",
            tree / "real" / ".." / "link" / "file.txt",
        ]
        for given in inputs:
            resolved = Path(str(CanonicalPath.resolve(given)))
            walked = Path("/")
            for part in resolved.parts[1:]:
                walked /= part
                if walked.exists() or walked.is_symlink():
                    assert not walked.is_symlink(), f"{given} resolved to {resolved}"

    def test_there_is_no_constructor_from_text(self) -> None:
        with pytest.raises(TypeError, match=r"CanonicalPath\.resolve"):
            CanonicalPath()
        with pytest.raises(TypeError):
            CanonicalPath("/srv")  # type: ignore[call-arg]  # the point of the test

    def test_is_not_a_string_or_a_path_so_it_cannot_be_opened_by_accident(self, tree: Path) -> None:
        canonical: object = CanonicalPath.resolve(tree)

        assert not isinstance(canonical, str | os.PathLike)
        assert canonical != str(tree)
        assert canonical != tree
        with pytest.raises(TypeError):
            open(canonical)  # type: ignore[call-overload]  # noqa: SIM115, PTH123  (the point)

    def test_str_is_the_path(self, tree: Path) -> None:
        assert str(CanonicalPath.resolve(tree)) == str(tree)
        assert repr(CanonicalPath.resolve("/")) == "CanonicalPath('/')"

    def test_equality_hash_and_order_follow_the_path(self, tree: Path) -> None:
        a = CanonicalPath.resolve(tree / "real")
        via_link = CanonicalPath.resolve(tree / "link")
        other = CanonicalPath.resolve(tree / "real" / "sub")

        assert a == via_link
        assert hash(a) == hash(via_link)
        assert a != other
        assert a < other
        assert len({a, via_link, other}) == 2

    def test_is_immutable_copyable_and_not_picklable(self, tree: Path) -> None:
        canonical = CanonicalPath.resolve(tree)

        with pytest.raises(AttributeError, match="immutable"):
            canonical._text = "/etc"
        assert copy.copy(canonical) is canonical
        assert copy.deepcopy(canonical) is canonical
        with pytest.raises(TypeError, match="cannot be pickled"):
            pickle.dumps(canonical)


class Boundary(BaseModel):
    """A boundary model in the house style holding every primitive except CanonicalPath."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    size: ByteCount
    version: ToolVersion
    digest: ContentHash
    model: ModelName
    request: ProviderRequestId
    path: WorkspaceRelPath
    payload: JsonValue = None


def boundary_json(**overrides: str) -> str:
    fields = {
        "size": "5",
        "version": "1",
        "digest": f'"{SHA256_OF_EMPTY}"',
        "model": '"claude-sonnet-5-5"',
        "request": '"req_011CS"',
        "path": '"out/a.png"',
        **overrides,
    }
    return "{" + ",".join(f'"{key}":{value}' for key, value in fields.items()) + "}"


class TestPydantic:
    def test_a_model_round_trips_through_json_in_raw_form(self) -> None:
        model = Boundary(
            size=ByteCount(5),
            version=ToolVersion(1),
            digest=ContentHash(SHA256_OF_EMPTY),
            model=ModelName("claude-sonnet-5-5"),
            request=ProviderRequestId("req_011CS"),
            path=WorkspaceRelPath("out/a.png"),
            payload={"a": [1, 2.5, None, True, "x"]},
        )

        dumped = model.model_dump_json()
        restored = Boundary.model_validate_json(dumped)

        assert restored == model
        assert dumped == boundary_json(payload='{"a":[1,2.5,null,true,"x"]}')
        assert type(restored.size) is ByteCount
        assert type(restored.version) is ToolVersion
        assert type(restored.digest) is ContentHash
        assert type(restored.model) is ModelName
        assert type(restored.request) is ProviderRequestId
        assert type(restored.path) is WorkspaceRelPath

    def test_validation_wraps_raw_values_in_the_typed_class(self) -> None:
        restored = Boundary.model_validate_json(boundary_json())

        assert restored.size == 5
        assert type(restored.size) is ByteCount

    @pytest.mark.parametrize(
        ("field", "bad_json"),
        [
            ("size", "-1"),
            ("size", "1.5"),
            ("size", "true"),
            ("size", '"5"'),
            ("size", "null"),
            ("version", "0"),
            ("version", "-3"),
            ("version", "true"),
            ("version", '"1"'),
            ("digest", '"abc"'),
            ("digest", f'"{SHA256_OF_EMPTY.upper()}"'),
            ("digest", "5"),
            ("model", '""'),
            ("model", '"has space"'),
            ("model", "null"),
            ("request", '"has space"'),
            ("request", '""'),
            ("path", '"../escape"'),
            ("path", '"/abs"'),
            ("path", '"a\\u0000b"'),
            ("path", '""'),
            ("path", "5"),
        ],
    )
    def test_invalid_values_are_rejected_at_the_boundary(self, field: str, bad_json: str) -> None:
        with pytest.raises(ValidationError) as raised:
            Boundary.model_validate_json(boundary_json(**{field: bad_json}))

        assert [error["loc"] for error in raised.value.errors()] == [(field,)]

    def test_python_mode_validates_raw_values_too_which_is_how_dicts_load(self) -> None:
        loaded = Boundary.model_validate(
            {
                "size": 5,
                "version": 1,
                "digest": SHA256_OF_EMPTY,
                "model": "claude-sonnet-5-5",
                "request": "req_011CS",
                "path": "out/a.png",
            }
        )

        assert type(loaded.size) is ByteCount

    def test_python_mode_stays_strict_about_the_raw_type(self) -> None:
        with pytest.raises(ValidationError):
            Boundary.model_validate(
                {**Boundary.model_validate_json(boundary_json()).model_dump(), "size": "5"}
            )
        with pytest.raises(ValidationError):
            Boundary.model_validate(
                {**Boundary.model_validate_json(boundary_json()).model_dump(), "version": True}
            )

    def test_a_field_rejects_a_value_of_the_wrong_primitive_type(self) -> None:
        with pytest.raises(ValidationError, match="plain int"):
            TypeAdapter(ToolVersion).validate_python(ByteCount(5))
        with pytest.raises(ValidationError, match="plain str"):
            TypeAdapter(ModelName).validate_python(ContentHash("0" * 64))
        with pytest.raises(ValidationError, match="plain str"):
            TypeAdapter(WorkspaceRelPath).validate_python(ModelName("claude-sonnet-5-5"))
        with pytest.raises(ValidationError):
            TypeAdapter(WorkspaceRelPath).validate_python(TaskId.new())
        with pytest.raises(ValidationError):
            TypeAdapter(ModelName).validate_python(5)

    def test_json_schema_carries_the_constraints(self) -> None:
        properties = Boundary.model_json_schema()["properties"]

        assert properties["size"] == {
            "maximum": INT64_MAX,
            "minimum": 0,
            "title": "Size",
            "type": "integer",
        }
        assert properties["version"]["minimum"] == 1
        assert properties["version"]["maximum"] == INT64_MAX
        assert properties["digest"]["pattern"] == "^(?:[0-9a-f]{64})$"
        assert properties["model"]["pattern"].startswith("^(?:[A-Za-z0-9]")
        assert properties["path"]["minLength"] == 1
        assert properties["path"]["maxLength"] == 4096

    def test_json_value_accepts_json_and_nothing_else(self) -> None:
        adapter = TypeAdapter(JsonValue)

        assert adapter.validate_python({"a": [1, {"b": None}]}) == {"a": [1, {"b": None}]}
        with pytest.raises(ValidationError):
            adapter.validate_python({"a": {1, 2}})
        with pytest.raises(ValidationError):
            adapter.validate_python({1: "non-string key"})
        with pytest.raises(ValidationError):
            adapter.validate_python(object())

    def test_canonical_path_dumps_to_a_string_but_never_parses_from_one(self, tree: Path) -> None:
        class Holder(BaseModel):
            model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

            path: CanonicalPath

        held = Holder(path=CanonicalPath.resolve(tree))

        assert held.model_dump_json() == f'{{"path":"{tree}"}}'
        assert held.model_dump()["path"] is held.path
        with pytest.raises(ValidationError, match=r"call CanonicalPath\.resolve"):
            Holder.model_validate_json(held.model_dump_json())
        with pytest.raises(ValidationError):
            Holder.model_validate({"path": str(tree)})
        with pytest.raises(ValidationError):
            Holder.model_validate({"path": WorkspaceRelPath("a")})

    def test_a_deep_copied_model_still_validates_and_compares(self) -> None:
        model = Boundary.model_validate_json(boundary_json())

        assert model.model_copy(deep=True) == model


class TestDictKeys:
    """Documented limit: int-based primitives dump as JSON object keys but do not validate back."""

    def test_a_dict_keyed_by_an_int_primitive_dumps_to_string_keys(self) -> None:
        adapter = TypeAdapter(dict[ByteCount, int])

        assert adapter.dump_json({ByteCount(5): 1, ByteCount(7): 2}) == b'{"5":1,"7":2}'

    def test_but_the_json_cannot_be_validated_back_by_design(self) -> None:
        adapter = TypeAdapter(dict[ByteCount, int])

        with pytest.raises(ValidationError):
            adapter.validate_json('{"5": 1}')

    def test_python_mode_keys_are_fine(self) -> None:
        adapter = TypeAdapter(dict[ByteCount, int])

        assert adapter.validate_python({ByteCount(5): 1, 7: 2}) == {5: 1, 7: 2}

    def test_string_primitives_round_trip_as_keys(self) -> None:
        adapter = TypeAdapter(dict[ContentHash, int])
        mapping = {ContentHash("a" * 64): 1}

        assert adapter.validate_json(adapter.dump_json(mapping)) == mapping


class TestHugeNumbersThroughPydantic:
    """A huge int must surface as a short ValidationError, not as the int-to-str ValueError."""

    @pytest.mark.parametrize("digits", [5000, 100_000])
    def test_json_and_python_mode_answer_with_a_validation_error(self, digits: int) -> None:
        adapter = TypeAdapter(ByteCount)
        attempts: list[Callable[[], object]] = [
            lambda: adapter.validate_json("9" * digits),
            lambda: adapter.validate_json("-" + "9" * digits),
            lambda: adapter.validate_python(10**digits),
            lambda: adapter.validate_python(-(10**digits)),
        ]

        for attempt in attempts:
            with pytest.raises(ValidationError) as raised:
                attempt()
            assert len(str(raised.value)) < 600
