"""Composing a run: disjoint mounts and the broker-owned environment."""

import pytest

from tumbleweed.broker.compose import (
    CALLER_ENV_NAMES,
    InvalidRunError,
    Mount,
    disjoint_mounts,
    sandbox_environment,
)
from tumbleweed.broker.identity import FsIdentity
from tumbleweed.broker.paths import StoredPath
from tumbleweed.core.ids import GrantId


def mount(path: str, ino: int = 1, *, writable: bool = False) -> Mount:
    return Mount(
        GrantId.new(),
        StoredPath(path=path, identity=FsIdentity(fsid=1, ino=ino)),
        writable=writable,
    )


class TestWriteAndReadMounts:
    """A write grant is kept disjoint from the read grants of the run (#74, S21, D28)."""

    def test_a_read_inside_a_write_is_merged_into_it(self) -> None:
        outer, inner = mount("/h/p", writable=True), mount("/h/p/sub")

        assert disjoint_mounts([inner, outer]) == [outer]

    def test_the_same_path_read_and_write_is_one_writable_mount_whatever_the_order(self) -> None:
        read, write = mount("/h/p"), mount("/h/p", writable=True)

        assert disjoint_mounts([read, write]) == [write]
        assert disjoint_mounts([write, read]) == [write]

    def test_a_write_inside_a_read_refuses_the_run(self) -> None:
        outer, inner = mount("/h/p"), mount("/h/p/sub", writable=True)

        with pytest.raises(InvalidRunError, match="write grant lies inside a read grant"):
            disjoint_mounts([outer, inner])

    def test_a_write_inside_a_write_is_the_outer_one(self) -> None:
        outer, inner = mount("/h/p", writable=True), mount("/h/p/sub", writable=True)

        assert disjoint_mounts([inner, outer]) == [outer]

    def test_a_write_beside_a_read_keeps_both_and_their_access(self) -> None:
        read, write = mount("/h/photos"), mount("/h/photos-out", writable=True)

        assert disjoint_mounts([write, read]) == [read, write]


class TestDisjointMounts:
    def test_the_outer_directory_replaces_the_one_inside_it(self) -> None:
        outer, inner = mount("/home/u/Photos"), mount("/home/u/Photos/2026")

        assert disjoint_mounts([inner, outer]) == [outer]

    def test_a_file_inside_a_granted_directory_is_dropped_too(self) -> None:
        outer, file = mount("/home/u/Docs"), mount("/home/u/Docs/notes.txt")

        assert disjoint_mounts([file, outer]) == [outer]

    def test_the_same_path_twice_is_mounted_once(self) -> None:
        first, second = mount("/home/u/Docs"), mount("/home/u/Docs")

        assert len(disjoint_mounts([first, second])) == 1

    def test_siblings_and_names_that_only_share_a_prefix_are_kept(self) -> None:
        a, b, c = mount("/home/u/Doc"), mount("/home/u/Docs"), mount("/home/u/Doc2")

        kept = disjoint_mounts([b, c, a])

        assert {m.path.path for m in kept} == {"/home/u/Doc", "/home/u/Docs", "/home/u/Doc2"}

    def test_the_result_never_overlaps_whatever_the_order(self) -> None:
        paths = ["/a", "/a/b", "/a/b/c", "/d/e", "/d", "/f/g/h", "/f/g"]
        import itertools

        for order in itertools.islice(itertools.permutations(paths), 200):
            kept = disjoint_mounts([mount(p) for p in order])
            texts = sorted(m.path.path for m in kept)
            assert texts == ["/a", "/d", "/f/g"]

    def test_nothing_in_is_nothing_out(self) -> None:
        assert disjoint_mounts([]) == []


class TestEnvironment:
    def test_the_fixed_variables_are_the_whole_environment(self) -> None:
        variables, allowlist = sandbox_environment([], path="/usr/bin:/bin")

        assert {str(v.name): v.value for v in variables} == {
            "PATH": "/usr/bin:/bin",
            "HOME": "/workspace",
            "TMPDIR": "/tmp",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        assert {str(name) for name in allowlist} == {str(v.name) for v in variables}

    def test_a_request_may_set_only_the_locale_and_the_time_zone(self) -> None:
        variables, _ = sandbox_environment(
            [("LANG", "de_DE.UTF-8"), ("TZ", "UTC"), ("LC_ALL", "C")], path="/usr/bin"
        )

        found = {str(v.name): v.value for v in variables}
        assert found["LANG"] == "de_DE.UTF-8"
        assert found["TZ"] == "UTC"
        assert found["LC_ALL"] == "C"
        assert {"LANG", "LC_ALL", "TZ"} == CALLER_ENV_NAMES

    @pytest.mark.parametrize(
        "name",
        [
            "ANTHROPIC_API_KEY",
            "API_TOKEN",
            "LD_PRELOAD",
            "PATH",
            "HOME",
            "PYTHONPATH",
            "SSH_AUTH_SOCK",
            "lang",
            "",
        ],
    )
    def test_any_other_name_is_refused_and_not_dropped(self, name: str) -> None:
        with pytest.raises(InvalidRunError):
            sandbox_environment([(name, "x")], path="/usr/bin")

    def test_a_name_given_twice_is_refused(self) -> None:
        with pytest.raises(InvalidRunError, match="twice"):
            sandbox_environment([("TZ", "UTC"), ("TZ", "CET")], path="/usr/bin")

    def test_a_value_the_sandbox_would_refuse_is_refused_here(self) -> None:
        with pytest.raises(InvalidRunError):
            sandbox_environment([("TZ", "a\0b")], path="/usr/bin")

    def test_the_error_does_not_repeat_the_name(self) -> None:
        with pytest.raises(InvalidRunError) as caught:
            sandbox_environment([("MY_SECRET_NAME", "v")], path="/usr/bin")

        assert "MY_SECRET_NAME" not in str(caught.value)
