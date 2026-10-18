"""Bundles: the single-file form of a package, and what import refuses (S23, I8)."""

import json
import time
from typing import cast

import pytest

from tests.tools.support import package, text_of, tree
from tests.tools.test_package import at
from tests.ui.spec.support import Json
from tumbleweed.core.canonical import content_hash
from tumbleweed.core.jcs import canonicalize
from tumbleweed.tools import (
    BUNDLE_FORMAT,
    ToolPackageError,
    ToolTooLargeError,
    export_bundle,
    parse_bundle,
)
from tumbleweed.tools.limits import MAX_BUNDLE_BYTES, MAX_PACKAGE_BYTES

NFD = "e\N{COMBINING ACUTE ACCENT}"


def bundle_of(package_tree: Json, *, hash_of: bytes | None = None) -> bytes:
    """A bundle around a package tree, with the hash of the tree as it is (or of `hash_of`)."""
    body = canonicalize(package_tree).encode("utf-8")
    digest = content_hash(body if hash_of is None else hash_of)
    return text_of({"format": BUNDLE_FORMAT, "hash": str(digest), "package": package_tree})


def refusal(data: bytes) -> list[str]:
    with pytest.raises(ToolPackageError) as caught:
        parse_bundle(data)
    return list(caught.value.problems)


class TestRoundTrip:
    def test_an_exported_bundle_imports_to_the_same_package_and_hash(self) -> None:
        stored = package(taint=("fs",))

        again = parse_bundle(export_bundle(stored))

        assert again == stored
        assert again.data == stored.data
        assert again.hash == stored.hash

    def test_a_bundle_is_one_canonical_json_document_around_the_stored_bytes(self) -> None:
        stored = package()
        bundle = export_bundle(stored)

        document = json.loads(bundle)

        assert list(document) == ["format", "hash", "package"]
        assert document["format"] == BUNDLE_FORMAT
        assert document["hash"] == stored.hash
        assert bundle == canonicalize(document).encode("utf-8")
        assert stored.data in bundle  # the package is embedded as it is stored, and hashed

    def test_export_is_deterministic(self) -> None:
        stored = package()

        assert export_bundle(stored) == export_bundle(stored)

    def test_a_bundle_is_not_an_archive(self) -> None:
        bundle = export_bundle(package())

        assert not bundle.startswith((b"PK", b"\x1f\x8b", b"BZh", b"\xfd7zXZ"))
        assert bundle.startswith(b"{")


