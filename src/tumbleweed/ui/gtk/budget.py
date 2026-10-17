"""How much text a rendered tool may put on the screen (I9).

Every label is bounded (chunks of at most `MAX_CHUNK` characters), but a spec can bind hundreds of
labels to one 64 KiB string, and laying them all out takes minutes on the main thread, which also
serves the trusted chrome. So the text shown is bounded in total:

- a text block shows at most `MAX_SHOWN_PER_TEXT` characters of its text, then `CUT_MARK`;
- all the text a tool shows (blocks, buttons, check boxes, options) is at most
  `MAX_SHOWN_PER_TOOL` characters. What does not fit is replaced by `NOT_SHOWN` (a block) or left
  out (an option of a list, which ends in a mark saying so).

The state still holds the whole value: only what is displayed is bounded. Nothing is shown out of
order: the claim is made when a widget is built or its text changes, first come first served.
"""

from typing import Final, final

MAX_SHOWN_PER_TEXT: Final = 8_192
MAX_SHOWN_PER_TOOL: Final = 65_536
CUT_MARK: Final = " (cut)"
NOT_SHOWN: Final = "(not shown: too much text on screen)"
MORE_OPTIONS: Final = "(more options not shown)"


@final
class TextBudget:
    """The characters one tool has put on the screen, by owner."""

    def __init__(self) -> None:
        self._claims: dict[object, int] = {}
        self._total = 0

    @property
    def total(self) -> int:
        return self._total

    def claim(self, owner: object, wanted: int) -> bool:
        """Make `owner`'s claim `wanted` characters (replacing its last): False if it won't fit.

        A claim that does not fit is dropped (the owner shows a mark of a few characters).
        """
        old = self._claims.get(owner, 0)
        if self._total - old + wanted > MAX_SHOWN_PER_TOOL:
            self._total -= old
            self._claims[owner] = 0
            return False
        self._total += wanted - old
        self._claims[owner] = wanted
        return True

    def fits(self, owner: object, text: str) -> bool:
        """Claim `text` for `owner`, for good (an option, a button label)."""
        return self.claim(owner, len(text))

    def clear(self) -> None:
        self._claims.clear()
        self._total = 0
