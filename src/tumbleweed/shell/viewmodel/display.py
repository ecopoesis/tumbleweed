"""The display-only transform of transcript text: keeps text a label can lay out in bounded time.

Transcript text is stored canonical (`Transcript` never changes what it was given, apart from NUL
and lone surrogates). What the GTK adapter puts into a label goes through `display_text` first,
because two kinds of text, small enough to pass every cap, can still wreck a layout:

- **One huge grapheme cluster.** Characters that extend a cluster (combining marks, spacing marks,
  ZWJ, Hangul jamo, ...) glue to the one before them, so 5000 jamo or a long ZWJ chain are a single
  unbreakable glyph run tens of thousands of pixels wide. Pango will not break inside a cluster, so
  no wrap mode helps. After `MAX_CLUSTER` code points in one cluster the next extender gets a
  U+200B ZERO WIDTH SPACE in front: its grapheme-break class is Control, so it ends the cluster.
- **Text with no break opportunity.** Measuring a wrapped label is quadratic in the length of an
  unbroken run (16 KiB of hex took seconds, 64 KiB minutes, on the main thread). A U+200B is also a
  line-break opportunity, and one is inserted whenever `MAX_RUN` code points have gone by without
  whitespace or one.

Ordinary text is untouched: no legitimate cluster comes near `MAX_CLUSTER` (the longest emoji ZWJ
sequences have 10 code points, a Devanagari conjunct a handful), and ordinary prose has a space
within 256 characters. Long runs of CJK or of a URL do get zero-width spaces, which is harmless.
The result is for display only: copying from a label copies the inserted zero-width spaces too.
"""

import unicodedata
from typing import Final, final

ZERO_WIDTH_SPACE: Final = "\u200b"
MAX_CLUSTER: Final = 16
MAX_RUN: Final = 256

_ZWJ = "\u200d"
_ZWNJ = "\u200c"
# Grapheme_Cluster_Break=Prepend: a prepend glues to whatever follows it, itself included.
_PREPEND = (
    (0x0600, 0x0605),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x0D4E, 0x0D4E),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x111C2, 0x111C3),
    (0x1193F, 0x1193F),
    (0x11941, 0x11941),
    (0x11A3A, 0x11A3A),
    (0x11A84, 0x11A89),
    (0x11D46, 0x11D46),
    (0x11F02, 0x11F02),
)
_EMOJI_MODIFIERS = (0x1F3FB, 0x1F3FF)
_TAGS = (0xE0020, 0xE007F)


def _in(code: int, *ranges: tuple[int, int]) -> bool:
    return any(low <= code <= high for low, high in ranges)


def _hangul(char: str) -> str:
    """The Hangul syllable-break class of `char` (UAX 29): L, V, T, LV, LVT, or "" for any other."""
    code = ord(char)
    if 0x1100 <= code <= 0x115F or 0xA960 <= code <= 0xA97C:
        return "L"
    if 0x1160 <= code <= 0x11A7 or 0xD7B0 <= code <= 0xD7C6:
        return "V"
    if 0x11A8 <= code <= 0x11FF or 0xD7CB <= code <= 0xD7FB:
        return "T"
    if 0xAC00 <= code <= 0xD7A3:
        return "LV" if (code - 0xAC00) % 28 == 0 else "LVT"
    return ""


def _hangul_extends(previous: str, char: str) -> bool:
    """UAX 29 rules GB6 to GB8: which jamo and syllables glue to the one before them."""
    before, after = _hangul(previous), _hangul(char)
    if before == "L":
        return after in {"L", "V", "LV", "LVT"}
    if before in {"LV", "V"}:
        return after in {"V", "T"}
    if before in {"LVT", "T"}:
        return after == "T"
    return False


def _extends(char: str, previous: str) -> bool:
    """Whether `char` glues to `previous` into one grapheme cluster (an approximation of UAX 29)."""
    code = ord(char)
    return (
        unicodedata.category(char) in {"Mn", "Me", "Mc"}  # extend and spacing marks
        or char in {_ZWJ, _ZWNJ}
        or _in(code, _EMOJI_MODIFIERS, _TAGS)
        or previous == _ZWJ  # an emoji after a ZWJ continues the sequence
        or _hangul_extends(previous, char)
        or _in(ord(previous), *_PREPEND)
    )


def _breaks_line(char: str) -> bool:
    return char.isspace() or char == ZERO_WIDTH_SPACE


@final
class DisplayText:
    """`display_text`, fed in pieces: a streaming reply is transformed delta by delta, in time
    proportional to what arrived, not to what is already there.

    `feed` returns the display form of each piece. Feeding a text in any number of pieces gives
    the same result as feeding it whole.
    """

    def __init__(self) -> None:
        self._cluster = 0  # code points in the grapheme cluster being read
        self._run = 0  # code points since the last whitespace or zero-width space
        self._previous = ""

    def feed(self, text: str) -> str:
        pieces: list[str] = []
        changed = False
        for char in text:
            extends = bool(self._previous) and _extends(char, self._previous)
            if (extends and self._cluster >= MAX_CLUSTER) or (
                self._run >= MAX_RUN and not _breaks_line(char)
            ):
                pieces.append(ZERO_WIDTH_SPACE)
                changed = True
                self._cluster = 0
                self._run = 0
                extends = False  # after the break, `char` starts a cluster of its own
            pieces.append(char)
            self._cluster = self._cluster + 1 if extends else 1
            self._run = 0 if _breaks_line(char) else self._run + 1
            self._previous = char
        return "".join(pieces) if changed else text


def display_text(text: str) -> str:
    """`text` with zero-width spaces added where a label could not lay it out (see the module).

    Linear in the length of `text`, and returns `text` itself when nothing needs adding.
    """
    return DisplayText().feed(text)


MAX_CHUNK: Final = 4096


@final
class DisplayChunks:
    """The display text of one message as chunks of at most about `MAX_CHUNK` characters.

    A label takes time superlinear in its length to measure (64 KiB of text took 3.5 s, 4 KiB a few
    hundredths of a second), and a streaming reply is measured again after every delta. Showing a
    message as a column of labels, one per chunk, keeps each layout small, and a delta touches only
    the last one. Joined, the chunks are exactly the display text. A chunk ends at a newline, else
    a space or zero-width space, else (never, given `display_text`'s breaks) at the limit.
    """

    def __init__(self) -> None:
        self._display = DisplayText()
        self._chunks = [""]

    @property
    def chunks(self) -> tuple[str, ...]:
        return tuple(self._chunks)

    def feed(self, text: str) -> int:
        """Add `text` (as it arrived, not yet transformed); return the index of the first chunk
        that is new or changed, so that a caller redraws only from there."""
        first = len(self._chunks) - 1
        self._chunks[-1] += self._display.feed(text)
        while len(self._chunks[-1]) > MAX_CHUNK:
            last = self._chunks[-1]
            cut = last.rfind("\n", MAX_CHUNK // 2, MAX_CHUNK)
            if cut < 0:
                cut = max(
                    last.rfind(" ", MAX_CHUNK // 2, MAX_CHUNK),
                    last.rfind(ZERO_WIDTH_SPACE, MAX_CHUNK // 2, MAX_CHUNK),
                )
            cut = cut + 1 if cut >= 0 else MAX_CHUNK
            self._chunks[-1] = last[:cut]
            self._chunks.append(last[cut:])
        return first
