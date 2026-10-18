"""`clean_transcript`: whatever a backend returns is made safe before it can enter the chat."""

import pytest

from tests.voice.support import max_bytes, pcm
from tumbleweed.core.types import ByteCount
from tumbleweed.voice.audio import Utterance
from tumbleweed.voice.errors import SttResponseError
from tumbleweed.voice.fake import FakeStt, Heard
from tumbleweed.voice.stt import clean_transcript, is_language_code

CAP = ByteCount(1000)


class TestCleanTranscript:
    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("hello", "hello"),
            ("  hello world \n", "hello world"),
            ("one\ntwo\r\nthree\tfour", "one two three four"),
            ("a    b", "a b"),
            ("café 日本語 \U0001f600", "café 日本語 \U0001f600"),
            ("a\x00b", "a b"),
            ("a\x1b[31mred", "a [31mred"),
            ("a\x85b\x9bc", "a b c"),
            ("evil\N{RIGHT-TO-LEFT OVERRIDE}txt.exe", "eviltxt.exe"),
            ("zero\N{ZERO WIDTH SPACE}width\N{ZERO WIDTH NO-BREAK SPACE}space", "zerowidthspace"),
            ("soft\N{SOFT HYPHEN}hyphen", "softhyphen"),
            ("line\N{LINE SEPARATOR}sep\N{PARAGRAPH SEPARATOR}para", "line sep para"),
            (
                "a\N{ZERO WIDTH JOINER}b\N{ZERO WIDTH NON-JOINER}c",
                "a\N{ZERO WIDTH JOINER}b\N{ZERO WIDTH NON-JOINER}c",
            ),
            (
                "\N{MAN}\N{ZERO WIDTH JOINER}\N{WOMAN}\N{ZERO WIDTH JOINER}\N{GIRL}",
                "\N{MAN}\N{ZERO WIDTH JOINER}\N{WOMAN}\N{ZERO WIDTH JOINER}\N{GIRL}",
            ),
            ("<b>bold</b> & %s {x}", "<b>bold</b> & %s {x}"),  # text stays text
        ],
    )
    def test_text_is_cleaned_not_interpreted(self, raw: str, clean: str) -> None:
        assert clean_transcript(raw, max_bytes=CAP) == clean

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   \n\t ",
            "\x00\x00",
            "\N{ZERO WIDTH SPACE}\N{RIGHT-TO-LEFT OVERRIDE}",
            "[BLANK_AUDIO]",
            " [BLANK_AUDIO]\n",
            "(silence)",
            "[ Silence ]",
            "[MUSIC] [MUSIC]",
            "(upbeat music) [applause]",
            "[MUSIC PLAYING]",
            "(soft gentle music)",
            "[ Background noise ]",
            "[Blank_Audio]",
        ],
    )
    def test_nothing_said_is_none(self, raw: str) -> None:
        assert clean_transcript(raw, max_bytes=CAP) is None

    @pytest.mark.parametrize(
        "raw",
        [
            "[sic] he said",
            "hello [BLANK_AUDIO]",
            "(1) first",
            "see [1]",
            "a (b) c",
            "(1)",
            "[sic]",
            "(yes)",
        ],
    )
    def test_brackets_among_words_are_speech(self, raw: str) -> None:
        assert clean_transcript(raw, max_bytes=CAP) == raw

    def test_exactly_the_cap_is_fine_and_one_byte_over_is_refused(self) -> None:
        assert clean_transcript("a" * 1000, max_bytes=CAP) == "a" * 1000

        with pytest.raises(SttResponseError, match="too long"):
            clean_transcript("a" * 1001, max_bytes=CAP)

    def test_the_cap_counts_utf8_bytes_not_characters(self) -> None:
        with pytest.raises(SttResponseError, match="too long"):
            clean_transcript("é" * 501, max_bytes=CAP)  # 1002 bytes

    def test_the_cap_applies_before_cleaning(self) -> None:
        with pytest.raises(SttResponseError, match="too long"):
            clean_transcript(" " * 1001 + "x", max_bytes=CAP)

    def test_a_lone_surrogate_is_refused(self) -> None:
        with pytest.raises(SttResponseError, match="not valid text"):
            clean_transcript("ok\ud800", max_bytes=CAP)

    @pytest.mark.parametrize("bad", [None, 5, b"bytes", ["a"], 1.5])
    def test_a_backend_that_returns_something_else_is_refused(self, bad: object) -> None:
        with pytest.raises(SttResponseError, match="not text"):
            clean_transcript(bad, max_bytes=CAP)  # type: ignore[arg-type]  # the point

    def test_a_str_subclass_is_refused(self) -> None:
        class Sneaky(str):
            __slots__ = ()

        with pytest.raises(SttResponseError, match="not text"):
            clean_transcript(Sneaky("hi"), max_bytes=CAP)

    def test_the_error_does_not_repeat_the_text(self) -> None:
        with pytest.raises(SttResponseError) as raised:
            clean_transcript("SECRET" * 500, max_bytes=CAP)

        assert "SECRET" not in str(raised.value)


class TestLanguageCode:
    @pytest.mark.parametrize("text", ["auto", "en", "de", "yue", "haw"])
    def test_codes(self, text: str) -> None:
        assert is_language_code(text)

    @pytest.mark.parametrize("text", ["", "EN", "english", "e", "auto ", "a1", "../x", "én"])
    def test_not_codes(self, text: str) -> None:
        assert not is_language_code(text)

    @pytest.mark.parametrize("bad", [None, 5, b"en"])
    def test_not_text(self, bad: object) -> None:
        assert not is_language_code(bad)  # type: ignore[arg-type]  # the point


class TestFakeStt:
    def test_it_plays_its_script_and_keeps_what_it_heard(self) -> None:
        stt = FakeStt([Heard("one"), "two"])
        utterance = Utterance(pcm(1), max_bytes=max_bytes(2))

        assert [stt.transcribe(utterance), stt.transcribe(utterance)] == ["one", "two"]
        assert stt.heard == [pcm(1), pcm(1)]

    def test_it_raises_what_it_is_told_to(self) -> None:
        from tumbleweed.voice.errors import SttError

        stt = FakeStt([SttError("down")])

        with pytest.raises(SttError, match="down"):
            stt.transcribe(Utterance(pcm(1), max_bytes=max_bytes(2)))