class TestWhatImportRefuses:
    def test_over_the_size_limit_is_refused_before_it_is_parsed(self) -> None:
        with pytest.raises(ToolTooLargeError):
            parse_bundle(b"{" * (MAX_BUNDLE_BYTES + 1))

    def test_exactly_the_limit_is_not_too_large(self) -> None:
        bundle = export_bundle(package())
        padded = bundle + b" " * (MAX_BUNDLE_BYTES - len(bundle))

        assert parse_bundle(padded).hash == parse_bundle(bundle).hash
        with pytest.raises(ToolTooLargeError):
            parse_bundle(padded + b" ")

    def test_not_bytes(self) -> None:
        with pytest.raises(ToolPackageError, match="expected bytes"):
            parse_bundle("{}")  # type: ignore[arg-type]  # the point

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"{",
            b"[]",
            b"null",
            b"\xff\xfe\x00",
            b"\xef\xbb\xbf{}",
            b"PK\x03\x04" + b"\x00" * 200,
            b"\x1f\x8b\x08" + b"\x00" * 200,
            b'{"format":NaN}',
        ],
        ids=repr,
    )
    def test_what_is_not_a_json_object_is_refused(self, data: bytes) -> None:
        assert refusal(data)

    def test_a_bundle_has_exactly_three_members(self) -> None:
        good = json.loads(export_bundle(package()))

        for member in ("format", "hash", "package"):
            without = {key: value for key, value in good.items() if key != member}
            assert any("exactly" in p for p in refusal(text_of(without)))
        assert any("exactly" in p for p in refusal(text_of(good | {"extra": 1})))

    def test_the_format_must_be_ours(self) -> None:
        good = json.loads(export_bundle(package()))

        assert any("format" in p for p in refusal(text_of(good | {"format": "other/1"})))

    @pytest.mark.parametrize("hash_text", ["", "abc", "A" * 64, "g" * 64, "0" * 63, None, 5])
    def test_the_hash_must_be_a_sha256_in_lowercase_hex(self, hash_text: object) -> None:
        good = json.loads(export_bundle(package()))

        assert refusal(text_of(good | {"hash": hash_text}))

    def test_a_hash_that_is_not_the_packages_is_refused(self) -> None:
        good = json.loads(export_bundle(package()))
        wrong = "0" * 64 if good["hash"] != "0" * 64 else "1" * 64

        assert any("hash" in p for p in refusal(text_of(good | {"hash": wrong})))

    def test_a_package_changed_after_it_was_hashed_is_refused(self) -> None:
        stored = package()
        bundle = json.loads(export_bundle(stored))
        modules = cast("list[Json]", bundle["package"]["modules"])
        modules[0]["source"] = f"{modules[0]['source']}# changed\n"

        assert any("hash" in p for p in refusal(text_of(bundle)))

    def test_a_truncated_bundle_is_refused(self) -> None:
        bundle = export_bundle(package())

        for cut in (1, 10, len(bundle) // 2, len(bundle) - 1):
            assert refusal(bundle[:cut])

    def test_a_repeated_key_is_refused_not_resolved(self) -> None:
        bundle = export_bundle(package())
        doubled = bundle.replace(b'"format":', b'"format":"x","format":', 1)

        assert any("repeats the key" in p for p in refusal(doubled))

    def test_a_package_that_is_not_in_normal_form_is_refused_even_with_its_own_hash(self) -> None:
        # NFD text and CRLF: the hash below is the hash of exactly these bytes, so integrity is
        # fine; the package is not what `canonical_package` would write for it, so it is refused
        # rather than repaired (the hash stated must be the hash stored).
        data = tree()
        modules = cast("list[Json]", data["modules"])
        modules[0]["source"] = f"a = '{NFD}'\r\n"

        assert any("normal form" in p for p in refusal(bundle_of(data)))

    def test_nfd_in_a_name_is_not_normal_form_either(self) -> None:
        data = tree()
        at(data, ("manifest",))["name"] = f"caf{NFD}"

        assert any("normal form" in p for p in refusal(bundle_of(data)))

    def test_a_package_that_leaves_out_a_default_is_not_canonical(self) -> None:
        # `taint` has a default, so the package is valid without it; it is not what is written.
        data = tree()
        del data["taint"]

        assert any("normal form" in p for p in refusal(bundle_of(data)))

    def test_an_integer_that_is_not_a_double_is_refused(self) -> None:
        bundle = export_bundle(package()).replace(
            b'"version":1', b'"version":1,"x":9007199254740993'
        )

        assert refusal(bundle)


class TestNothingCanBeSmuggledIn:
    """I8: a bundle carries no grant and no state; it is parsed by the closed models."""

    @pytest.mark.parametrize(
        "field", ["grants", "state", "values", "instances", "task", "tool_grants"]
    )
    def test_an_unknown_member_of_the_envelope(self, field: str) -> None:
        good = json.loads(export_bundle(package()))

        assert any("exactly" in p for p in refusal(text_of(good | {field: []})))

    @pytest.mark.parametrize("where", [(), ("manifest",), ("ui",), ("modules", 0)], ids=str)
    @pytest.mark.parametrize("field", ["grants", "state", "values", "task"])
    def test_an_unknown_member_of_the_package(
        self, where: tuple[str | int, ...], field: str
    ) -> None:
        data = tree()
        at(data, where)[field] = [{"kind": "fs.read", "path": "/"}]

        assert refusal(bundle_of(data))

    def test_a_current_value_cannot_be_put_beside_a_default(self) -> None:
        data = tree()
        state = cast("list[Json]", at(data, ("ui",))["state"])
        state[0]["current"] = "/home/user/secret.csv"

        assert refusal(bundle_of(data))

    def test_a_bundle_cannot_lower_what_it_never_had_the_taint_is_the_exporters_claim(self) -> None:
        # The taint a bundle states is kept as written; it can be a restriction or nothing, and
        # both fields it could lie with (grants, state) do not exist. A claim of both is refused.
        data = tree(package(taint=("fs",)))
        data["taint"] = ["fs", "net"]

        assert refusal(bundle_of(data))
        assert parse_bundle(export_bundle(package(taint=("fs",)))).package.taint == ("fs",)


class TestHostileShapes:
    def test_deep_nesting_cannot_exhaust_the_stack(self) -> None:
        for opening in (b"[", b'{"a":'):
            assert refusal(opening * 100_000)

    def test_a_package_of_deep_nesting_inside_a_valid_envelope(self) -> None:
        deep = b"[" * 200_000
        bundle = (
            b'{"format":"'
            + BUNDLE_FORMAT.encode()
            + b'","hash":"'
            + b"0" * 64
            + b'","package":'
            + deep
            + b"}"
        )

        assert len(bundle) <= MAX_BUNDLE_BYTES
        assert refusal(bundle)

    def test_a_wide_object_is_refused_while_parsing(self) -> None:
        wide = b"{" + b",".join(b'"k%d":0' % n for n in range(6000)) + b"}"

        assert len(wide) < MAX_PACKAGE_BYTES
        assert any("members" in p for p in refusal(wide))

    def test_a_refusal_is_quick(self) -> None:
        started = time.monotonic()
        refusal(b"[" * 400_000)
        refusal(b'{"a":' * 80_000)

        assert time.monotonic() - started < 5

    def test_the_errors_are_bounded_and_do_not_repeat_the_input(self) -> None:
        marker = "SECRET-MARKER-" + "z" * 400
        data = tree()
        manifest = at(data, ("manifest",))
        manifest["name"] = marker
        manifest["tags"] = [marker] * 30
        data["format"] = marker

        problems = refusal(bundle_of(data))

        assert len(problems) <= 11
        assert all(len(p) <= 300 for p in problems)
        assert "SECRET-MARKER" not in " ".join(problems)
