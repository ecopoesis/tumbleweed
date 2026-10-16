"""The display-only transform of transcript text: keeps text a label can lay out in bounded time.

Transcript text is stored canonical (`Transcript` never changes what it was given, apart from NUL
and lone surrogates). What the GTK adapter puts into a label goes through this module first,
because two kinds of text, small enough to pass every cap, can still wreck a layout:

- **One huge grapheme cluster.** Characters that extend a cluster (combining marks, spacing marks,
  ZWJ, Hangul jamo, a chain of Indic consonants joined by viramas, ...) glue to the one before
  them, so 5000 jamo or a long ZWJ chain are a single unbreakable glyph run tens of thousands of
  pixels wide. Pango will not break inside a cluster, so no wrap mode helps. After `MAX_CLUSTER`
  code points in one cluster the next one gets a U+200B ZERO WIDTH SPACE in front: its
  grapheme-break class is Control, so it ends the cluster. Clusters are found with a real UAX 29
  segmenter (`regex`'s `\\X`, see DECISIONS D10), not with tables of ours.
- **Text with no break opportunity.** Measuring a wrapped label is quadratic in the length of an
  unbroken run (16 KiB of hex took seconds, 64 KiB minutes, on the main thread). A U+200B is also a
  line-break opportunity, and one is inserted whenever `MAX_RUN` code points have gone by without
  one. "A break opportunity" here means a real one: whitespace that UAX 14 breaks at, and U+200B.
  The no-break spaces (U+00A0, U+2007, U+202F), U+2060, U+FEFF and the control characters are
  *not* breaks: text joined by them has no break opportunity, and they count towards the run.

A message is also split into chunks of at most `MAX_CHUNK` characters (`DisplayChunks`), shown as
one label each: measuring a label takes time superlinear in its length (64 KiB took 3.5 s, 4 KiB a
few hundredths of a second), and a streaming reply is measured again after every delta.

Ordinary text is untouched: no legitimate cluster comes near `MAX_CLUSTER` (the longest emoji ZWJ
sequences have 10 code points, a Devanagari conjunct a handful), and ordinary prose has a space
within 256 characters. Long runs of CJK or of a URL do get zero-width spaces, which is harmless.
The inserted characters are display only: `DisplayChunks.source_text` gives a selection back
without them, which is what copying from a label puts on the clipboard.
"""

from collections.abc import Iterator
from typing import Final, final

import regex

ZERO_WIDTH_SPACE: Final = chr(0x200B)
MAX_CLUSTER: Final = 16
MAX_RUN: Final = 256
MAX_CHUNK: Final = 4096

_CLUSTER: Final = regex.compile(r"\X")

# Where UAX 14 allows a break and a label wraps: the spaces (not the no-break ones), the line and
# paragraph separators, tab, and the zero-width space. Everything else, controls included, is part
# of a run.
_BREAKS: Final = frozenset(
    chr(code)
    for code in (
        0x09,  # tab
        0x0A,  # line feed
        0x0B,  # line tabulation
        0x0C,  # form feed
        0x0D,  # carriage return
        0x20,  # space
        0x85,  # next line
        0x1680,  # ogham space mark
        *range(0x2000, 0x2007),  # en quad .. six-per-em space (U+2007, figure space, is no-break)
        *range(0x2008, 0x200B),  # punctuation space .. hair space
        0x200B,  # zero width space
        0x2028,  # line separator
        0x2029,  # paragraph separator
        0x205F,  # medium mathematical space
        0x3000,  # ideographic space
    )
)
# What ends a line: a chunk that is cut after one of these does not show it (the label boundary is
# the line break), or it would add an empty line.
_LINE_ENDS: Final = frozenset(chr(code) for code in (0x0A, 0x0B, 0x0C, 0x0D, 0x85, 0x2028, 0x2029))


def _breaks_line(char: str) -> bool:
    return char in _BREAKS


