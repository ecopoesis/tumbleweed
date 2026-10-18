"""Normalised text, canonical bytes and the content hash: what the tool store hashes (S23)."""

import hashlib
import sys
import unicodedata
from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tumbleweed.core.canonical import canonical_bytes, content_hash, normalize_text, normalize_tree
from tumbleweed.core.types import ContentHash

NFD = "é"  # e + combining acute
NFC = "é"


class TestNormalizeText:
    def test_nfd_becomes_nfc(self) -> None:
        assert normalize_text(NFD) == NFC

    @pytest.mark.parametrize("ending", ["\r\n", "\r"])
    def test_any_line_ending_becomes_a_line_feed(self, ending: str) -> None:
        assert normalize_text(f"a{ending}b{ending}") == "a\nb\n"

    def test_a_line_feed_stays_and_other_separators_are_not_line_endings(self) -> None:
        text = "a\nb\N{LINE SEPARATOR}c\N{NEXT LINE}d"

        assert normalize_text(text) == text

    def test_both_at_once(self) -> None:
        assert normalize_text(f"{NFD}\r\n{NFD}") == f"{NFC}\n{NFC}"

    def test_nfc_can_make_a_text_longer(self) -> None:
        # U+0344 decomposes to two characters and stays decomposed in NFC: callers must validate
        # the normalised text, never the original.
        text = "̈́"

        assert len(normalize_text(text)) == 2 > len(text)

    @given(st.text())
    def test_it_is_idempotent(self, text: str) -> None:
        once = normalize_text(text)

        assert normalize_text(once) == once
        assert unicodedata.is_normalized("NFC", once)
        assert "\r" not in once


class TestNormalizeTree:
    def test_strings_and_keys_are_normalised_at_every_depth(self) -> None:
        tree = {NFD: [f"{NFD}\r\n", {"k" + NFD: {"deep": NFD}}], "n": 1, "t": True, "z": None}

        assert normalize_tree(tree) == {
            NFC: [f"{NFC}\n", {"k" + NFC: {"deep": NFC}}],
            "n": 1,
            "t": True,
            "z": None,
        }

    def test_the_input_is_not_changed(self) -> None:
        tree = {NFD: [NFD]}

        normalize_tree(tree)

        assert tree == {NFD: [NFD]}

    def test_two_keys_that_become_one_are_refused_not_merged(self) -> None:
        with pytest.raises(ValueError, match="one key once normalised"):
            normalize_tree({NFD: 1, NFC: 2})

    def test_a_key_that_is_not_a_string_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be a string"):
            normalize_tree({1: "a"})

    def test_what_is_not_json_is_passed_on_for_the_canonicaliser_to_refuse(self) -> None:
        assert normalize_tree({"a": (1, 2)}) == {"a": (1, 2)}
        with pytest.raises(ValueError, match="not a JSON value"):
            canonical_bytes({"a": (1, 2)})

    def test_a_subclass_of_str_is_not_normalised_and_not_json(self) -> None:
        class Text(str):
            pass

        with pytest.raises(ValueError, match="not a JSON value"):
            canonical_bytes(Text(NFD))

    def test_it_does_not_recurse(self) -> None:
        value: object = NFD
        for _ in range(150):
            value = [value]
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(60)
        try:
            normalised = normalize_tree(value)
        finally:
            sys.setrecursionlimit(limit)

        for _ in range(150):
            assert isinstance(normalised, list)
            normalised = normalised[0]
        assert normalised == NFC

    def test_nesting_and_size_are_bounded(self) -> None:
        deep: object = []
        for _ in range(1000):
            deep = [deep]
        with pytest.raises(ValueError, match="nested deeper than 200"):
            normalize_tree(deep)
        with pytest.raises(ValueError, match="more than 5 values"):
            normalize_tree([0] * 10, max_nodes=5)
        with pytest.raises(ValueError, match="more than 5 values"):
            normalize_tree(dict.fromkeys("abcdefghij", 0), max_nodes=5)


