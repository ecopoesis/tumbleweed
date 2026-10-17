"""The database schema as a list of versioned migrations, and the checks that guard them.

`PRAGMA user_version` is the schema version and `PRAGMA application_id` marks the file as ours.
`migrate` runs inside one `BEGIN IMMEDIATE` transaction: SQLite lets one writer in at a time, so
when several shells start together the first migrates and the others wait (the busy timeout), then
find the schema current and do nothing. A migration is atomic: it applies completely or not at all.
A shell never migrates a file down: a version newer than its own is refused.

The file is untrusted, so a schema is trusted only if it is *exactly* what the migrations produce
(`schema_signature`): every table, index, trigger and view with its original SQL. A file that has
an extra trigger or view, a missing column, a changed constraint, another application's tables or
another `application_id` is refused before anything is read from or written to it. The expected
signature of each version is computed by running the migrations on an in-memory database.

To change the schema, append a `Migration` with the next version; never edit an earlier one.
"""

import functools
import sqlite3
from dataclasses import dataclass
from typing import Final, Literal, final

from tumbleweed.history.errors import HistoryCorruptError, HistoryRefusedError

APPLICATION_ID: Final = 0x54574544  # "TWED"


@final
@dataclass(frozen=True)
class Migration:
    """The statements that take the schema from `version - 1` to `version`."""

    version: int
    statements: tuple[str, ...]


# The millisecond bounds and text limits repeat `tumbleweed.history.records`; the CHECKs are a
# second line for writers other than this code (the file is user-editable), not the first.
_MILLIS = "BETWEEN 0 AND 253402300799999"

MIGRATIONS: Final = (
    Migration(
        1,
        (
            f"""CREATE TABLE sessions (
    id TEXT NOT NULL PRIMARY KEY,
    created_at INTEGER NOT NULL CHECK (created_at {_MILLIS}),
    updated_at INTEGER NOT NULL CHECK (updated_at {_MILLIS})
) STRICT""",
            "CREATE INDEX sessions_by_activity ON sessions (updated_at DESC, id DESC)",
            f"""CREATE TABLE messages (
    id TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    task_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    state TEXT NOT NULL CHECK (state IN ('complete', 'failed')),
    text TEXT NOT NULL CHECK (length(CAST(text AS BLOB)) <= 65536),
    error TEXT CHECK (error IS NULL OR length(error) <= 200),
    created_at INTEGER NOT NULL CHECK (created_at {_MILLIS}),
    UNIQUE (session_id, seq)
) STRICT""",
        ),
    ),
)

type Signature = tuple[tuple[str, str, str, str], ...]


def _integer(
    connection: sqlite3.Connection, pragma: Literal["user_version", "application_id"]
) -> int:
    row: tuple[object, ...] | None = connection.execute(f"PRAGMA {pragma}").fetchone()
    if row is None or type(row[0]) is not int:
        raise HistoryCorruptError("The chat history database could not be read.")
    return row[0]


def schema_signature(connection: sqlite3.Connection) -> Signature:
    """Every user-defined object of the schema as (type, name, table, SQL), sorted."""
    rows: list[tuple[object, ...]] = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' "
        "ESCAPE '\\' ORDER BY type, name"
    ).fetchall()
    found: list[tuple[str, str, str, str]] = []
    for row in rows:
        values = tuple(_text(value) for value in row)
        if len(values) != 4:
            raise HistoryCorruptError("The chat history database could not be read.")
        found.append((values[0], values[1], values[2], values[3]))
    return tuple(found)


def _text(value: object) -> str:
    if value is None:
        return ""  # a NULL `sql` (an automatic index): it cannot match anything we wrote
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise HistoryCorruptError("The chat history database could not be read.") from None
    raise HistoryCorruptError("The chat history database could not be read.")


@functools.cache
def expected_signature(version: int, migrations: tuple[Migration, ...]) -> Signature:
    """The signature of a database at `version`, made by running `migrations` on a scratch one."""
    scratch = sqlite3.connect(":memory:", isolation_level=None)
    try:
        for migration in migrations:
            if migration.version <= version:
                for statement in migration.statements:
                    scratch.execute(statement)
        return schema_signature(scratch)
    finally:
        scratch.close()


def _validate_migrations(migrations: tuple[Migration, ...]) -> None:
    if [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
        raise ValueError("migration versions must be 1, 2, 3, ... in order")


def _refuse_foreign(connection: sqlite3.Connection, migrations: tuple[Migration, ...]) -> int:
    """The file's schema version, once it is known to be ours and exactly what that version is."""
    latest = migrations[-1].version
    version = _integer(connection, "user_version")
    application = _integer(connection, "application_id")
    signature = schema_signature(connection)
    if version == 0:
        if application != 0 or signature:
            raise HistoryRefusedError("The chat history file is not a Tumbleweed database.")
        return 0
    if application != APPLICATION_ID:
        raise HistoryRefusedError("The chat history file is not a Tumbleweed database.")
    if version > latest:
        raise HistoryRefusedError(
            "The chat history was written by a newer version of Tumbleweed and is left alone."
        )
    if signature != expected_signature(version, migrations):
        raise HistoryRefusedError("The chat history database has an unexpected schema.")
    return version


def migrate(connection: sqlite3.Connection, migrations: tuple[Migration, ...] = MIGRATIONS) -> None:
    """Bring the database to the newest version, or refuse it. See the module docstring.

    The connection is in autocommit mode (`isolation_level=None`).
    """
    _validate_migrations(migrations)
    latest = migrations[-1].version
    # No write lock for the common case: a database that is already current.
    if _refuse_foreign(connection, migrations) == latest:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = _refuse_foreign(connection, migrations)  # another shell may have migrated
        for migration in migrations:
            if migration.version > version:
                for statement in migration.statements:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {migration.version}")
        if version == 0:
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