@final
class DisplayText:
    """`display_text`, fed in pieces: a streaming reply is transformed delta by delta, in time
    proportional to what arrived, not to what is already there.

    `feed` returns the display form of each piece. Feeding a text in any number of pieces gives
    the same result as feeding it whole.
    """

    def __init__(self) -> None:
        # The grapheme cluster being read (as displayed, so at most `MAX_CLUSTER` code points):
        # the next piece may continue it, and the segmenter needs to see it to know.
        self._tail = ""
        self._run = 0  # code points since the last break opportunity

    def feed(self, text: str) -> str:
        return self.feed_marked(text)[0]

    def feed_marked(self, text: str) -> tuple[str, tuple[int, ...]]:
        """`feed`, and the offsets in the result of the zero-width spaces that were added."""
        if not text:
            return text, ()
        context = self._tail
        data = context + text
        starts = {match.start() for match in _CLUSTER.finditer(data)}
        pieces: list[str] = []
        added: list[int] = []
        position = 0  # length of the result so far
        cluster = len(context)  # code points in the cluster being read
        cluster_start = 0  # where it starts in `data`
        run = self._run
        for offset in range(len(context), len(data)):
            char = data[offset]
            extends = offset not in starts  # glued to the code point before it
            if (extends and cluster >= MAX_CLUSTER) or (run >= MAX_RUN and not _breaks_line(char)):
                pieces.append(ZERO_WIDTH_SPACE)
                added.append(position)
                position += 1
                cluster = 0
                run = 0
                extends = False  # after the break, `char` starts a cluster of its own
            pieces.append(char)
            position += 1
            if extends:
                cluster += 1
            else:
                cluster = 1
                cluster_start = offset
            run = 0 if _breaks_line(char) else run + 1
        self._tail = data[cluster_start:]
        self._run = run
        return ("".join(pieces) if added else text), tuple(added)


def display_text(text: str) -> str:
    """`text` with zero-width spaces added where a label could not lay it out (see the module).

    Linear in the length of `text`, and returns `text` itself when nothing needs adding.
    """
    return DisplayText().feed(text)


def _boundary_after(text: str, index: int) -> bool:
    """Whether `text` may be cut after `text[index]`: that code point is a whole cluster."""
    match = _CLUSTER.match(text, index)
    return match is not None and match.end() == index + 1


def _cluster_ends(text: str, low: int, high: int) -> Iterator[int]:
    """The ends of the clusters of `text` that lie in [low, high], from the last."""
    ends = [m.end() for m in _CLUSTER.finditer(text, 0, high + MAX_CLUSTER) if m.end() <= high]
    yield from (end for end in reversed(ends) if end >= low)


def _cut(text: str) -> int:
    """Where to cut `text`, which is over `MAX_CHUNK`, into a head of at most `MAX_CHUNK`.

    After the last line end in the second half of the allowed range if there is one, else after the
    last other break, and in neither case inside a grapheme cluster. If the range has no break
    (`display_text` makes that impossible: it puts one in every 256 code points), at the last
    cluster boundary in it.
    """
    low = MAX_CHUNK // 2
    breaks = [
        index
        for index in range(MAX_CHUNK - 1, low - 1, -1)
        if text[index] in _BREAKS and _boundary_after(text, index)
    ]
    for index in breaks:
        if text[index] in _LINE_ENDS:
            return index + 1
    if breaks:
        return breaks[0] + 1
    return next(_cluster_ends(text, low, MAX_CHUNK), MAX_CHUNK)


def _without_line_end(piece: str) -> str:
    """`piece` without the one line end it may finish with."""
    if piece.endswith("\r\n"):
        return piece[:-2]
    return piece[:-1] if piece and piece[-1] in _LINE_ENDS else piece


@final
class DisplayChunks:
    """The display text of one message as chunks of at most `MAX_CHUNK` characters.

    Showing a message as a column of labels, one per chunk, keeps each layout small, and a delta
    touches only the last one. A chunk ends at a line end, else a space or zero-width space, never
    inside a grapheme cluster. A chunk that ends at a line end does not show it: the boundary
    between two labels is already a line break.
    """

    def __init__(self) -> None:
        self._display = DisplayText()
        self._pieces = [""]  # exactly the display text, cut up
        self._added: list[list[int]] = [[]]  # per piece, where `DisplayText` added a break

    @property
    def text(self) -> str:
        """The whole display text."""
        return "".join(self._pieces)

    @property
    def chunks(self) -> tuple[str, ...]:
        """What each label shows."""
        return (*(_without_line_end(piece) for piece in self._pieces[:-1]), self._pieces[-1])

    def source_text(self, index: int, start: int, end: int) -> str:
        """The text of chunk `index` from `start` to `end`, without the breaks that were added.

        A zero-width space the message itself had is kept: only the inserted ones go.
        """
        added = set(self._added[index])
        piece = self._pieces[index]
        return "".join(piece[i] for i in range(start, min(end, len(piece))) if i not in added)

    def feed(self, text: str) -> int:
        """Add `text` (as it arrived, not yet transformed); return the index of the first chunk
        that is new or changed, so that a caller redraws only from there."""
        first = len(self._pieces) - 1
        shown, added = self._display.feed_marked(text)
        base = len(self._pieces[-1])
        self._pieces[-1] += shown
        self._added[-1].extend(base + offset for offset in added)
        while len(self._pieces[-1]) > MAX_CHUNK:
            last = self._pieces[-1]
            marks = self._added[-1]
            cut = _cut(last)
            self._pieces[-1] = last[:cut]
            self._added[-1] = [m for m in marks if m < cut]
            self._pieces.append(last[cut:])
            self._added.append([m - cut for m in marks if m >= cut])
        return first