class TestCanonicalBytesAndHash:
    def test_the_bytes_are_normalised_rfc_8785_utf8(self) -> None:
        data = canonical_bytes({"b": f"{NFD}\r\n", "a": [1, 2.5, None]})

        assert data == b'{"a":[1,2.5,null],"b":"\xc3\xa9\\n"}'

    def test_texts_that_differ_only_in_nfc_and_line_endings_have_one_hash(self) -> None:
        one = canonical_bytes({"code": f"x = '{NFD}'\r\ny = 1\r\n"})
        other = canonical_bytes({"code": f"x = '{NFC}'\ny = 1\n"})

        assert one == other
        assert content_hash(one) == content_hash(other)

    def test_a_different_text_has_a_different_hash(self) -> None:
        assert content_hash(canonical_bytes({"a": "x"})) != content_hash(
            canonical_bytes({"a": "y"})
        )

    def test_the_hash_is_the_sha256_of_the_bytes_as_a_content_hash(self) -> None:
        data = canonical_bytes({"a": 1})

        digest = content_hash(data)

        assert type(digest) is ContentHash
        assert digest == hashlib.sha256(b'{"a":1}').hexdigest()
        assert content_hash(b"") == hashlib.sha256(b"").hexdigest()

    def test_the_hash_of_known_bytes_is_stable(self) -> None:
        # The SHA-256 of the UTF-8 bytes of `{"a":2,"b":[1,"é\n"]}`, computed with `shasum`. A
        # literal, so that a change of the normalisation or of the canonical form is noticed here
        # and has to be a decision (every stored hash and every tool grant would change).
        assert content_hash(canonical_bytes({"b": [1, "é\r\n"], "a": 2})) == (
            "e7217ab1660e211c26a87ed08078c164c6595120c229118fac3a426ee6ec4f87"
        )

    def test_integers_and_floats_of_one_value_are_one_value(self) -> None:
        # The note of #23: inside a `json` value int and float do not survive canonicalisation
        # (5.0 is 5); the bytes, and so the hash, are the same either way.
        assert canonical_bytes({"v": 5.0}) == canonical_bytes({"v": 5}) == b'{"v":5}'

    def test_what_canonicalisation_refuses_is_refused(self) -> None:
        with pytest.raises(ValueError, match="IEEE-754"):
            canonical_bytes({"v": 2**53 + 1})
        with pytest.raises(ValueError, match="lone surrogate"):
            canonical_bytes({"v": "\ud800"})

    @given(st.dictionaries(st.text(max_size=8), st.text(max_size=20), max_size=6))
    def test_the_bytes_of_normalised_data_are_a_fixed_point(self, data: dict[str, str]) -> None:
        try:
            once = canonical_bytes(data)
        except ValueError:  # a lone surrogate, or keys that collide once normalised
            return

        assert canonical_bytes(normalize_tree(data)) == once


class TestUnicodeStability:
    """NFC changes with the Unicode version (U+105D2 U+0307 composes in 16.0, not in 15.0), so text
    that holds a character 15.0 had not assigned is refused: one text, one hash, on every Python."""

    NEW_IN_16 = "\U000105d2\u0307"

    def test_text_with_a_character_unassigned_in_unicode_15_is_refused(self) -> None:
        for text in (self.NEW_IN_16, "a\u0378b", "x\U0010ffffy", "\ufdd0", "\U0001fffe"):
            with pytest.raises(ValueError, match=r"contains a character newer than Unicode 15\.0"):
                canonical_bytes({"k": text})
            with pytest.raises(ValueError, match=r"contains a character newer than Unicode 15\.0"):
                canonical_bytes({text: 1})

    def test_text_assigned_in_unicode_15_is_accepted(self) -> None:
        for text in ("plain", NFC, "\u0e86", "\U00020000", "\U0010fffd", "\U0001f600", "\ue000"):
            assert canonical_bytes([text])

    def test_the_table_is_what_unicode_15_says_wherever_python_has_it(self) -> None:
        import unicodedata

        from tumbleweed.core import unicode15

        if unicodedata.unidata_version != "15.0.0":
            pytest.skip(f"this Python has Unicode {unicodedata.unidata_version}, not 15.0")
        for code in range(0x110000):
            char = chr(code)
            assert unicode15.is_stable(char) == (unicodedata.category(char) != "Cn"), hex(code)

    def test_the_table_is_sorted_and_disjoint(self) -> None:
        from tumbleweed.core.unicode15 import UNASSIGNED_15_0

        assert all(a <= b for a, b in UNASSIGNED_15_0)
        assert all(b1 < a2 for (_, b1), (a2, _) in pairwise(UNASSIGNED_15_0))


def test_nfc_not_nfkc_a_ligature_stays_a_ligature() -> None:
    # NFKC would write "fi"; the canonical form is NFC. A literal, as a change would change hashes.
    assert canonical_bytes(["\N{LATIN SMALL LIGATURE FI}"]) == '["\ufb01"]'.encode()
    assert (
        content_hash(canonical_bytes("\ufb01")) == hashlib.sha256('"\ufb01"'.encode()).hexdigest()
    )
