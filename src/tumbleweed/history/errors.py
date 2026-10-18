"""Errors of the chat history.

Every `HistoryError` carries a fixed message that is safe to show in the chat as it is: never text
read from the database file (which is untrusted), an SQLite error message (it can quote a column
or a table name from a hostile schema), or a path. What went wrong in detail is logged by type and
SQLite error name only.
"""

from tumbleweed.core.errors import TumbleweedError


class HistoryError(TumbleweedError):
    """Base of the history's expected failures; `str(error)` is safe to show."""


class HistoryRefusedError(HistoryError):
    """The file or directory is not one this process will use: a symlink, someone else's, not
    a regular file, not our database, or written by a newer version. It is left as it is."""


class HistoryCorruptError(HistoryError):
    """The database is damaged, or holds something that is not valid chat history."""


class HistoryBusyError(HistoryError):
    """Another process held the database for longer than the busy timeout."""


class HistoryIoError(HistoryError):
    """The disk refused: full, read-only, permission denied, or another I/O failure."""
