"""Who is asking: one IPC connection, as far as the broker's service and prompts are concerned."""

from typing import NewType

Owner = NewType("Owner", int)
"""One IPC connection: the daemon makes one per connection. A task belongs to the `Owner` that made
it, and a prompt is answered only by the `Owner` it was sent to (I5)."""
