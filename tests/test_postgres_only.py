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

    def test_seller_helpers_have_no_sqlite_branches(self):
        import inspect

        for func in (
            database.get_all_sellers,
            database.get_active_seller_ids,
            database.toggle_seller_admin,
            database.is_admin_seller,
        ):
            with self.subTest(func=func.__name__):
                source = inspect.getsource(func)
                self.assertNotIn("DATABASE_URL.startswith", source)
                self.assertNotIn("COALESCE(is_admin, 0)", source)
                self.assertNotIn("is_active = 1", source)

    def test_credit_helpers_have_no_sqlite_branches(self):
        import inspect

        for func in (
            database.get_user_balance,
            database.get_user_credit_limit,
            database.set_user_credit_limit,
        ):
            with self.subTest(func=func.__name__):
                source = inspect.getsource(func)
                self.assertNotIn("DATABASE_URL.startswith", source)
                self.assertNotIn("WHERE id=?", source)

    def test_balance_record_helpers_have_no_sqlite_branches(self):
        import inspect

        for func in (
            database.get_balance_records,
            database.update_user_balance,
            database.set_user_balance,
        ):
            with self.subTest(func=func.__name__):
                source = inspect.getsource(func)
                self.assertNotIn("DATABASE_URL.startswith", source)
                self.assertNotIn("sqlite3", source)
                self.assertNotIn("orders.db", source)
                self.assertNotIn("LIMIT ? OFFSET ?", source)

    def test_refund_order_has_no_sqlite_branches(self):
        import inspect

        source = inspect.getsource(database.refund_order)
        self.assertNotIn("DATABASE_URL.startswith", source)
        self.assertNotIn("sqlite3", source)
        self.assertNotIn("orders.db", source)
        self.assertNotIn("SQLite", source)
        self.assertNotIn("WHERE id = ?", source)

    def test_create_order_with_deduction_has_no_sqlite_branches(self):
        import inspect

        source = inspect.getsource(database.create_order_with_deduction_atomic)
        self.assertNotIn("DATABASE_URL.startswith", source)
        self.assertNotIn("sqlite3", source)
        self.assertNotIn("orders.db", source)
        self.assertNotIn("SQLite", source)
        self.assertNotIn("BEGIN TRANSACTION", source)
        self.assertNotIn("VALUES (?, ?, ?, ?, ?, ?, ?)", source)

    def test_recharge_helpers_have_no_sqlite_branches(self):
        import inspect

        for func in (
            database.create_recharge_tables,
            database.create_recharge_request,
            database.get_user_recharge_requests,
            database.get_pending_recharge_requests,
            database.approve_recharge_request,
            database.reject_recharge_request,
        ):
            with self.subTest(func=func.__name__):
                source = inspect.getsource(func)
                self.assertNotIn("DATABASE_URL.startswith", source)
                self.assertNotIn("sqlite3", source)
                self.assertNotIn("orders.db", source)
                self.assertNotIn("SQLite", source)
                self.assertNotIn("sqlite_master", source)
                self.assertNotIn("AUTOINCREMENT", source)
                self.assertNotIn("lastrowid", source)
                self.assertNotIn("BEGIN TRANSACTION", source)

    def test_activation_code_helpers_have_no_sqlite_branches(self):
        import inspect

        for func in (
            database.create_activation_code_table,
            database.generate_activation_code,
            database.create_activation_code,
            database.get_activation_code,
            database.mark_activation_code_used,
            database.get_admin_activation_codes,
        ):
            with self.subTest(func=func.__name__):
                source = inspect.getsource(func)
                self.assertNotIn("DATABASE_URL.startswith", source)
                self.assertNotIn("sqlite3", source)
                self.assertNotIn("orders.db", source)
                self.assertNotIn("SQLite", source)
                self.assertNotIn("sqlite_master", source)
                self.assertNotIn("AUTOINCREMENT", source)
                self.assertNotIn("BEGIN TRANSACTION", source)
                self.assertNotIn("LIMIT ? OFFSET ?", source)

    def test_order_acceptance_helpers_have_no_sqlite_branches(self):
        import inspect

        for func in (
            database.get_unnotified_orders,
            database.accept_order_atomic,
        ):
            with self.subTest(func=func.__name__):
                source = inspect.getsource(func)
                self.assertNotIn("DATABASE_URL.startswith", source)
                self.assertNotIn("sqlite3", source)
                self.assertNotIn("orders.db", source)
                self.assertNotIn("SQLite", source)
                self.assertNotIn("BEGIN EXCLUSIVE", source)
                self.assertNotIn("WHERE id = ?", source)

    def test_custom_price_helpers_have_no_sqlite_branches(self):
        import inspect

        for func in (
            database.get_user_custom_prices,
            database.set_user_custom_price,
            database.delete_user_custom_price,
        ):
            with self.subTest(func=func.__name__):
                source = inspect.getsource(func)
                self.assertNotIn("DATABASE_URL.startswith", source)
                self.assertNotIn("WHERE user_id = ?", source)
                self.assertNotIn("VALUES (?, ?, ?, ?, ?)", source)
                self.assertNotIn("SET price = ?", source)

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
