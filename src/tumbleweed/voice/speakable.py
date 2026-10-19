"""What of a reply is spoken: Markdown and code are stripped, the text is made safe, and it is cut
into sentences as it streams.

A reply is untrusted model output. Before any of it reaches a synthesiser it goes through
`SentenceChunker`, which does three things in one pass over the stream:

- *Strips Markdown.* Fenced code blocks (a ``` or ~~~ fence, to its closing fence or the end of the
  reply) are dropped entirely; so are heading marks, list bullets, quote marks and table pipes.
  Inline, links and images keep their text and lose their address, bare URLs become "link", HTML
  tags go, and emphasis and backtick marks go (the words of an inline code span stay: they are part
  of the sentence). Whatever has no letter or digit left is not spoken.
- *Makes it safe.* Control characters become spaces, format characters (right-to-left overrides,
  zero-width spaces, which change what a listener is told) and lone surrogates are dropped, and
  whitespace collapses to single spaces, so a sentence is one line of printable text (a newline
  would make a synthesiser read it as two utterances).
- *Chunks it.* A sentence ends at `.`, `!`, `?` or an ellipsis followed by whitespace, or at a line
  end, so the first sentence can be spoken while the rest is still arriving. A sentence is at most
  `max_sentence_chars` (cut at a space where there is one), and one shorter than `MIN_CHUNK_CHARS`
  waits for the next so that the synthesiser is not started for "Sure." alone. Memory is bounded:
  a text with no boundary is cut at `max_sentence_chars`, and the block-level state is bounded: the
  first few characters of a line, and a run of fence marks held as a character and a count (an
  integer, O(1) however long the run, and exact: a fence closes only on a run at least as long as
  its opener).

It takes the stream as it comes, in pieces of any size: block syntax is decided at the start of a
line from the first few characters, so a fence split across two deltas is still a fence.
"""

import enum
import re
import unicodedata
from typing import Final, final

