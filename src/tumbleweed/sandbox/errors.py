"""Domain exceptions of the sandbox launcher.

Messages are built from our own text, names and sizes. What the sandboxed program wrote is never
repeated in one beyond a short `excerpt`: these messages end up in logs and in the UI.
"""

from tumbleweed.core.errors import InvalidValueError, TumbleweedError


class SandboxError(TumbleweedError):
    """Base class of every error the sandbox launcher raises on purpose."""


class InvalidPolicyError(SandboxError, InvalidValueError):
    """A `SandboxPolicy` (or one of its parts) is not one the launcher will run."""


class SandboxUnsupported(SandboxError):  # noqa: N818  (a statement about the platform, not a failure)
    """Sandboxes cannot run on this platform: they need Linux namespaces (macOS has none)."""


class SandboxUnavailable(SandboxError):  # noqa: N818  (a statement about the host, not a failure)
    """A program the launcher needs is missing, so nothing is run (it never runs unconfined)."""


class SandboxLaunchError(SandboxError):
    """The sandbox could not be set up, so the program never started.

    Examples: a mount source that vanished, a program that does not exist inside the sandbox,
    user namespaces that the host refuses.
    """


class SandboxTeardownError(SandboxError):
    """The sandbox was killed but its launcher could not be reaped in time."""
