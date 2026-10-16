"""Tests for the display-only text transform (GTK-free; GTK tests prove it on real labels)."""

import unicodedata

import pytest

from tumbleweed.shell.viewmodel.display import (
    MAX_CHUNK,
    MAX_CLUSTER,
    MAX_RUN,
    ZERO_WIDTH_SPACE,
    DisplayChunks,
    DisplayText,
    display_text,
)

Z = ZERO_WIDTH_SPACE
JAMO_L = "\u1100"  # HANGUL CHOSEONG KIYEOK
VISARGA = "\u0903"  # DEVANAGARI SIGN VISARGA, a spacing mark
ACUTE = "\u0301"  # COMBINING ACUTE ACCENT
ZWJ = "\u200d"
WOMAN = "\U0001f469"
KA = "\u0915"  # DEVANAGARI LETTER KA

ORDINARY = {
    "prose": "The quick brown fox jumps over the lazy dog. " * 40,
    "a family emoji": "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",
    "emoji with a skin tone": "\U0001f44d\U0001f3fd",
    "a flag by tags": "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
    "regional indicators": "\U0001f1e9\U0001f1ea" * 30,
    "devanagari conjuncts": "\u0915\u094d\u0937 \u0938\u094d\u0924\u094d\u0930\u0940 " * 20,
    "thai": "\u0e01\u0e34\u0e49\u0e32\u0e22 \u0e04\u0e48\u0e30 " * 20,
    "arabic": "\u0627\u0644\u0639\u0631\u0628\u064a\u0629 \u0645\u064e\u0643\u0652\u062a\u064e "
    * 20,
    "composed hangul": "\uc548\ub155\ud558\uc138\uc694" * 50,
    # Decomposed Hangul (what macOS file names are): each syllable is L V T, and T then L breaks.
    "decomposed hangul": unicodedata.normalize(
        "NFD", "\ud55c\uad6d\uc5b4\ub97c \ubc30\uc6b0\ub2e4" * 80
    ),
    "combining accents": "e\u0301\u0302 a\u0300" * 100,
    "cjk": "\u65e5\u672c\u8a9e\u306e\u6587\u7ae0" * 20,
    "empty": "",
}


@pytest.mark.parametrize("text", ORDINARY.values(), ids=list(ORDINARY))
def test_ordinary_text_is_returned_as_it_is(text: str) -> None:
    assert display_text(text) is text


def test_the_decomposed_hangul_sample_really_is_decomposed() -> None:
    # Without this, the sample above could silently be composed text and prove nothing.
    text = ORDINARY["decomposed hangul"]

    assert any(0x1100 <= ord(char) <= 0x11FF for char in text)
    assert not any(0xAC00 <= ord(char) <= 0xD7A3 for char in text)


class TestGraphemeClusters:
    @pytest.mark.parametrize(
        ("name", "text"),
        [
            ("jamo", JAMO_L * 5000),
            ("a ZWJ chain", WOMAN + (ZWJ + WOMAN) * 1000),
            ("a base and spacing marks", KA + VISARGA * 3000),
            ("a base and combining marks", "e" + ACUTE * 3000),
            ("many ZWJs", ZWJ * 3000),
            ("prepend characters", "\u0600" * 3000),
            ("variation selectors", "a" + "\ufe0f" * 3000),
            ("tag characters", "\U0001f3f4" + "\U000e0067" * 3000),
            ("emoji modifiers", "\U0001f44d" + "\U0001f3fd" * 3000),
        ],
    )
    def test_no_cluster_stays_longer_than_the_cap(self, name: str, text: str) -> None:
        shown = display_text(text)

        assert shown != text, name
        assert Z in shown
        # Between two zero-width spaces (a cluster boundary) there are at most MAX_CLUSTER
        # code points, and only zero-width spaces were added.
        assert max(len(piece) for piece in shown.split(Z)) <= MAX_CLUSTER
        assert shown.replace(Z, "") == text

    def test_a_cluster_exactly_at_the_cap_is_left_alone(self) -> None:
        text = "e" + ACUTE * (MAX_CLUSTER - 1)

        assert display_text(text) is text

    def test_one_more_extender_gets_a_break_in_front(self) -> None:
        text = "e" + ACUTE * MAX_CLUSTER

        assert display_text(text) == "e" + ACUTE * (MAX_CLUSTER - 1) + Z + ACUTE

    def test_the_break_starts_a_cluster_of_its_own(self) -> None:
        shown = display_text("e" + ACUTE * (MAX_CLUSTER * 3))

        assert all(0 < len(piece) <= MAX_CLUSTER for piece in shown.split(Z))

    def test_a_base_that_does_not_extend_ends_the_cluster_without_a_break(self) -> None:
        text = ("e" + ACUTE * (MAX_CLUSTER - 1)) * 5

        assert display_text(text) is text  # each "e" starts a new cluster

    def test_the_break_character_is_a_format_character_and_not_a_mark(self) -> None:
        # U+200B has Grapheme_Cluster_Break=Control (and Line_Break=ZW), which is what ends a
        # cluster and offers a line break; it is not itself one that extends.
        assert unicodedata.category(Z) == "Cf"
        assert unicodedata.category(Z) not in {"Mn", "Mc", "Me"}


