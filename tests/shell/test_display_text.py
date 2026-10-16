"""Tests for the display-only text transform (GTK-free; GTK tests prove it on real labels)."""

import unicodedata

import pytest
import regex

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
JAMO_L = "\u1100"  # HANGUL CHOSEONG KIYEOK (L)
JAMO_V = "\u1161"  # HANGUL JUNGSEONG A (V)
JAMO_T = "\u11a8"  # HANGUL JONGSEONG KIYEOK (T)
SYLLABLE_LV = "\uac00"  # a precomposed syllable without a final consonant (LV)
SYLLABLE_LVT = "\uac01"  # a precomposed syllable with one (LVT)
VISARGA = "\u0903"  # DEVANAGARI SIGN VISARGA, a spacing mark
VIRAMA = "\u094d"  # DEVANAGARI SIGN VIRAMA, a conjunct linker
KA = "\u0915"  # DEVANAGARI LETTER KA
ACUTE = "\u0301"  # COMBINING ACUTE ACCENT
HALFWIDTH_VOICED = "\uff9e"  # HALFWIDTH KATAKANA VOICED SOUND MARK: Lm, but a cluster Extend
HALFWIDTH_KA = "\uff76"
ZWJ = "\u200d"
ZWNJ = "\u200c"
WOMAN = "\U0001f469"

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
    "decomposed hangul without spaces": (JAMO_L + JAMO_V + JAMO_T) * 80,
    "halfwidth katakana": ("\uff76\uff9e\uff8a\uff9f" * 40 + " ") * 4,
    "zwnj in a persian word": "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645 " * 30,
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


# Each is one grapheme cluster by UAX 29, by the rule named, and so far too long to leave alone.
CLUSTERS = {
    "GB6: L then L": JAMO_L * 5000,
    "GB6 and GB7: L, then V and V": JAMO_L + JAMO_V * 3000,
    "GB7 and GB8: L, V, then T and T": JAMO_L + JAMO_V + JAMO_T * 3000,
    "GB6 and GB8: L, LVT, then T and T": JAMO_L + SYLLABLE_LVT + JAMO_T * 3000,
    "GB6 and GB7: L, LV, then V and V": JAMO_L + SYLLABLE_LV + JAMO_V * 3000,
    "GB7 and GB8: LV, then V, V, T and T": SYLLABLE_LV + JAMO_V * 1500 + JAMO_T * 1500,
    "GB9: a base and combining marks": "e" + ACUTE * 3000,
    "GB9a: a base and spacing marks": KA + VISARGA * 3000,
    "GB9: ZWNJ extends": "a" + ZWNJ * 3000,
    "GB9: ZWJ extends": ZWJ * 3000,
    "GB9: halfwidth voiced marks (Lm, but Extend)": HALFWIDTH_KA + HALFWIDTH_VOICED * 3000,
    "GB9: variation selectors": "a" + "\ufe0f" * 3000,
    "GB9: tag characters": "\U0001f3f4" + "\U000e0067" * 3000,
    "GB9: emoji modifiers": "\U0001f44d" + "\U0001f3fd" * 3000,
    "GB9b: prepend characters": "\u0600" * 3000,
    "GB9c: a conjunct chain, consonant virama consonant ...": (KA + VIRAMA) * 2500 + KA,
    "GB11: an emoji ZWJ chain": WOMAN + (ZWJ + WOMAN) * 1000,
}


@pytest.mark.parametrize("text", CLUSTERS.values(), ids=list(CLUSTERS))
def test_each_sample_is_a_single_cluster_by_uax_29(text: str) -> None:
    # The premise of the tests below, stated about the segmenter and not about our code.
    assert len(regex.findall(r"\X", text)) <= 2


