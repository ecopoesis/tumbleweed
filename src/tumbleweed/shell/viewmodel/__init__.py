"""GTK-free view-model of the shell: the transcript and the controller that feeds it.

Never imports `gi` (import-linter enforces it, ARCHITECTURE section 10), so all of its logic is
unit-tested without a display; the GTK adapter in `tumbleweed.shell` only renders it.
"""