MIN_CHUNK_CHARS: Final = 12
_TERMINATED: Final = re.compile(r"[.!?\u2026]+[\"'\u201d\u2019)\]*_`~]*\Z")
# A full stop after one of these is not the end of a sentence.
_ABBREVIATIONS: Final = frozenset(
    {"mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "e.g", "i.e", "etc", "vs"}
)
_CLOSERS: Final = "\"'\u201d\u2019)]*_`~"
_LINK: Final = re.compile(r"!?\[([^\]\n]{0,300})\]\([^)\n]{0,1000}\)")
_REFERENCE_LINK: Final = re.compile(r"\[([^\]\n]{0,300})\]\[[^\]\n]{0,100}\]")
_URL: Final = re.compile(r"https?://[^\s<>)\]]{1,2000}")
_HTML_TAG: Final = re.compile(r"</?[A-Za-z][^<>\n]{0,300}>")
_MARKS: Final = re.compile(r"[*`]+|~~|(?<!\w)_+(?=\S)|(?<=\S)_+(?!\w)")
_TABLE_PIPES: Final = re.compile(r"\|+")
_DROPPED: Final = frozenset({"Cf", "Cs", "Co", "Cn"})
_BLANKS: Final = frozenset({"Cc", "Zl", "Zp"})
_FENCE_CHARS: Final = "`~"
_FENCE_MIN: Final = 3
_BULLET_CHARS: Final = "-*+"


def clean_speech(text: str) -> str:
    """`text` as one line of safe, printable text: controls to spaces, format characters, lone
    surrogates and unassigned code points dropped, whitespace collapsed. May be empty."""
    kept: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category in _BLANKS:
            kept.append(" ")
        elif category not in _DROPPED:
            kept.append(char)
    return " ".join("".join(kept).split())


def strip_inline(text: str) -> str:
    """`text` without inline Markdown, as the module docstring describes. Not yet cleaned."""
    text = _LINK.sub(r"\1", text)
    text = _REFERENCE_LINK.sub(r"\1", text)
    text = _URL.sub("link", text)
    text = _HTML_TAG.sub(" ", text)
    return _TABLE_PIPES.sub(" ", _MARKS.sub("", text))


def _ends_a_sentence(text: str) -> bool:
    """Whether `text` ends in `.`, `!`, `?` or an ellipsis, and not in an abbreviation's stop."""
    if _TERMINATED.search(text) is None:
        return False
    bare = text.rstrip(_CLOSERS)
    if not bare.endswith(".") or bare.endswith(".."):
        return True
    words = bare[:-1].rsplit(None, 1)
    return not words or words[-1].lower().lstrip(_CLOSERS + "([") not in _ABBREVIATIONS


class _Line(enum.Enum):
    START = enum.auto()  # at the start of a line: deciding what kind of line it is
    TEXT = enum.auto()  # prose: its characters are spoken
    DROP = enum.auto()  # to the end of the line: a fence line, or a line inside a fence
    CLOSE = enum.auto()  # a fence's closing run was seen: it closes it if only blanks follow


@final
class SentenceChunker:
    """Markdown-stripping sentence splitter for a streamed reply (see the module docstring).

    `feed` returns the sentences completed by a piece of the stream, `finish` the rest. One
    chunker serves one reply.
    """

    def __init__(self, *, max_sentence_chars: int) -> None:
        self._max = max_sentence_chars
        self._line = _Line.START
        self._probe = ""  # the first characters of a line, while they are not yet understood
        self._run_char = ""  # a run of fence marks at the start of a line: its character...
        self._run_len = 0  # ...and its length, exactly (a count, not a growing string)
        self._fence: tuple[str, int] | None = None  # the character and length of the open fence
        self._skip_space = False  # one space after a quote mark or bullet is part of the mark
        self._text = ""  # prose since the last sentence boundary
        self._carry = ""  # a short sentence waiting for the next

    def feed(self, delta: str) -> list[str]:
        out: list[str] = []
        for char in delta:
            self._char(char, out)
        return out

    def finish(self) -> list[str]:
        out: list[str] = []
        if self._probe and self._fence is None:
            self._text += self._probe  # a line that ended while it was still being understood
        if 0 < self._run_len < _FENCE_MIN and self._fence is None:
            self._text += self._run_char * self._run_len
        self._probe = ""
        self._run_len = 0
        self._cut(out, final=True)
        return out

    # One character at a time

    def _char(self, char: str, out: list[str]) -> None:
        if char == "\n":
            if self._line is _Line.START and (self._probe or self._run_len):
                self._probe_ended_by_newline()
            elif self._line is _Line.CLOSE:
                self._fence = None  # nothing but blanks followed the closing fence
            self._probe = ""
            self._run_len = 0
            self._skip_space = False
            self._cut(out)
            self._line = _Line.START
            return
        if self._line is _Line.CLOSE:
            if not char.isspace():
                self._line = _Line.DROP  # not a closing fence after all: more of the code
            return
        if self._line is _Line.DROP:
            return
        if self._line is _Line.START:
            self._start(char, out)
            return
        self._prose(char, out)

    def _probe_ended_by_newline(self) -> None:
        """A line ended while it was still being understood: a fence line, or short prose."""
        if self._run_len:
            if self._fence is None:
                if self._run_len >= _FENCE_MIN:
                    self._fence = (self._run_char, self._run_len)  # an opener with no info string
                else:
                    self._text += self._run_char * self._run_len
            elif self._run_char == self._fence[0] and self._run_len >= self._fence[1]:
                self._fence = None  # a closing fence
        elif self._fence is None:
            self._text += self._probe

    def _start(self, char: str, out: list[str]) -> None:
        probe = self._probe
        if not probe and not self._run_len and char in " \t":
            return  # indentation
        if self._fence is not None:
            self._start_in_fence(char)
            return
        if self._run_len:
            if char == self._run_char:
                self._run_len += 1  # the run goes on: how long is the fence?
                return
            run_char, run_len = self._run_char, self._run_len
            self._run_len = 0
            if run_len >= _FENCE_MIN:
                self._fence = (run_char, run_len)  # the rest of the line is its info string
                self._line = _Line.DROP
                self._cut(out)  # the prose before a fence is a sentence of its own
                return
            self._begin_prose(run_char * run_len)  # `code` or ``two``: prose after all
            self._prose(char, out)
            return
        if not probe and char in _FENCE_CHARS:
            self._run_char, self._run_len = char, 1
            return
        probe += char
        first = probe[0]
        if first == "#" and probe == "#" * len(probe) and len(probe) <= 6:
            self._probe = probe
            return
        if first == "#" and probe[:-1] == "#" * (len(probe) - 1) and char == " ":
            self._begin_prose("")  # a heading mark
            return
        if first in _BULLET_CHARS and len(probe) == 1:
            self._probe = probe
            return
        if first in _BULLET_CHARS and len(probe) == 2 and char == " ":
            self._begin_prose("")  # a bullet
            return
        if first == ">" and len(probe) == 1:
            self._begin_prose("")  # a quote mark: the space after it goes too
            self._skip_space = True
            return
        # Nothing special: the probe was prose after all.
        self._begin_prose(probe[:-1])
        self._prose(char, out)

    def _start_in_fence(self, char: str) -> None:
        """Inside a fence every line is dropped, except one that closes it: a run of the opening
        character at least as long as the opening run, and nothing else on the line."""
        assert self._fence is not None  # noqa: S101  (the caller checked)
        mark, length = self._fence
        if char == mark and self._run_len == 0:
            self._run_char, self._run_len = mark, 1
            return
        if char == mark and self._run_char == mark:
            self._run_len += 1
            return
        if self._run_len >= length and self._run_char == mark and char in " \t":
            self._run_len = 0
            self._line = _Line.CLOSE  # a closing fence if only blanks follow
            return
        self._run_len = 0
        self._line = _Line.DROP

    def _begin_prose(self, held: str) -> None:
        self._text += held
        self._probe = ""
        self._line = _Line.TEXT

    def _prose(self, char: str, out: list[str]) -> None:
        if self._skip_space:
            self._skip_space = False
            if char == " ":
                return
        text = self._text
        if char.isspace() and text and _ends_a_sentence(text):
            self._text = text + char
            self._cut(out)
            return
        self._text = text + char
        if len(self._text) >= self._max:
            self._cut_long(out)

    # Sentences

    def _cut_long(self, out: list[str]) -> None:
        """The text has no boundary and is at its limit: cut at the last space, else here."""
        space = self._text.rfind(" ")
        if space <= 0:
            self._cut(out, force=True)
            return
        rest = self._text[space + 1 :]
        self._text = self._text[:space]
        self._cut(out, force=True)
        self._text = rest

    def _cut(self, out: list[str], *, final: bool = False, force: bool = False) -> None:
        """Make the text since the last boundary a sentence: spoken now, or (if it is short and
        nothing forces it out) carried to the next."""
        text, self._text = self._text, ""
        text = clean_speech(strip_inline(text))
        if not any(char.isalnum() for char in text):
            if final:
                self._flush_carry(out)
            return
        if self._carry:
            # A heading or a short line is joined to the next with a pause between.
            joint = " " if _TERMINATED.search(self._carry) else ". "
            text = f"{self._carry}{joint}{text}"
        self._carry = ""
        if len(text) < MIN_CHUNK_CHARS and not final and not force:
            self._carry = text
            return
        self._split(text, out)

    def _flush_carry(self, out: list[str]) -> None:
        carry, self._carry = self._carry, ""
        if carry:
            self._split(carry, out)

    def _split(self, text: str, out: list[str]) -> None:
        while len(text) > self._max:
            space = text.rfind(" ", 1, self._max + 1)
            cut = space if space > 0 else self._max
            self._emit(text[:cut].strip(), out)
            text = text[cut:].strip()
        self._emit(text, out)

    @staticmethod
    def _emit(piece: str, out: list[str]) -> None:
        if any(char.isalnum() for char in piece):  # a cut can leave only punctuation
            out.append(piece)
