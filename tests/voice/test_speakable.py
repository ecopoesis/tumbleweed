"""What is spoken of a reply (S07): Markdown and code are stripped, the text is made safe, and it is
chunked into sentences as it streams, the same whatever size the pieces of the stream are."""

import time

import pytest

from tumbleweed.voice.speakable import (
    MIN_CHUNK_CHARS,
    SentenceChunker,
    clean_speech,
    strip_inline,
)


def chunks(pieces: list[str], *, limit: int = 400) -> list[str]:
    chunker = SentenceChunker(max_sentence_chars=limit)
    spoken: list[str] = []
    for piece in pieces:
        spoken += chunker.feed(piece)
    return spoken + chunker.finish()


def speak(text: str, *, limit: int = 400) -> list[str]:
    """`text` fed whole, and fed one character at a time: the answer must be the same."""
    whole = chunks([text], limit=limit)
    assert chunks(list(text), limit=limit) == whole
    return whole


class TestSentences:
    def test_a_reply_is_cut_at_sentence_ends(self) -> None:
        assert speak("The first sentence is here. The second one follows! Is there a third?") == [
            "The first sentence is here.",
            "The second one follows!",
            "Is there a third?",
        ]

    def test_the_first_sentence_is_out_before_the_rest_has_arrived(self) -> None:
        chunker = SentenceChunker(max_sentence_chars=400)

        assert chunker.feed("The first sentence is here") == []
        assert chunker.feed(". And the sec") == ["The first sentence is here."]
        assert chunker.feed("ond one") == []
        assert chunker.finish() == ["And the second one"]

    def test_a_full_stop_inside_a_number_or_a_name_is_not_an_end(self) -> None:
        assert speak("Pi is 3.14159 and the file is notes.txt in this folder.") == [
            "Pi is 3.14159 and the file is notes.txt in this folder."
        ]

    def test_a_line_end_ends_a_sentence(self) -> None:
        assert speak("A heading with no stop\nThen a sentence that follows it.\n") == [
            "A heading with no stop",
            "Then a sentence that follows it.",
        ]

    def test_a_short_heading_is_joined_to_what_follows_with_a_pause(self) -> None:
        assert speak("# Notes\nThen a sentence that follows it.\n") == [
            "Notes. Then a sentence that follows it."
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "Ask Dr. Smith about the plan, e.g. tomorrow morning.",
            "Mr. and Mrs. Jones are here, vs. the others, etc. and so on.",
            "That is, i.e. the one that we want, is here today.",
        ],
    )
    def test_an_abbreviation_does_not_end_a_sentence(self, text: str) -> None:
        assert speak(text) == [text]

    def test_an_abbreviation_is_one_when_marked_up_too(self) -> None:
        text = "We use **Dr.** Who and _e.g._ items here. Next one now."

        assert speak(text) == ["We use Dr. Who and e.g. items here.", "Next one now."]

    @pytest.mark.parametrize(
        "ending", ["5 (ms).", "5 `ms`.", "5 [ms].", "5 *vs*.", "5 _Dr_.", "5 (etc.)."]
    )
    def test_a_stop_after_a_word_that_closes_a_mark_still_ends_the_sentence(
        self, ending: str
    ) -> None:
        # The mark before the stop ("(ms)." and so on) means the word is not an abbreviation's.
        text = f"Timing is {ending} Then more words follow here."

        spoken = speak(text)

        assert len(spoken) == 2
        assert spoken[1] == "Then more words follow here."

    def test_a_sentence_ends_where_a_closing_mark_follows_the_stop(self) -> None:
        assert speak("This is **done now.** Then the next one starts here.") == [
            "This is done now.",
            "Then the next one starts here.",
        ]

    def test_a_short_sentence_waits_for_the_next(self) -> None:
        assert speak("Sure. I can do that for you right now.") == [
            "Sure. I can do that for you right now."
        ]
        assert len("Sure.") < MIN_CHUNK_CHARS

    def test_a_short_reply_is_spoken_at_the_end(self) -> None:
        assert speak("Sure.") == ["Sure."]

    def test_a_long_run_with_no_boundary_is_cut_at_a_space_within_the_limit(self) -> None:
        spoken = speak(" ".join(["word"] * 40), limit=50)

        assert len(spoken) > 1
        assert all(len(sentence) <= 50 for sentence in spoken)
        assert " ".join(spoken) == " ".join(["word"] * 40)

    def test_a_run_with_no_space_at_all_is_cut_at_the_limit(self) -> None:
        spoken = speak("x" * 130, limit=50)

        assert [len(sentence) for sentence in spoken] == [50, 50, 30]

    def test_the_text_waiting_for_a_boundary_is_bounded(self) -> None:
        chunker = SentenceChunker(max_sentence_chars=60)
        for _ in range(10_000):
            chunker.feed("abcdefghij ")

        assert len(chunker._text) < 60  # noqa: SLF001  (the bound is the point)


