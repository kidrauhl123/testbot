import inspect
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules import database
from modules import db_core


class WebRoutesPostgresOnlyTests(unittest.TestCase):
    def test_web_routes_have_no_sqlite_branches(self):
        source = (PROJECT_ROOT / "modules" / "web_routes.py").read_text()
        forbidden_markers = (
            "import sqlite3",
            "sqlite3.connect",
            "orders.db",
            "DATABASE_URL.startswith('postgres')",
            "DATABASE_URL.startswith(\"postgres\")",
            "BEGIN TRANSACTION",
            "cursor.lastrowid",
            "LIMIT ? OFFSET ?",
            "WHERE id = ?",
            "WHERE id=?",
            "SET account=?, password=?, package=?, status=?, remark=?",
        )
        for marker in forbidden_markers:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source)


class TelegramBotPostgresOnlyTests(unittest.TestCase):
    def test_telegram_bot_has_no_sqlite_branches(self):
        source = (PROJECT_ROOT / "modules" / "telegram_bot.py").read_text()
        forbidden_markers = (
            "import sqlite3",
            "sqlite3.connect",
            "orders.db",
            "DATABASE_URL.startswith('postgres')",
            "DATABASE_URL.startswith(\"postgres\")",
            "placeholder = '%s' if DATABASE_URL.startswith('postgres') else '?'",
            "SELECT * FROM orders WHERE id = ?",
            "UPDATE orders SET notified = 1 WHERE id = ?",
            "UPDATE orders SET status = ?, handler_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            "UPDATE orders SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        )
        for marker in forbidden_markers:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source)


class PostgresOnlyDatabaseTests(unittest.TestCase):
    def test_rejects_missing_or_sqlite_database_url(self):
        with mock.patch.object(db_core, "DATABASE_URL", ""):
            with self.assertRaises(RuntimeError):
                database.ensure_postgres_configured()

        with mock.patch.object(db_core, "DATABASE_URL", "sqlite:///orders.db"):
            with self.assertRaises(RuntimeError):
                database.ensure_postgres_configured()

    def test_accepts_postgres_database_url(self):
        for url in (
            "postgres://user:pass@localhost:5432/testbot",
            "postgresql://user:pass@localhost:5432/testbot",
        ):
            with self.subTest(url=url):
                with mock.patch.object(db_core, "DATABASE_URL", url):
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

    def test_database_reexports_core_database_helpers(self):
        from modules import db_core

        self.assertIs(database.ensure_postgres_configured, db_core.ensure_postgres_configured)
        self.assertIs(database.get_postgres_connection, db_core.get_postgres_connection)
        self.assertIs(database.execute_postgres_query, db_core.execute_postgres_query)
        self.assertIs(database.execute_query, db_core.execute_query)

    def test_database_reexports_order_balance_helpers(self):
        from modules import order_balance

        helper_names = (
            "get_china_time",
            "add_balance_record",
            "get_unnotified_orders",
            "accept_order_atomic",
            "get_order_details",
            "get_user_balance",
            "get_user_credit_limit",
            "set_user_credit_limit",
            "get_balance_records",
            "update_user_balance",
            "set_user_balance",
            "check_balance_for_package",
            "refund_order",
            "create_order_with_deduction_atomic",
        )
        for name in helper_names:
            with self.subTest(name=name):
                self.assertIs(getattr(database, name), getattr(order_balance, name))

    def test_database_reexports_recharge_helpers(self):
        from modules import recharge

        helper_names = (
            "create_recharge_tables",
            "create_recharge_request",
            "get_user_recharge_requests",
            "get_pending_recharge_requests",
            "approve_recharge_request",
            "reject_recharge_request",
        )
        for name in helper_names:
            with self.subTest(name=name):
                self.assertIs(getattr(database, name), getattr(recharge, name))

    def test_database_reexports_activation_code_helpers(self):
        from modules import activation_codes

        helper_names = (
            "create_activation_code_table",
            "generate_activation_code",
            "create_activation_code",
            "get_activation_code",
            "mark_activation_code_used",
            "get_admin_activation_codes",
        )
        for name in helper_names:
            with self.subTest(name=name):
                self.assertIs(getattr(database, name), getattr(activation_codes, name))

    def test_database_reexports_custom_price_helpers(self):
        from modules import custom_prices

        helper_names = (
            "get_user_custom_prices",
            "set_user_custom_price",
            "delete_user_custom_price",
        )
        for name in helper_names:
            with self.subTest(name=name):
                self.assertIs(getattr(database, name), getattr(custom_prices, name))

    def test_database_reexports_seller_helpers(self):
        from modules import sellers

        helper_names = (
            "hash_password",
            "get_all_sellers",
            "get_active_seller_ids",
            "add_seller",
            "toggle_seller_status",
            "remove_seller",
            "toggle_seller_admin",
            "is_admin_seller",
        )
        for name in helper_names:
            with self.subTest(name=name):
                self.assertIs(getattr(database, name), getattr(sellers, name))

    def test_database_reexports_schema_helpers(self):
        from modules import db_schema

        helper_names = (
            "init_db",
            "init_postgres_db",
            "create_performance_indexes",
        )
        for name in helper_names:
            with self.subTest(name=name):
                self.assertIs(getattr(database, name), getattr(db_schema, name))

    def test_schema_helpers_have_no_sqlite_branches(self):
        from modules import db_schema

        for name in ("init_db", "init_postgres_db", "create_performance_indexes"):
            source = inspect.getsource(getattr(db_schema, name))
            with self.subTest(name=name):
                self.assertNotIn("sqlite3", source)
                self.assertNotIn("orders.db", source)
                self.assertNotIn("SQLite", source)
                self.assertNotIn("DATABASE_URL.startswith", source)

    def test_execute_query_requires_postgres_placeholders(self):
        import inspect
        from modules import db_core

        source = inspect.getsource(db_core.execute_postgres_query)
        self.assertNotIn("replace('?', '%s')", source)
        self.assertNotIn('replace("?", "%s")', source)

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

        with mock.patch.object(db_core, "DATABASE_URL", "postgresql://user:***@localhost/testbot"), \
             mock.patch.object(db_core, "get_postgres_connection", return_value=FakeConnection()):
            result = database.execute_query("SELECT * FROM users WHERE id = %s", (123,), fetch=True)

        self.assertEqual(result, [(1,)])
        self.assertEqual(executed["query"], "SELECT * FROM users WHERE id = %s")
        self.assertEqual(executed["params"], (123,))
        self.assertTrue(executed["committed"])
        self.assertTrue(executed["closed"])

    def test_application_sql_uses_postgres_placeholders(self):
        forbidden_snippets = (
            " = ?",
            "=?",
            "IN (?,",
            "VALUES (?,",
            "SET status=?",
            "SET remark=?",
            "completed_at=?",
            "LIMIT ?",
            "OFFSET ?",
        )
        for relative_path in (
            "modules/constants.py",
            "modules/database.py",
            "modules/db_core.py",
            "modules/db_schema.py",
            "modules/web_auth_routes.py",
            "modules/web_recharge_routes.py",
            "modules/web_activation_routes.py",
            "modules/web_seller_routes.py",
            "modules/order_balance.py",
            "modules/recharge.py",
            "modules/activation_codes.py",
            "modules/custom_prices.py",
            "modules/sellers.py",
            "modules/web_routes.py",
            "modules/telegram_bot.py",
        ):
            source = (PROJECT_ROOT / relative_path).read_text()
            with self.subTest(file=relative_path):
                for snippet in forbidden_snippets:
                    self.assertNotIn(snippet, source)


if __name__ == "__main__":
    unittest.main()
