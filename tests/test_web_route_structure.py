import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class WebRouteStructureTests(unittest.TestCase):
    def test_auth_routes_are_registered_by_auth_module(self):
        from flask import Flask
        from modules.web_auth_routes import register_auth_routes

        app = Flask(__name__)
        app.secret_key = "test-secret"

        register_auth_routes(app)

        rules = {rule.endpoint: rule.rule for rule in app.url_map.iter_rules()}
        self.assertEqual(rules["login"], "/login")
        self.assertEqual(rules["register"], "/register")
        self.assertEqual(rules["logout"], "/logout")

    def test_web_routes_delegates_auth_route_registration(self):
        source = (PROJECT_ROOT / "modules" / "web_routes.py").read_text()
        self.assertIn("register_auth_routes(app)", source)
        self.assertNotIn("@app.route('/login'", source)
        self.assertNotIn("@app.route('/register'", source)
        self.assertNotIn("@app.route('/logout'", source)

    def test_recharge_routes_are_registered_by_recharge_module(self):
        from flask import Flask
        from modules.web_recharge_routes import register_recharge_routes

        def admin_required(func):
            return func

        app = Flask(__name__)
        app.secret_key = "test-secret"

        register_recharge_routes(app, notification_queue=None, admin_required=admin_required)

        rules = {rule.endpoint: rule.rule for rule in app.url_map.iter_rules()}
        self.assertEqual(rules["recharge_page"], "/recharge")
        self.assertEqual(rules["submit_recharge"], "/recharge")
        self.assertEqual(rules["admin_recharge_requests"], "/admin/recharge-requests")
        self.assertEqual(rules["approve_recharge"], "/admin/api/recharge/<int:request_id>/approve")
        self.assertEqual(rules["reject_recharge"], "/admin/api/recharge/<int:request_id>/reject")

    def test_web_routes_delegates_recharge_route_registration(self):
        source = (PROJECT_ROOT / "modules" / "web_routes.py").read_text()
        self.assertIn("register_recharge_routes(app, notification_queue, admin_required)", source)
        self.assertNotIn("@app.route('/recharge'", source)
        self.assertNotIn("@app.route('/admin/recharge-requests'", source)
        self.assertNotIn("@app.route('/admin/api/recharge/<int:request_id>/approve'", source)
        self.assertNotIn("@app.route('/admin/api/recharge/<int:request_id>/reject'", source)

    def test_admin_activation_routes_are_registered_by_activation_module(self):
        from flask import Flask
        from modules.web_activation_routes import register_activation_routes

        def admin_required(func):
            return func

        app = Flask(__name__)
        app.secret_key = "test-secret"

        register_activation_routes(app, admin_required=admin_required)

        rules = {rule.endpoint: rule.rule for rule in app.url_map.iter_rules()}
        self.assertEqual(rules["admin_activation_codes"], "/admin/activation-codes")
        self.assertEqual(rules["admin_api_get_activation_codes"], "/admin/api/activation-codes")
        self.assertEqual(rules["admin_api_create_activation_code"], "/admin/api/activation-codes")
        self.assertEqual(
            rules["admin_api_batch_delete_activation_codes"],
            "/admin/api/activation-codes/batch-delete",
        )
        self.assertEqual(
            rules["admin_api_export_activation_codes"],
            "/admin/api/activation-codes/export",
        )

    def test_web_routes_delegates_activation_route_registration(self):
        source = (PROJECT_ROOT / "modules" / "web_routes.py").read_text()
        self.assertIn("register_activation_routes(app, admin_required)", source)
        self.assertNotIn("@app.route('/admin/activation-codes'", source)
        self.assertNotIn("@app.route('/admin/api/activation-codes'", source)
        self.assertNotIn("@app.route('/admin/api/activation-codes/batch-delete'", source)
        self.assertNotIn("@app.route('/admin/api/activation-codes/export'", source)

    def test_seller_routes_are_registered_by_seller_module(self):
        from flask import Flask
        from modules.web_seller_routes import register_seller_routes

        def admin_required(func):
            return func

        app = Flask(__name__)
        app.secret_key = "test-secret"

        register_seller_routes(app, admin_required=admin_required)

        rules = {rule.endpoint: rule.rule for rule in app.url_map.iter_rules()}
        self.assertEqual(rules["admin_api_get_sellers"], "/admin/api/sellers")
        self.assertEqual(rules["admin_api_add_seller"], "/admin/api/sellers")
        self.assertEqual(rules["admin_api_remove_seller"], "/admin/api/sellers/<int:telegram_id>")
        self.assertEqual(rules["admin_api_toggle_seller"], "/admin/api/sellers/<int:telegram_id>/toggle")
        self.assertEqual(rules["admin_api_toggle_seller_admin"], "/admin/api/sellers/toggle_admin")

    def test_web_routes_delegates_seller_route_registration(self):
        source = (PROJECT_ROOT / "modules" / "web_routes.py").read_text()
        self.assertIn("register_seller_routes(app, admin_required)", source)
        self.assertNotIn("@app.route('/admin/api/sellers'", source)
        self.assertNotIn("@app.route('/admin/api/sellers/<int:telegram_id>'", source)
        self.assertNotIn("@app.route('/admin/api/sellers/<int:telegram_id>/toggle'", source)
        self.assertNotIn("@app.route('/admin/api/sellers/toggle_admin'", source)

    def test_user_admin_routes_are_registered_by_user_module(self):
        from flask import Flask
        from modules.web_user_routes import register_user_routes

        def admin_required(func):
            return func

        app = Flask(__name__)
        app.secret_key = "test-secret"

        register_user_routes(app, admin_required=admin_required)

        rules = {rule.endpoint: rule.rule for rule in app.url_map.iter_rules()}
        self.assertEqual(rules["admin_api_users"], "/admin/api/users")
        self.assertEqual(rules["admin_update_user_balance"], "/admin/api/users/<int:user_id>/balance")
        self.assertEqual(rules["admin_update_user_credit"], "/admin/api/users/<int:user_id>/credit")
        self.assertEqual(rules["admin_get_user_custom_prices"], "/admin/api/users/<int:user_id>/custom-prices")
        self.assertEqual(rules["admin_set_user_custom_price"], "/admin/api/users/<int:user_id>/custom-prices")

    def test_web_routes_delegates_user_route_registration(self):
        source = (PROJECT_ROOT / "modules" / "web_routes.py").read_text()
        self.assertIn("register_user_routes(app, admin_required)", source)
        self.assertNotIn("@app.route('/admin/api/users'", source)
        self.assertNotIn("@app.route('/admin/api/users/<int:user_id>/balance'", source)
        self.assertNotIn("@app.route('/admin/api/users/<int:user_id>/credit'", source)
        self.assertNotIn("@app.route('/admin/api/users/<int:user_id>/custom-prices'", source)

    def test_admin_order_routes_are_registered_by_order_admin_module(self):
        from flask import Flask
        from modules.web_order_admin_routes import register_order_admin_routes

        def admin_required(func):
            return func

        app = Flask(__name__)
        app.secret_key = "test-secret"

        register_order_admin_routes(app, admin_required=admin_required)

        routes = {rule.endpoint: (rule.rule, rule.methods) for rule in app.url_map.iter_rules()}
        self.assertEqual(routes["admin_api_orders"][0], "/admin/api/orders")
        self.assertIn("GET", routes["admin_api_orders"][1])
        self.assertEqual(routes["admin_api_order_detail"][0], "/admin/api/orders/<int:order_id>")
        self.assertIn("GET", routes["admin_api_order_detail"][1])
        self.assertEqual(routes["admin_api_edit_order"][0], "/admin/api/orders/<int:order_id>")
        self.assertIn("PUT", routes["admin_api_edit_order"][1])
        self.assertEqual(routes["admin_api_batch_delete_orders"][0], "/admin/api/orders/batch-delete")
        self.assertIn("POST", routes["admin_api_batch_delete_orders"][1])

    def test_web_routes_delegates_order_admin_route_registration(self):
        source = (PROJECT_ROOT / "modules" / "web_routes.py").read_text()
        self.assertIn("register_order_admin_routes(app, admin_required)", source)
        self.assertNotIn("@app.route('/admin/api/orders')", source)
        self.assertNotIn("@app.route('/admin/api/orders/<int:order_id>')", source)
        self.assertNotIn("@app.route('/admin/api/orders/<int:order_id>', methods=['PUT'])", source)
        self.assertNotIn("@app.route('/admin/api/orders/batch-delete'", source)

    def test_order_action_routes_are_registered_by_order_module(self):
        from flask import Flask
        from modules.web_order_routes import register_order_routes

        app = Flask(__name__)
        app.secret_key = "test-secret"

        register_order_routes(app, notification_queue=None)

        routes = {rule.endpoint: (rule.rule, rule.methods) for rule in app.url_map.iter_rules()}
        self.assertEqual(routes["web_user_stats"][0], "/orders/stats/web/<user_id>")
        self.assertIn("GET", routes["web_user_stats"][1])
        self.assertEqual(routes["orders_recent"][0], "/orders/recent")
        self.assertIn("GET", routes["orders_recent"][1])
        self.assertEqual(routes["cancel_order"][0], "/orders/cancel/<int:oid>")
        self.assertIn("POST", routes["cancel_order"][1])
        self.assertEqual(routes["dispute_order"][0], "/orders/dispute/<int:oid>")
        self.assertIn("POST", routes["dispute_order"][1])
        self.assertEqual(routes["urge_order"][0], "/orders/urge/<int:oid>")
        self.assertIn("POST", routes["urge_order"][1])

    def test_web_routes_delegates_order_route_registration(self):
        source = (PROJECT_ROOT / "modules" / "web_routes.py").read_text()
        self.assertIn("register_order_routes(app, notification_queue)", source)
        self.assertNotIn("@app.route('/orders/stats/web/<user_id>'", source)
        self.assertNotIn("@app.route('/orders/recent'", source)
        self.assertNotIn("@app.route('/orders/cancel/<int:oid>'", source)
        self.assertNotIn("@app.route('/orders/dispute/<int:oid>'", source)
        self.assertNotIn("@app.route('/orders/urge/<int:oid>'", source)

    def test_account_routes_are_registered_by_account_module(self):
        from flask import Flask
        from modules.web_account_routes import register_account_routes

        app = Flask(__name__)
        app.secret_key = "test-secret"

        register_account_routes(app)

        routes = {rule.endpoint: (rule.rule, rule.methods) for rule in app.url_map.iter_rules()}
        self.assertEqual(routes["admin_dashboard"][0], "/admin")
        self.assertIn("GET", routes["admin_dashboard"][1])
        self.assertEqual(routes["user_dashboard"][0], "/dashboard")
        self.assertIn("GET", routes["user_dashboard"][1])
        self.assertEqual(routes["api_balance_records"][0], "/api/balance/records")
        self.assertIn("GET", routes["api_balance_records"][1])
        self.assertEqual(routes["api_get_user_prices"][0], "/api/user-prices")
        self.assertIn("GET", routes["api_get_user_prices"][1])

    def test_web_routes_delegates_account_route_registration(self):
        source = (PROJECT_ROOT / "modules" / "web_routes.py").read_text()
        self.assertIn("register_account_routes(app)", source)
        self.assertNotIn("@app.route('/admin')", source)
        self.assertNotIn("@app.route('/dashboard')", source)
        self.assertNotIn("@app.route('/api/balance/records'", source)
        self.assertNotIn("@app.route('/api/user-prices'", source)

    def test_full_web_routes_register_without_name_errors(self):
        from flask import Flask
        from modules.web_routes import register_routes

        app = Flask(__name__)
        app.secret_key = "test-secret"

        register_routes(app, notification_queue=None)

        endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
        self.assertIn("login", endpoints)
        self.assertIn("index", endpoints)
        self.assertIn("admin_dashboard", endpoints)


if __name__ == "__main__":
    unittest.main()