class TestMarkdown:
    def test_a_fenced_code_block_is_not_spoken(self) -> None:
        reply = "Run this first.\n```python\nprint('hi')\nrm -rf /\n```\nThen it is done now."

        assert speak(reply) == ["Run this first.", "Then it is done now."]

    def test_an_unclosed_fence_drops_the_rest_of_the_reply(self) -> None:
        assert speak("Here it is now.\n```\nstuff\nmore stuff. Still code. Yes.\n") == [
            "Here it is now."
        ]

    def test_a_tilde_fence_and_an_indented_one_are_fences(self) -> None:
        assert speak("Before the code.\n  ~~~\n  code\n  ~~~\nAfter the code is spoken.") == [
            "Before the code.",
            "After the code is spoken.",
        ]

    def test_a_fence_split_across_pieces_is_still_a_fence(self) -> None:
        pieces = ["Before the code.\n``", "`py\nsecret code\n`", "``\nAfter it is fine."]

        assert chunks(pieces) == ["Before the code.", "After it is fine."]

    def test_a_longer_fence_is_not_closed_by_a_shorter_one(self) -> None:
        reply = "````\n```\nprint('secret')\n```\nstill code\n````\nDone now, all good."

        assert speak(reply) == ["Done now, all good."]

    def test_a_closing_fence_may_be_longer_and_followed_by_blanks_only(self) -> None:
        assert speak("Before the code.\n```\ncode\n`````   \nAfter the code is spoken.") == [
            "Before the code.",
            "After the code is spoken.",
        ]

    def test_a_shorter_run_with_blanks_after_it_does_not_close_a_longer_fence_either(self) -> None:
        reply = "````\n```  \nsecret code\n```\nmore secret\n````\nDone now, all good."

        assert speak(reply) == ["Done now, all good."]

    def test_a_backtick_line_inside_a_tilde_fence_does_not_close_it(self) -> None:
        reply = "Before the code.\n~~~\n```\nsecret code\n```\n~~~\nAfter it all is spoken."

        assert speak(reply) == ["Before the code.", "After it all is spoken."]

    def test_a_tilde_line_inside_a_backtick_fence_does_not_close_it(self) -> None:
        reply = "Before the code.\n```\n~~~\nsecret code\n~~~\n```\nAfter it all is spoken."

        assert speak(reply) == ["Before the code.", "After it all is spoken."]

    def test_the_opening_length_counts_with_an_info_string_too(self) -> None:
        reply = "Before the code.\n````python\n```\nsecret()\n```\n````\nAfter it all is spoken."

        assert speak(reply) == ["Before the code.", "After it all is spoken."]

    def test_a_huge_run_of_fence_marks_is_bounded_and_fast(self) -> None:
        chunker = SentenceChunker(max_sentence_chars=400)
        started = time.monotonic()

        chunker.feed("`" * 70_000)  # one line of marks, no end yet

        assert chunker._probe == ""  # noqa: SLF001  (the bound is the point: a count, no string)
        assert chunker._run_len == 70_000  # noqa: SLF001
        inside = SentenceChunker(max_sentence_chars=400)
        inside.feed("```\n" + "`" * 70_000)  # a run of marks on a line inside a fence
        assert inside._probe == ""  # noqa: SLF001
        assert time.monotonic() - started < 30  # linear: a quadratic copy would take far longer
        reply = (
            "Before it is here.\n"
            + "`" * 70_000
            + "\ncode\n"
            + "`" * 70_000
            + "\nAfter it is done."
        )
        assert speak(reply) == ["Before it is here.", "After it is done."]

    @pytest.mark.parametrize("mark", ["`", "~"])
    @pytest.mark.parametrize(
        ("opener", "inner"), [(200, 100), (65, 64), (64, 63), (10, 9), (4, 3), (130, 129)]
    )
    def test_a_fence_closes_only_on_a_run_as_long_as_its_opener(
        self, mark: str, opener: int, inner: int
    ) -> None:
        reply = (
            f"Before the code.\n{mark * opener}\nsecret one here.\n{mark * inner}\n"
            f"secret two here.\n{mark * inner} \nsecret three here.\n{mark * opener}\n"
            "After it all is spoken."
        )

        assert speak(reply) == ["Before the code.", "After it all is spoken."]

    @pytest.mark.parametrize("mark", ["`", "~"])
    @pytest.mark.parametrize("opener", [3, 4, 65, 200])
    def test_a_run_longer_than_the_opener_closes_it_and_so_does_the_exact_one(
        self, mark: str, opener: int
    ) -> None:
        for closer in (opener, opener + 1, opener + 70):
            reply = (
                f"Before the code.\n{mark * opener}py\nsecret here.\n{mark * closer}  \n"
                "After it all is spoken."
            )

            assert speak(reply) == ["Before the code.", "After it all is spoken."]

    def test_a_fence_line_with_words_after_the_marks_does_not_close_it(self) -> None:
        assert speak(
            "Before the code.\n```\ncode\n``` not a close\nmore code\n```\nAfter it all."
        ) == [
            "Before the code.",
            "After it all.",
        ]

    def test_a_fence_that_is_one_line_with_no_info_string_opens_at_the_line_end(self) -> None:
        assert speak("Before the code.\n```\nsecret code here\n```\n") == ["Before the code."]

    def test_a_fence_needs_all_three_marks(self) -> None:
        assert speak("``not a fence`` is how this reads, really.") == [
            "not a fence is how this reads, really."
        ]

    def test_a_fence_line_inside_a_fence_of_the_other_kind_does_not_close_it(self) -> None:
        assert speak("Start here now.\n```\n~~~\nstill code\n```\nBack to prose here.") == [
            "Start here now.",
            "Back to prose here.",
        ]

    def test_headings_bullets_and_quotes_lose_their_marks(self) -> None:
        reply = (
            "# A heading here\n- first item here\n* second item here\n+ third item here\n"
            "> a quote line here\n"
        )

        assert speak(reply) == [
            "A heading here",
            "first item here",
            "second item here",
            "third item here",
            "a quote line here",
        ]

    def test_a_dash_or_star_that_is_not_a_bullet_is_kept_as_text(self) -> None:
        assert speak("-5 degrees is cold, and **bold** is loud.") == [
            "-5 degrees is cold, and bold is loud."
        ]

    def test_emphasis_and_code_marks_go_and_the_words_stay(self) -> None:
        assert speak("This is **bold**, *slanted*, ~~gone~~, _soft_ and `code` text.") == [
            "This is bold, slanted, gone, soft and code text."
        ]

    def test_underscores_inside_names_stay(self) -> None:
        assert speak("Call snake_case_name and __init__ now please.") == [
            "Call snake_case_name and init now please."
        ]

    def test_a_link_keeps_its_text_and_loses_its_address(self) -> None:
        assert speak("See [the docs](https://example.com/a?b=c) for more.") == [
            "See the docs for more."
        ]

    def test_an_image_keeps_its_alt_text(self) -> None:
        assert speak("Here: ![a cat on a mat](https://example.com/cat.png) it is.") == [
            "Here: a cat on a mat it is."
        ]

    def test_a_bare_address_becomes_the_word_link(self) -> None:
        assert speak("Visit https://example.com/some/long/path?x=1 today please.") == [
            "Visit link today please."
        ]

    def test_html_tags_go(self) -> None:
        assert speak("A <b>bold</b> claim and a <br/> break are here.") == [
            "A bold claim and a break are here."
        ]

    def test_table_pipes_go(self) -> None:
        assert "|" not in " ".join(speak("| name | value |\n|---|---|\n| a | b |\n"))

    def test_a_cut_never_leaves_a_piece_of_punctuation_alone(self) -> None:
        for limit in range(40, 60):
            for sentence in speak("x" * (limit - 1) + " ... --- !!! ???", limit=limit):
                assert any(c.isalnum() for c in sentence)

    def test_splitting_text_never_leaves_a_piece_of_punctuation_alone(self) -> None:
        chunker = SentenceChunker(max_sentence_chars=40)
        out: list[str] = []

        chunker._split("a " + ". " * 60 + "end", out)  # noqa: SLF001  (the splitting step itself)

        assert out
        assert all(any(c.isalnum() for c in piece) for piece in out)
        assert all(len(piece) <= 40 for piece in out)

    def test_text_with_no_letter_or_digit_is_not_spoken(self) -> None:
        assert speak("---\n***\n...\n") == []

    def test_strip_inline_leaves_plain_text_alone(self) -> None:
        assert strip_inline("plain text, nothing to do.") == "plain text, nothing to do."


