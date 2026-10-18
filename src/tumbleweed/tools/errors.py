"""What goes wrong with a tool package or a bundle, in a form safe to show and to log."""

from typing import final

from tumbleweed.ui.spec.errors import SpecError


class ToolPackageError(SpecError):
    """A package, a draft or a bundle was refused. `problems` says why, boundedly: at most ten
    lines, each cut and with every non-printable character written as `\\uXXXX`, and none repeats
    the input beyond a short excerpt. (It is a `SpecError` for that bounding, and because most of
    what a package holds is a spec; nothing that handles a spec should catch it by accident, so
    the S12 entry points keep catching `SpecError` from their own calls only.)"""


@final
class ToolTooLargeError(ToolPackageError):
    """A package or bundle is over its size limit, so it was not parsed (or not stored)."""