class TestUnbrokenRuns:
    @pytest.mark.parametrize("size", [16 * 1024, 64 * 1024])
    def test_a_run_without_a_break_opportunity_gets_one_every_max_run_characters(
        self, size: int
    ) -> None:
        text = "ab" * (size // 2)

        shown = display_text(text)

        pieces = shown.split(Z)
        assert all(len(piece) <= MAX_RUN for piece in pieces)
        assert len(pieces) == size // MAX_RUN
        assert shown.replace(Z, "") == text

    def test_whitespace_resets_the_run(self) -> None:
        text = ("a" * (MAX_RUN - 1) + " ") * 10

        assert display_text(text) is text

    def test_a_zero_width_space_already_there_resets_the_run(self) -> None:
        text = ("a" * (MAX_RUN - 1) + Z) * 10

        assert display_text(text) is text

    def test_the_run_is_counted_in_code_points(self) -> None:
        assert display_text("\U0001f600" * MAX_RUN) == "\U0001f600" * MAX_RUN
        assert display_text("\U0001f600" * (MAX_RUN + 1)).count(Z) == 1

    def test_a_break_is_not_added_in_front_of_a_space(self) -> None:
        text = "a" * MAX_RUN + " " + "b"

        assert display_text(text) is text


class TestIncremental:
    @pytest.mark.parametrize("piece", [1, 2, 7, 100, 4096])
    @pytest.mark.parametrize(
        "text",
        [
            JAMO_L * 2000,
            "ab" * 2000,
            "x " * 500,
            WOMAN + (ZWJ + WOMAN) * 200,
            KA + VISARGA * 900,
        ],
        ids=["jamo", "hex", "words", "zwj", "marks"],
    )
    def test_feeding_in_pieces_gives_the_same_result_as_feeding_whole(
        self, text: str, piece: int
    ) -> None:
        feeder = DisplayText()

        streamed = "".join(feeder.feed(text[i : i + piece]) for i in range(0, len(text), piece))

        assert streamed == display_text(text)

    def test_an_empty_piece_changes_nothing(self) -> None:
        feeder = DisplayText()
        feeder.feed("abc")

        assert feeder.feed("") == ""

    def test_a_break_decided_across_two_pieces_is_the_same_as_in_one(self) -> None:
        feeder = DisplayText()
        first = feeder.feed("e" + ACUTE * (MAX_CLUSTER - 1))  # exactly at the cap
        second = feeder.feed(ACUTE)  # the next extender needs the break

        assert (first, second) == ("e" + ACUTE * (MAX_CLUSTER - 1), Z + ACUTE)


def test_the_constants_leave_room_for_real_text() -> None:
    assert MAX_CLUSTER >= 10  # the longest emoji ZWJ sequences
    assert MAX_RUN >= 128  # more than any word


SAMPLES = {
    "hex": "ab" * 40_000,
    "jamo": JAMO_L * 30_000,
    "prose with paragraphs": ("A sentence of some length. " * 40 + "\n") * 60,
    "prose with no newlines": "word " * 20_000,
    "one long line of spaces and words": ("x" * 100 + " ") * 2000,
    "mixed": ("ab" * 3000 + "\n" + JAMO_L * 3000 + " " + "word " * 500) * 5,
    "newlines only": "\n" * 20_000,
    "short": "hello",
    "empty": "",
}


class TestChunks:
    @pytest.mark.parametrize("text", SAMPLES.values(), ids=list(SAMPLES))
    def test_the_chunks_are_exactly_the_display_text_and_all_small(self, text: str) -> None:
        chunks = DisplayChunks()
        chunks.feed(text)

        assert "".join(chunks.chunks) == display_text(text)
        assert all(len(chunk) <= MAX_CHUNK for chunk in chunks.chunks)

    def test_text_that_fits_stays_one_chunk(self) -> None:
        chunks = DisplayChunks()
        chunks.feed("a" * 100)

        assert chunks.chunks == ("a" * 100,)

    def test_a_message_with_nothing_yet_has_one_empty_chunk(self) -> None:
        assert DisplayChunks().chunks == ("",)

    def test_a_chunk_ends_at_a_newline_when_there_is_one_in_range(self) -> None:
        line = "w" * 1000 + "\n"
        chunks = DisplayChunks()
        chunks.feed(line * 10)

        assert all(chunk.endswith("\n") for chunk in chunks.chunks[:-1])

    def test_without_newlines_a_chunk_ends_at_a_space_or_a_zero_width_space(self) -> None:
        chunks = DisplayChunks()
        chunks.feed("word " * 5000)
        hexed = DisplayChunks()
        hexed.feed("ab" * 20_000)

        assert all(chunk.endswith(" ") for chunk in chunks.chunks[:-1])
        assert all(chunk.endswith(Z) for chunk in hexed.chunks[:-1])

    @pytest.mark.parametrize("piece", [1, 13, 500, 5000])
    @pytest.mark.parametrize("name", ["hex", "prose with paragraphs", "mixed", "jamo"])
    def test_feeding_in_pieces_gives_the_same_chunks_as_feeding_whole(
        self, name: str, piece: int
    ) -> None:
        text = SAMPLES[name]
        whole = DisplayChunks()
        whole.feed(text)
        pieces = DisplayChunks()

        for start in range(0, len(text), piece):
            pieces.feed(text[start : start + piece])

        assert pieces.chunks == whole.chunks

    def test_feed_says_where_the_change_starts_and_earlier_chunks_never_change(self) -> None:
        chunks = DisplayChunks()
        text = "ab" * 20_000
        previous: tuple[str, ...] = chunks.chunks

        for start in range(0, len(text), 700):
            first = chunks.feed(text[start : start + 700])
            now = chunks.chunks

            assert now[:first] == previous[:first]  # nothing before `first` changed
            assert first >= len(previous) - 1  # only the last chunk was open
            previous = now
        assert len(previous) > 5
