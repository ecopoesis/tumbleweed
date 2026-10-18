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

from collections.abc import Callable
from typing import Final, final

MAX_SHOWN_PER_TEXT: Final = 8_192
MAX_SHOWN_PER_TOOL: Final = 65_536
CUT_MARK: Final = " (cut)"
NOT_SHOWN: Final = "(not shown: too much text on screen)"
MORE_OPTIONS: Final = "(more options not shown)"


@final
class TextBudget:
    """The characters one tool has put on the screen, by owner.

    An owner whose claim is refused can ask to be told when room appears (`wait_for_room`): it
    shows a mark meanwhile, and shows its text once a claim shrinks or an owner lets go. The
    tool schedules the retry (`on_room`) for its next idle turn, so refused owners are served in
    the order they were refused.
    """

    def __init__(self) -> None:
        self._claims: dict[object, int] = {}
        self._total = 0
        self._waiting: dict[object, Callable[[], None]] = {}
        self.on_room: Callable[[], None] | None = None

    @property
    def total(self) -> int:
        return self._total

    def claim(self, owner: object, wanted: int, *, keep: bool = False) -> bool:
        """Make `owner`'s claim `wanted` characters (replacing its last): False if it won't fit.

        A claim that does not fit is dropped (the owner shows a mark of a few characters), which
        gives back what it held. With `keep` it is made whatever the total is: that is for the
        entry the user is typing in, which must not turn into a mark under their fingers. The
        total can then pass the budget by at most one entry's cap (the user types in one), and
        the next claim that has to fit sees it.
        """
        old = self._claims.get(owner, 0)
        if not keep and self._total - old + wanted > MAX_SHOWN_PER_TOOL:
            self._total -= old
            self._claims[owner] = 0
            if old:
                self._room()
            return False
        self._total += wanted - old
        self._claims[owner] = wanted
        if wanted < old:
            self._room()
        return True

    def fits(self, owner: object, text: str) -> bool:
        """Claim `text` for `owner`, for good (an option, a button label)."""
        return self.claim(owner, len(text))

    def wait_for_room(self, owner: object, retry: Callable[[], None]) -> None:
        """Call `retry` (once) when room has appeared and `retry_waiting` is run."""
        self._waiting[owner] = retry

    def stop_waiting(self, owner: object) -> None:
        self._waiting.pop(owner, None)

    def retry_waiting(self) -> None:
        """Let the owners that were refused try again, first refused first."""
        waiting, self._waiting = self._waiting, {}
        for retry in waiting.values():
            retry()

    def _room(self) -> None:
        if self._waiting and self.on_room is not None:
            self.on_room()

    def clear(self) -> None:
        self._claims.clear()
        self._waiting.clear()
        self._total = 0
