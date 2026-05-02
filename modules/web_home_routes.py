import logging

from flask import jsonify, render_template, request, session

from modules.constants import STATUS, STATUS_TEXT_ZH, WEB_PRICES, PLAN_OPTIONS
from modules.database import (
    create_order_with_deduction_atomic,
    execute_query,
    get_user_balance,
    get_user_credit_limit,
)
from modules.web_auth_routes import login_required

logger = logging.getLogger(__name__)


def register_home_routes(app, notification_queue):
    @app.route('/', methods=['GET'])
    @login_required
    def index():
        # 显示订单创建表单和最近订单
        logger.info("访问首页")
        logger.info(f"当前会话: {session}")
        
        try:
            orders = execute_query("SELECT id, account, package, status, created_at FROM orders ORDER BY id DESC LIMIT 5", fetch=True)
            logger.info(f"获取到最近订单: {orders}")
            
            # 获取用户余额和透支额度
            user_id = session.get('user_id')
            balance = get_user_balance(user_id)
            credit_limit = get_user_credit_limit(user_id)
            
            return render_template('index.html', 
                                   orders=orders, 
                                   prices=WEB_PRICES, 
                                   plan_options=PLAN_OPTIONS,
                                   username=session.get('username'),
                                   is_admin=session.get('is_admin'),
                                   balance=balance,
                                   credit_limit=credit_limit)
        except Exception as e:
            logger.error(f"获取订单失败: {str(e)}", exc_info=True)
            return render_template('index.html', 
                                   error='获取订单失败', 
                                   prices=WEB_PRICES, 
                                   plan_options=PLAN_OPTIONS,
                                   username=session.get('username'),
                                   is_admin=session.get('is_admin'))

    @app.route('/', methods=['POST'])
    @login_required
    def create_order():
        account = request.form.get('account')
        password = request.form.get('password')
        package = request.form.get('package', '1')
        remark = request.form.get('remark', '')
        
        logger.info(f"收到订单提交请求: 账号={account}, 套餐={package}")
        
        if not account or not password:
            logger.warning("订单提交失败: 账号或密码为空")
            return jsonify({"success": False, "error": "账号和密码不能为空"}), 400
        
        try:
            user_id = session.get('user_id')
            username = session.get('username')
            
            # 使用原子操作创建订单和扣款
            success, message, new_balance, credit_limit = create_order_with_deduction_atomic(
                account, password, package, remark, username, user_id
            )
            
            if not success:
                logger.warning(f"订单创建失败: {message} (用户={username})")
                return jsonify({
                    "success": False,
                    "error": message,
                    "balance": new_balance, # Might be None, but client-side should handle
                    "credit_limit": credit_limit
                }), 400

            logger.info(f"订单提交成功: 用户={username}, 套餐={package}, 新余额={new_balance}")
            
            # 获取最新订单列表并格式化
            orders_raw = execute_query("SELECT id, account, password, package, status, created_at, user_id FROM orders ORDER BY id DESC LIMIT 5", fetch=True)
            orders = []
            
            # 获取新创建的订单ID
            new_order_id = None
            if orders_raw and len(orders_raw) > 0:
                new_order_id = orders_raw[0][0]
                logger.info(f"新创建的订单ID: {new_order_id}")
            
            for o in orders_raw:
                orders.append({
                    "id": o[0],
                    "account": o[1],
                    "password": o[2],
                    "package": o[3],
                    "status": o[4],
                    "status_text": STATUS_TEXT_ZH.get(o[4], o[4]),
                    "created_at": o[5],
                    "accepted_at": "",
                    "completed_at": "",
                    "remark": "",
                    "creator": username, # Simplification, actual creator might differ if admin creates for others
                    "accepted_by": "",
                    "can_cancel": o[4] == STATUS['SUBMITTED'] and (session.get('is_admin') or session.get('user_id') == o[6])
                })
            
            # 触发立即通知卖家 - 获取新创建的订单ID并加入通知队列
            if new_order_id:
                # 加入通知队列，通知类型为new_order
                notification_queue.put({
                    'type': 'new_order',
                    'order_id': new_order_id,
                    'account': account,
                    'password': password,
                    'package': package
                })
                logger.info(f"已将订单 #{new_order_id} 加入通知队列")
            else:
                logger.warning("无法获取新创建的订单ID，无法发送通知")
            
            return jsonify({
                "success": True,
                "message": '订单已提交成功！',
                "balance": new_balance,
                "credit_limit": credit_limit,
                "orders": orders
            })

        except Exception as e:
            logger.error(f"创建订单时发生意外错误: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": "服务器内部错误，请联系管理员。"}), 500