class TestSafety:
    def test_controls_become_spaces_and_format_characters_go(self) -> None:
        assert clean_speech("bell\x07tab\there\u202eevil\u200bwords\x00end") == (
            "bell tab hereevilwords end"
        )

    def test_a_bidi_override_cannot_reach_a_spoken_sentence(self) -> None:
        spoken = speak("Read ‮txet this‬ carefully, it is odd.")

        assert all(not any(ord(c) in range(0x202A, 0x202F) for c in s) for s in spoken)

    def test_lone_surrogates_and_private_use_are_dropped(self) -> None:
        assert clean_speech("a\ud800bc") == "abc"

    def test_a_sentence_is_one_line(self) -> None:
        for sentence in speak("one\r\ntwo\u2028three\u2029four\x0bfive\x0csix is the way.\n"):
            assert sentence == clean_speech(sentence)
            assert not any(c in sentence for c in "\r\n\u2028\u2029\x0b\x0c")

    def test_text_is_encodable(self) -> None:
        for sentence in speak("odd \ud800 text, with a lone surrogate in it.\n"):
            sentence.encode("utf-8")

    @pytest.mark.parametrize("text", ["", " ", "\n\n", "\x00\x01"])
    def test_nothing_is_nothing(self, text: str) -> None:
        assert speak(text) == []
