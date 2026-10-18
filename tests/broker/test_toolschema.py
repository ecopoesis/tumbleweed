"""The tool store's tables in the broker's database: migration, an exact schema, hostile rows."""

# ruff: noqa: E501  (rows written out in full)

import sqlite3
from pathlib import Path

import pytest

from tests.broker.test_store import make_database, ours, untouched
from tests.broker.test_toolstore import Rig
from tests.tools.support import draft
from tumbleweed.broker.errors import StoreError, StoreRefusedError
from tumbleweed.broker.store import APPLICATION_ID, DATABASE_NAME, MIGRATIONS, BrokerStore
from tumbleweed.broker.task import TaintKind
from tumbleweed.core.ids import TaskId


def raw(directory: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(directory / DATABASE_NAME, isolation_level=None)
    connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, False)
    return connection


class TestMigration:
    def test_a_database_made_before_the_tool_store_gains_its_tables_and_keeps_its_data(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "broker"
        make_database(directory, list(MIGRATIONS[0]), version=1, application=APPLICATION_ID)
        task = TaskId.new()
        connection = raw(directory)
        connection.execute(
            "INSERT INTO tasks (id, state, taint, created_at, workspace, workspace_fsid, "
            "workspace_ino) VALUES (?, 'live', 'fs', 1000, 'present', '1', '2')",
            (str(task),),
        )
        connection.close()

        store = BrokerStore.open(directory)
        try:
            kept = store.task(task)
            assert kept is not None
            assert kept.taint == {TaintKind.FS}
            assert store.tool_totals() == (0, 0)
        finally:
            store.close()

        connection = raw(directory)
        assert connection.execute("PRAGMA user_version").fetchone() == (len(MIGRATIONS),)
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        connection.close()
        assert {"tools", "tool_versions", "tool_search"} <= names

    def test_the_migration_is_one_step_the_store_can_be_reopened_and_used(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path / "broker")
        saved = rig.save()
        rig.close()

        again = Rig(tmp_path / "broker")
        try:
            assert again.tools.open(saved.record.tool).record == saved.record
            assert [r.tool for r in again.tools.find("csv")] == [saved.record.tool]
        finally:
            again.close()


class TestAHostileFile:
    """Refused with the bytes exactly as they were (as `test_store.TestAHostileFile`)."""

    def refuse(self, directory: Path) -> None:
        before = untouched(directory)

        with pytest.raises(StoreRefusedError):
            BrokerStore.open(directory)

        assert untouched(directory) == before

    def test_the_search_table_missing(self, tmp_path: Path) -> None:
        ours(tmp_path)
        connection = raw(tmp_path)
        connection.execute("DROP TABLE tool_search")
        connection.close()

        self.refuse(tmp_path)

    def test_the_search_table_with_another_tokenizer_or_columns(self, tmp_path: Path) -> None:
        ours(tmp_path)
        connection = raw(tmp_path)
        connection.execute("DROP TABLE tool_search")
        connection.execute("CREATE VIRTUAL TABLE tool_search USING fts5(name, description, tags)")
        connection.close()

        self.refuse(tmp_path)

    def test_an_extra_shadow_table_of_the_search_index(self, tmp_path: Path) -> None:
        ours(tmp_path)
        connection = raw(tmp_path)
        connection.execute("CREATE TABLE tool_search_extra (x TEXT)")
        connection.close()

        self.refuse(tmp_path)

    def test_an_extra_index_on_the_versions(self, tmp_path: Path) -> None:
        ours(tmp_path)
        connection = raw(tmp_path)
        connection.execute("CREATE INDEX sneaky ON tool_versions (name)")
        connection.close()

        self.refuse(tmp_path)

    def test_a_trigger_on_the_versions(self, tmp_path: Path) -> None:
        ours(tmp_path)
        connection = raw(tmp_path)
        connection.execute(
            "CREATE TRIGGER evil AFTER INSERT ON tool_versions BEGIN DELETE FROM tools; END"
        )
        connection.close()

        self.refuse(tmp_path)

    def test_a_constraint_of_the_versions_relaxed(self, tmp_path: Path) -> None:
        ours(tmp_path)
        connection = raw(tmp_path)
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = replace(sql, 'UNIQUE CHECK (length(hash) = 64', "
            "'CHECK (length(hash) = 64') WHERE name = 'tool_versions'"
        )
        connection.close()

        self.refuse(tmp_path)


class TestTheTablesRefuseWhatCannotBeReal:
    @pytest.mark.parametrize(
        "statement",
        [
            "INSERT INTO tools (id, latest) VALUES ('nope', 1)",
            "INSERT INTO tools (id, latest) VALUES ('tool_01m56q3ayn1sqq4f1newcs9742', 0)",
            "INSERT INTO tool_versions VALUES (1, 0, '{h}', 5, 'n', '', '', 'none', 'none', 'saved', 1)",
            "INSERT INTO tool_versions VALUES (1, 1, 'XYZ', 5, 'n', '', '', 'none', 'none', 'saved', 1)",
            "INSERT INTO tool_versions VALUES (1, 1, '{h}', 0, 'n', '', '', 'none', 'none', 'saved', 1)",
            "INSERT INTO tool_versions VALUES (1, 1, '{h}', 500000, 'n', '', '', 'none', 'none', 'saved', 1)",
            "INSERT INTO tool_versions VALUES (1, 1, '{h}', 5, 'n', '', '', 'none', 'both', 'saved', 1)",
            "INSERT INTO tool_versions VALUES (1, 1, '{h}', 5, 'n', '', '', 'none', 'none', 'forged', 1)",
            "INSERT INTO tool_versions VALUES (1, 1, '{h}', 5, 'n', '', '', 'weird', 'none', 'saved', 1)",
            "INSERT INTO tool_versions VALUES (1, 1, '{h}', 5, 'n', '', '"
            + "t" * 600
            + "', 'none', 'none', 'saved', 1)",
            "INSERT INTO tool_versions VALUES (1, 1, '{h}', 5, 'n', '', '', 'none', 'none', 'saved', -1)",
            "INSERT INTO tool_versions VALUES (1, 1, '{h}', 5, 'n', '', '', 'none', 'none', 'saved', 'x')",
            "INSERT INTO tool_versions VALUES (1, 1, '{h}', 5, 'n', '', '', 'none', 'none', 'saved', 1.5)",
        ],
    )
    def test_a_row_that_cannot_be_real_is_refused_by_the_database(
        self, tmp_path: Path, statement: str
    ) -> None:
        rig = Rig(tmp_path / "broker")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                rig.sql(statement.format(h="a" * 64))
        finally:
            rig.close()

    def test_a_search_index_that_was_corrupted_is_an_error_not_a_crash(
        self, tmp_path: Path
    ) -> None:
        rig = Rig(tmp_path / "broker")
        try:
            rig.save(draft())
            connection = raw(tmp_path / "broker")
            connection.execute("UPDATE tool_search_data SET block = x'00ff00ff00ff'")
            connection.close()

            with pytest.raises(StoreError):
                rig.tools.find("csv")
            assert len(rig.tools.find(None)) == 1  # listing does not use the index
        finally:
            rig.close()