class TestGraphemeClusters:
    @pytest.mark.parametrize("text", CLUSTERS.values(), ids=list(CLUSTERS))
    def test_no_cluster_stays_longer_than_the_cap(self, text: str) -> None:
        shown = display_text(text)

        assert shown != text
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

    def test_a_hangul_syllable_boundary_is_not_a_cluster_and_is_not_split(self) -> None:
        # T then L breaks (GB999), so a long run of syllables is many short clusters.
        text = (JAMO_L + JAMO_V + JAMO_T) * (MAX_RUN // 3 - 1)

        assert display_text(text) is text

    def test_the_break_character_is_a_format_character_and_not_a_mark(self) -> None:
        # U+200B has Grapheme_Cluster_Break=Control (and Line_Break=ZW), which is what ends a
        # cluster and offers a line break; it is not itself one that extends.
        assert unicodedata.category(Z) == "Cf"
        assert unicodedata.category(Z) not in {"Mn", "Mc", "Me"}
        assert len(regex.findall(r"\X", "a" + Z + ACUTE)) == 3  # the break ends the cluster


REAL_BREAKS = {
    "space": " ",
    "tab": "\t",
    "line feed": "\n",
    "line tabulation": "\v",
    "form feed": "\f",
    "carriage return": "\r",
    "next line": "\u0085",
    "ogham space": "\u1680",
    "en quad": "\u2000",
    "em quad": "\u2001",
    "en space": "\u2002",
    "em space": "\u2003",
    "three-per-em space": "\u2004",
    "four-per-em space": "\u2005",
    "six-per-em space": "\u2006",
    "punctuation space": "\u2008",
    "thin space": "\u2009",
    "hair space": "\u200a",
    "zero width space": "\u200b",
    "line separator": "\u2028",
    "paragraph separator": "\u2029",
    "medium mathematical space": "\u205f",
    "ideographic space": "\u3000",
}
NOT_BREAKS = {
    "no-break space": "\u00a0",
    "narrow no-break space": "\u202f",
    "figure space": "\u2007",
    "word joiner": "\u2060",
    "zero width no-break space": "\ufeff",
    "unit separator": "\u001f",
    "group separator": "\u001e",
    "record separator": "\u001d",
    "file separator": "\u001c",
    "escape": "\u001b",
    "a C1 control": "\u0090",
    "soft hyphen": "\u00ad",
    "a letter": "x",
}


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

    @pytest.mark.parametrize("char", REAL_BREAKS.values(), ids=list(REAL_BREAKS))
    def test_a_real_break_resets_the_run(self, char: str) -> None:
        text = ("a" * (MAX_RUN - 1) + char) * 10

        assert display_text(text) is text

    @pytest.mark.parametrize("char", NOT_BREAKS.values(), ids=list(NOT_BREAKS))
    def test_what_is_not_a_break_opportunity_does_not_reset_the_run(self, char: str) -> None:
        text = ("a" * (MAX_RUN - 1) + char) * 10

        shown = display_text(text)

        assert shown != text
        assert shown.replace(Z, "") == text
        assert all(len(piece) <= MAX_RUN for piece in shown.split(Z))

    @pytest.mark.parametrize("char", NOT_BREAKS.values(), ids=list(NOT_BREAKS))
    @pytest.mark.parametrize("size", [16 * 1024, 64 * 1024])
    def test_text_joined_by_it_is_broken_into_runs_of_at_most_max_run(
        self, char: str, size: int
    ) -> None:
        text = ("a" + char) * (size // (1 + len(char.encode())))

        shown = display_text(text)

        assert all(len(piece) <= MAX_RUN for piece in shown.split(Z))
        assert shown.replace(Z, "") == text

    def test_the_sample_chars_really_are_what_the_tables_say(self) -> None:
        # The review found these via str.isspace(), which is true for the first three and for the
        # separators U+001C to U+001F: none of them is a line break opportunity.
        for char in ("\u00a0", "\u2007", "\u202f", "\u001f"):
            assert char.isspace()
            assert char in NOT_BREAKS.values()
        assert not (set(REAL_BREAKS.values()) & set(NOT_BREAKS.values()))

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
            (KA + VIRAMA) * 400 + KA,
            "a\u00a0" * 600,
            "a\u001f" * 600,
            HALFWIDTH_KA + HALFWIDTH_VOICED * 600,
        ],
        ids=[
            "jamo",
            "hex",
            "words",
            "zwj",
            "marks",
            "conjuncts",
            "nbsp",
            "unit separator",
            "halfwidth",
        ],
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

    def test_a_conjunct_chain_split_between_pieces_is_still_one_cluster(self) -> None:
        feeder = DisplayText()
        parts = [KA + VIRAMA, KA, VIRAMA + KA, VIRAMA] * 20

        shown = "".join(feeder.feed(part) for part in parts)

        assert shown == display_text("".join(parts))
        assert max(len(piece) for piece in shown.split(Z)) <= MAX_CLUSTER

    def test_the_added_breaks_are_reported_where_they_are(self) -> None:
        feeder = DisplayText()
        shown, added = feeder.feed_marked("ab" * 400)

        assert [i for i, char in enumerate(shown) if char == Z] == list(added)
        assert len(added) == 3


SAMPLES = {
    "hex": "ab" * 40_000,
    "jamo": JAMO_L * 30_000,
    "prose with paragraphs": ("A sentence of some length. " * 40 + "\n") * 60,
    "prose with no newlines": "word " * 20_000,
    "one long line of spaces and words": ("x" * 100 + " ") * 2000,
    "mixed": ("ab" * 3000 + "\n" + JAMO_L * 3000 + " " + "word " * 500) * 5,
    "newlines only": "\n" * 20_000,
    "tabs as the only breaks": ("w" * 200 + "\t") * 200,
    "ideographic spaces": ("w" * 200 + "\u3000") * 200,
    "no-break spaces": "a\u00a0" * 30_000,
    "unit separators": "a\u001f" * 30_000,
    "short": "hello",
    "empty": "",
}


class TestChunks:
    @pytest.mark.parametrize("text", SAMPLES.values(), ids=list(SAMPLES))
    def test_the_chunks_are_the_display_text_cut_up_and_all_small(self, text: str) -> None:
        chunks = DisplayChunks()
        chunks.feed(text)

        assert chunks.text == display_text(text)
        assert all(len(chunk) <= MAX_CHUNK for chunk in chunks.chunks)

    def test_text_that_fits_stays_one_chunk(self) -> None:
        chunks = DisplayChunks()
        chunks.feed("a" * 100)

        assert chunks.chunks == ("a" * 100,)

    def test_a_message_with_nothing_yet_has_one_empty_chunk(self) -> None:
        assert DisplayChunks().chunks == ("",)

    def test_exactly_max_chunk_characters_are_one_chunk_and_one_more_makes_two(self) -> None:
        exact = ("ab " * (MAX_CHUNK // 3 + 1))[:MAX_CHUNK]
        over = exact + "c"
        assert (len(exact), len(over)) == (MAX_CHUNK, MAX_CHUNK + 1)
        assert display_text(over) is over  # spaces keep the runs short: the cut is the chunker's

        one = DisplayChunks()
        one.feed(exact)
        two = DisplayChunks()
        two.feed(over)

        assert one.chunks == (exact,)
        assert len(two.chunks) == 2
        assert "".join(two.chunks) == over
        assert all(len(chunk) <= MAX_CHUNK for chunk in two.chunks)

    def test_the_cut_falls_on_the_boundary_whichever_side_the_last_character_arrives(self) -> None:
        for size in (MAX_CHUNK - 1, MAX_CHUNK, MAX_CHUNK + 1, MAX_CHUNK + 2):
            chunks = DisplayChunks()
            chunks.feed(("ab " * MAX_CHUNK)[:size])

            assert len(chunks.chunks) == (1 if size <= MAX_CHUNK else 2), size

    @pytest.mark.parametrize("name", ["tabs as the only breaks", "ideographic spaces"])
    def test_a_chunk_may_end_at_any_real_break(self, name: str) -> None:
        chunks = DisplayChunks()
        chunks.feed(SAMPLES[name])

        assert len(chunks.chunks) > 1
        breaks = ("\t", "\u3000")
        assert all(chunk.endswith(breaks) for chunk in chunks.chunks[:-1])

    def test_without_line_ends_a_chunk_ends_at_a_space_or_a_zero_width_space(self) -> None:
        words = DisplayChunks()
        words.feed("word " * 5000)
        hexed = DisplayChunks()
        hexed.feed("ab" * 20_000)

        assert all(chunk.endswith(" ") for chunk in words.chunks[:-1])
        assert all(chunk.endswith(Z) for chunk in hexed.chunks[:-1])

    @pytest.mark.parametrize(
        "separator", ["\n", "\r\n", "\r", "\u2028", "\u2029", "\v", "\f", "\u0085"]
    )
    def test_a_chunk_cut_at_a_line_end_does_not_show_it(self, separator: str) -> None:
        text = ("word " * 30 + separator) * 60
        chunks = DisplayChunks()
        chunks.feed(text)

        assert len(chunks.chunks) > 1
        for chunk in chunks.chunks[:-1]:
            assert not chunk.endswith(("\n", "\r", "\v", "\f", "\u0085", "\u2028", "\u2029"))
            assert chunk.endswith(" ")  # the last word's space: the line end itself is gone
        # Nothing but the line ends was dropped: each boundary stands for exactly one of them.
        assert "".join(c + separator for c in chunks.chunks[:-1]) + chunks.chunks[-1] == text

    def test_a_chunk_never_ends_between_a_carriage_return_and_its_line_feed(self) -> None:
        text = ("x" * 2047 + "\r\n") * 6  # the CR lands on the edge of the range
        chunks = DisplayChunks()
        chunks.feed(text)

        assert len(chunks.chunks) > 1
        assert all(not chunk.endswith(("\r", "\n")) for chunk in chunks.chunks[:-1])
        shown = "".join(chunk + "\r\n" for chunk in chunks.chunks[:-1]) + chunks.chunks[-1]
        assert shown == chunks.text

    def test_a_chunk_is_never_cut_inside_a_grapheme_cluster(self) -> None:
        # The only breaks in range are spaces that a combining mark has glued to: no cut there.
        text = ("a" * 50 + " " + ACUTE) * 120
        chunks = DisplayChunks()
        chunks.feed(text)
        starts = {match.start() for match in regex.finditer(r"\X", chunks.text)}

        assert len(chunks.chunks) > 1
        position = 0
        for chunk in chunks.chunks[:-1]:
            position += len(chunk)
            assert position in starts  # a cut lies on a cluster boundary of the whole text

    def test_the_cut_after_a_space_that_nothing_glues_to_is_taken(self) -> None:
        chunks = DisplayChunks()
        chunks.feed(("a" * 50 + " ") * 120)

        assert all(chunk.endswith(" ") for chunk in chunks.chunks[:-1])

    @pytest.mark.parametrize("piece", [1, 13, 500, 5000])
    @pytest.mark.parametrize(
        "name", ["hex", "prose with paragraphs", "mixed", "jamo", "no-break spaces"]
    )
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
        assert pieces.text == whole.text

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


class TestSourceText:
    def source_of_all(self, chunks: DisplayChunks) -> str:
        return "".join(
            chunks.source_text(index, 0, len(chunk)) for index, chunk in enumerate(chunks.chunks)
        )

    @pytest.mark.parametrize(
        "name", ["hex", "jamo", "no-break spaces", "unit separators", "tabs as the only breaks"]
    )
    def test_taking_every_chunk_whole_gives_back_the_source_exactly(self, name: str) -> None:
        text = SAMPLES[name]
        chunks = DisplayChunks()
        chunks.feed(text)

        assert Z in chunks.text or name == "tabs as the only breaks"
        assert self.source_of_all(chunks) == text

    def test_a_zero_width_space_the_message_had_is_kept(self) -> None:
        text = "a" * 300 + Z + "b" * 600 + Z + "c" * 300
        chunks = DisplayChunks()
        chunks.feed(text)

        assert chunks.text != text  # some were added
        assert self.source_of_all(chunks) == text  # and only those were taken out

    def test_a_selection_across_an_added_break_gives_the_source_between_its_ends(self) -> None:
        text = "ab" * 400
        shown = display_text(text)
        chunks = DisplayChunks()
        chunks.feed(text)
        assert Z in shown[250:270]

        selected = chunks.source_text(0, 250, 270)

        assert selected == shown[250:270].replace(Z, "")
        assert Z not in selected

    def test_a_selection_in_a_later_chunk_counts_from_that_chunk(self) -> None:
        text = "ab" * 9000
        chunks = DisplayChunks()
        chunks.feed(text)
        assert len(chunks.chunks) >= 3
        start_of_third = sum(len(chunk) for chunk in chunks.chunks[:2])

        selected = chunks.source_text(2, 0, 40)

        assert selected == chunks.text[start_of_third : start_of_third + 40].replace(Z, "")

    def test_a_selection_past_the_end_is_clamped(self) -> None:
        chunks = DisplayChunks()
        chunks.feed("hello")

        assert chunks.source_text(0, 0, 100) == "hello"
        assert chunks.source_text(0, 3, 2) == ""


def test_the_constants_leave_room_for_real_text() -> None:
    assert MAX_CLUSTER >= 10  # the longest emoji ZWJ sequences
    assert MAX_RUN >= 128  # more than any word
