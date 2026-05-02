import logging
import asyncio
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
from modules.database import get_china_time
from modules.telegram_bot import check_and_push_orders

# 设置日志
logger = logging.getLogger(__name__)

# ===== Web路由 =====
def register_routes(app, notification_queue):
    register_auth_routes(app)

    register_home_routes(app, notification_queue)

    register_order_routes(app, notification_queue)

    # 添加一个测试路由
    @app.route('/test')
    def test_route():
        logger.info("访问测试路由")
        return jsonify({
            'status': 'ok',
            'message': '服务器正常运行',
            'time': get_china_time(),
        })

    # 添加一个路由用于手动触发订单检查
    @app.route('/check-orders')
    def manual_check_orders():
        logger.info("手动触发订单检查")
        
        try:
            # 检查机器人实例
            if notification_queue is None:
                return jsonify({
                    'status': 'error',
                    'message': 'Telegram机器人实例未初始化'
                })
            
            # 创建事件循环并执行订单检查
            asyncio.run(check_and_push_orders())
            
            return jsonify({
                'status': 'ok',
                'message': '订单检查已触发',
                'time': get_china_time()
            })
        except Exception as e:
            logger.error(f"手动触发订单检查失败: {str(e)}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': f'触发失败: {str(e)}'
            })

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
