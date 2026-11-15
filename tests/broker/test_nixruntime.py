"""The sandbox runtime of a Nix install (S26, D32): what the broker accepts as a manifest.

The manifest is what `nix/runtime.nix` builds. The shape and location rules are about trust: a
runtime that is not exactly some store paths read from the store is refused, and the broker does
not start, instead of running sandboxes over something else. The test of the real closure, with a
real sandbox over it, is `tests/sandbox/test_nix_runtime.py`.
"""

import json
from pathlib import Path

import pytest

from tumbleweed.broker.daemon import _parser, build_config
from tumbleweed.broker.errors import RuntimeManifestError
from tumbleweed.broker.nixruntime import (
    MANIFEST_KIND,
    MAX_MANIFEST_BYTES,
    RUNTIME_MANIFEST_ENV,
    load_nix_runtime,
    parse_nix_runtime,
)
from tumbleweed.sandbox.policy import MAX_RUNTIME_MOUNTS, RuntimeSpec

HASH = "a" * 32
PYTHON = f"/nix/store/{HASH}-python3-3.12.16-env"
LIBC = f"/nix/store/{'b' * 32}-glibc-2.42"


def manifest(**changes: object) -> str:
    fields: dict[str, object] = {
        "kind": MANIFEST_KIND,
        "mounts": [PYTHON, LIBC],
        "interpreter": f"{PYTHON}/bin/python3",
        "path": f"{PYTHON}/bin",
    }
    return json.dumps(fields | changes)


def test_a_manifest_becomes_the_runtime_and_the_path_of_a_sandbox() -> None:
    runtime = parse_nix_runtime(manifest())

    assert runtime.spec.mounts == (PYTHON, LIBC)
    assert runtime.spec.interpreter == f"{PYTHON}/bin/python3"
    assert runtime.spec.symlinks == ()
    assert runtime.sandbox_path == f"{PYTHON}/bin"


def test_the_path_may_name_several_directories_each_inside_a_mount() -> None:
    runtime = parse_nix_runtime(manifest(path=f"{PYTHON}/bin:{LIBC}/bin"))

    assert runtime.sandbox_path == f"{PYTHON}/bin:{LIBC}/bin"


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"kind": "tumbleweed.nix-runtime/2"}, "another kind"),
        ({"extra": 1}, "an unknown field"),
        ({"mounts": []}, "no mounts"),
        ({"mounts": [PYTHON, PYTHON]}, "a store path twice"),
        ({"mounts": ["/nix/store"]}, "the whole store"),
        ({"mounts": [f"{PYTHON}/bin"]}, "a path below a store path"),
        ({"mounts": [f"{PYTHON}/"]}, "a trailing slash"),
        ({"mounts": ["/usr"]}, "the system's /usr"),
        ({"mounts": ["/home/me/.nix-profile"]}, "a home directory"),
        ({"mounts": [f"/nix/store/short-{HASH}"]}, "a hash of the wrong length"),
        ({"mounts": [f"/nix/store/{'A' * 32}-upper"]}, "a hash of the wrong alphabet"),
        ({"mounts": [PYTHON, 7]}, "a mount that is not text"),
        ({"interpreter": "/usr/bin/python3"}, "an interpreter outside the mounts"),
        ({"interpreter": f"/nix/store/{'c' * 32}-other/bin/python3"}, "an unmounted interpreter"),
        ({"interpreter": f"{PYTHON}/bin/../bin/python3"}, "an unnormalised interpreter"),
        ({"interpreter": PYTHON}, "an interpreter that is a mount, not a file in one"),
        ({"path": ""}, "an empty path"),
        ({"path": ":"}, "a path of separators only"),
        ({"path": f"{PYTHON}/bin:/usr/bin"}, "a path entry outside the mounts"),
        ({"path": f"{PYTHON}/bin:bin"}, "a relative path entry"),
        ({"path": f"{PYTHON}/../{HASH}-x/bin"}, "a path entry with .."),
    ],
)
def test_a_manifest_that_is_not_what_the_build_makes_is_refused(
    changes: dict[str, object], why: str
) -> None:
    with pytest.raises(RuntimeManifestError):
        parse_nix_runtime(manifest(**changes))


@pytest.mark.parametrize("text", ["", "[]", "null", "{", '{"kind": "x"}', "\xff"])
def test_text_that_is_not_a_manifest_is_refused(text: str) -> None:
    with pytest.raises(RuntimeManifestError):
        parse_nix_runtime(text)


def many_mounts(count: int) -> str:
    mounts = [f"/nix/store/{n:032x}-pkg" for n in range(count)]
    return manifest(mounts=mounts, interpreter=f"{mounts[0]}/bin/python3", path=f"{mounts[0]}/bin")


def test_a_runtime_may_have_as_many_mounts_as_the_policy_allows_and_not_one_more() -> None:
    assert len(parse_nix_runtime(many_mounts(MAX_RUNTIME_MOUNTS)).spec.mounts) == MAX_RUNTIME_MOUNTS

    with pytest.raises(RuntimeManifestError):
        parse_nix_runtime(many_mounts(MAX_RUNTIME_MOUNTS + 1))


@pytest.mark.parametrize(
    "location",
    [
        "",
        "manifest.json",
        "/tmp/runtime.json",
        "/nix/store",
        "/nix/store/",
        "/nix/storeish/x",
        f"/nix/store/{HASH}-runtime.json/../../../tmp/x",
        f"/nix/store//{HASH}-runtime.json",
        f"/nix/store/{HASH}-runtime.json\0",
    ],
)
def test_a_manifest_outside_the_store_is_never_opened(location: str) -> None:
    with pytest.raises(RuntimeManifestError, match="not a file in the Nix store"):
        load_nix_runtime(location)


def test_a_manifest_that_is_not_there_is_refused_without_naming_the_path() -> None:
    location = f"/nix/store/{HASH}-absent-runtime.json"

    with pytest.raises(RuntimeManifestError) as caught:
        load_nix_runtime(location)

    assert location not in str(caught.value)


def test_the_size_limit_leaves_room_for_the_most_mounts_a_runtime_may_have() -> None:
    longest_mount = f"/nix/store/{HASH}-{'x' * 200}"

    assert MAX_RUNTIME_MOUNTS * (len(longest_mount) + 4) < MAX_MANIFEST_BYTES


class TestTheDaemonsConfiguration:
    def environment(self, home: Path, **extra: str) -> dict[str, str]:
        return {"HOME": str(home), "XDG_RUNTIME_DIR": str(home / "run"), **extra}

    def test_without_a_manifest_the_runtime_is_the_systems(self, tmp_path: Path) -> None:
        config = build_config(
            _parser().parse_args([]), self.environment(tmp_path), tmp_path / "sockets"
        )

        assert config.runtime == RuntimeSpec.merged_usr()
        assert config.sandbox_path == "/usr/bin:/bin"

    @pytest.mark.parametrize("location", ["", "relative.json", "/tmp/runtime.json"])
    def test_a_manifest_that_is_not_in_the_store_stops_the_broker_from_starting(
        self, tmp_path: Path, location: str
    ) -> None:
        # Not a fall back to /usr: a Nix system has none, and a broker over a runtime it was
        # not given must not run sandboxes.
        environment = self.environment(tmp_path, **{RUNTIME_MANIFEST_ENV: location})

        with pytest.raises(RuntimeManifestError):
            build_config(_parser().parse_args([]), environment, tmp_path / "sockets")
