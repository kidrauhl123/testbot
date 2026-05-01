import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import database


class PostgresOnlyDatabaseTests(unittest.TestCase):
    def test_rejects_missing_or_sqlite_database_url(self):
        with mock.patch.object(database, "DATABASE_URL", ""):
            with self.assertRaises(RuntimeError):
                database.ensure_postgres_configured()

        with mock.patch.object(database, "DATABASE_URL", "sqlite:///orders.db"):
            with self.assertRaises(RuntimeError):
                database.ensure_postgres_configured()

    def test_accepts_postgres_database_url(self):
        for url in (
            "postgres://user:pass@localhost:5432/testbot",
            "postgresql://user:pass@localhost:5432/testbot",
        ):
            with self.subTest(url=url):
                with mock.patch.object(database, "DATABASE_URL", url):
                    database.ensure_postgres_configured()

    def test_sqlite_entrypoints_are_not_exposed(self):
        self.assertFalse(hasattr(database, "init_sqlite_db"))
        self.assertFalse(hasattr(database, "execute_sqlite_query"))

    def test_add_balance_record_has_no_sqlite_fallback(self):
        import inspect

        source = inspect.getsource(database.add_balance_record)
        self.assertNotIn("sqlite3", source)
        self.assertNotIn("orders.db", source)

    def test_get_order_details_has_no_sqlite_placeholder_branch(self):
        import inspect

        source = inspect.getsource(database.get_order_details)
        self.assertNotIn("DATABASE_URL.startswith", source)
        self.assertNotIn(" else '?'", source)

    def test_execute_query_converts_sqlite_placeholders_for_postgres(self):
        executed = {}

        class FakeCursor:
            def execute(self, query, params=None):
                executed["query"] = query
                executed["params"] = params

            def fetchall(self):
                return [(1,)]

        class FakeConnection:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                executed["committed"] = True

            def close(self):
                executed["closed"] = True

        with mock.patch.object(database, "DATABASE_URL", "postgresql://user:pass@localhost/testbot"), \
             mock.patch.object(database, "get_postgres_connection", return_value=FakeConnection()):
            result = database.execute_query("SELECT * FROM users WHERE id = ?", (123,), fetch=True)

        self.assertEqual(result, [(1,)])
        self.assertEqual(executed["query"], "SELECT * FROM users WHERE id = %s")
        self.assertEqual(executed["params"], (123,))
        self.assertTrue(executed["committed"])
        self.assertTrue(executed["closed"])


if __name__ == "__main__":
    unittest.main()
