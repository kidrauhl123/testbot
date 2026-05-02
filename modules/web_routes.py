from functools import wraps

from flask import jsonify, session

from modules.web_auth_routes import register_auth_routes
from modules.web_recharge_routes import register_recharge_routes
from modules.web_activation_routes import register_activation_routes
from modules.web_seller_routes import register_seller_routes
from modules.web_user_routes import register_user_routes
from modules.web_order_admin_routes import register_order_admin_routes
from modules.web_order_routes import register_order_routes
from modules.web_account_routes import register_account_routes
from modules.web_redeem_routes import register_redeem_routes
from modules.web_home_routes import register_home_routes
from modules.web_utility_routes import register_utility_routes

# ===== Web路由 =====
def register_routes(app, notification_queue):
    register_auth_routes(app)

    register_home_routes(app, notification_queue)

    register_order_routes(app, notification_queue)

    register_utility_routes(app, notification_queue)

    # ==================================
    #        后台管理 (Admin)
    # ==================================
    def admin_required(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not session.get('is_admin'):
                return jsonify({"error": "管理员权限不足"}), 403
            return f(*args, **kwargs)
        return decorated_function



    register_account_routes(app)

    register_user_routes(app, admin_required)

        
    register_order_admin_routes(app, admin_required)

    register_seller_routes(app, admin_required)


    register_recharge_routes(app, notification_queue, admin_required)



    register_redeem_routes(app)

    register_activation_routes(app, admin_required)
