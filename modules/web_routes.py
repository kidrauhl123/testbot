import logging
import asyncio
from functools import wraps

from flask import Flask, request, render_template, jsonify, session, redirect, url_for, flash

from modules.constants import STATUS, STATUS_TEXT_ZH, WEB_PRICES, PLAN_OPTIONS
from modules.web_auth_routes import login_required, register_auth_routes
from modules.web_recharge_routes import register_recharge_routes
from modules.web_activation_routes import register_activation_routes
from modules.web_seller_routes import register_seller_routes
from modules.web_user_routes import register_user_routes
from modules.web_order_admin_routes import register_order_admin_routes
from modules.web_order_routes import register_order_routes
from modules.database import (
    execute_query,
    get_user_balance, get_user_credit_limit,
    create_order_with_deduction_atomic,
    get_balance_records, get_activation_code, mark_activation_code_used,
    get_china_time, get_postgres_connection
)
import modules.constants as constants

# 设置日志
logger = logging.getLogger(__name__)

# ===== Web路由 =====
def register_routes(app, notification_queue):
    register_auth_routes(app)

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

    @app.route('/admin')
    @login_required
    def admin_dashboard():
        if not session.get('is_admin'):
            return redirect(url_for('index'))
        return render_template('admin.html')

    @app.route('/dashboard')
    @login_required
    def user_dashboard():
        """用户仪表盘"""
        user_id = session.get('user_id')
        username = session.get('username')
        is_admin = session.get('is_admin', 0)
        
        # 获取用户余额和透支额度
        balance = get_user_balance(user_id)
        credit_limit = get_user_credit_limit(user_id)
        
        return render_template('dashboard.html', 
                              username=username, 
                              is_admin=is_admin,
                              balance=balance,
                              credit_limit=credit_limit)


    register_user_routes(app, admin_required)

        
    register_order_admin_routes(app, admin_required)

    register_seller_routes(app, admin_required)


    register_recharge_routes(app, notification_queue, admin_required)

    @app.route('/api/balance/records')
    @login_required
    def api_balance_records():
        """获取用户余额明细记录"""
        try:
            limit = int(request.args.get('limit', 50))
            offset = int(request.args.get('offset', 0))
            user_id = session.get('user_id')
            is_admin = session.get('is_admin', False)
            
            # 如果是管理员，可以查看指定用户的记录或所有用户的记录
            view_user_id = None
            if is_admin and 'user_id' in request.args:
                view_user_id = int(request.args.get('user_id'))
            elif not is_admin:
                view_user_id = user_id  # 普通用户只能查看自己的记录
            
            # 获取余额明细记录
            records = get_balance_records(view_user_id, limit, offset)
                
            return jsonify({
                "success": True, 
                "records": records
            })
        except Exception as e:
            logger.error(f"获取余额明细记录失败: {str(e)}", exc_info=True)
            return jsonify({
                "success": False,
                "error": "获取余额明细记录失败，请刷新重试"
            }), 500
            
    @app.route('/api/user-prices')
    @login_required
    def api_get_user_prices():
        """获取用户的定制价格"""
        try:
            user_id = session.get('user_id')
            
            if not user_id:
                return jsonify({"success": False, "error": "未登录"})
                
            # 导入get_user_package_price函数
            from modules.constants import WEB_PRICES, get_user_package_price
            
            # 获取用户所有套餐的价格
            custom_prices = {}
            for package in WEB_PRICES.keys():
                custom_prices[package] = get_user_package_price(user_id, package)
                
            return jsonify({
                "success": True, 
                "user_id": user_id, 
                "prices": custom_prices,
                "default_prices": WEB_PRICES
            })
        except Exception as e:
            logger.error(f"获取用户价格失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": f"获取用户价格失败: {str(e)}"})

    @app.route('/redeem', methods=['GET'])
    def redeem_page():
        """激活码兑换页面"""
        # 从URL获取激活码参数
        code = request.args.get('code', '')
        
        try:
            orders = execute_query("SELECT id, account, package, status, created_at FROM orders ORDER BY id DESC LIMIT 5", fetch=True)
            
            # 如果有激活码参数，检查是否已被使用，并获取相关订单信息
            order_info = None
            code_info = None
            
            # 1. 检查URL中的激活码
            if code:
                code_info = get_activation_code(code)
                if code_info:
                    # 查找使用此激活码创建的订单
                    order_query = execute_query(
                        "SELECT id, account, package, status, created_at, completed_at, remark FROM orders WHERE remark LIKE %s ORDER BY id DESC LIMIT 1", 
                        (f"%通过激活码兑换: {code}%",), 
                        fetch=True
                    )
                    if order_query and len(order_query) > 0:
                        order = order_query[0]
                        order_info = {
                            "id": order[0],
                            "account": order[1],
                            "package": order[2],
                            "status": order[3],
                            "status_text": STATUS_TEXT_ZH.get(order[3], order[3]),
                            "created_at": order[4],
                            "completed_at": order[5] or "",
                            "remark": order[6]
                        }
            
            # 2. 如果URL中没有激活码或没找到订单，检查session中的上次兑换记录
            if not order_info and 'last_redeemed_code' in session and 'last_order_id' in session:
                last_code = session.get('last_redeemed_code')
                last_order_id = session.get('last_order_id')
                
                # 查询订单详情
                order_query = execute_query(
                    "SELECT id, account, package, status, created_at, completed_at, remark FROM orders WHERE id = %s", 
                    (last_order_id,), 
                    fetch=True
                )
                
                if order_query and len(order_query) > 0:
                    order = order_query[0]
                    order_info = {
                        "id": order[0],
                        "account": order[1],
                        "package": order[2],
                        "status": order[3],
                        "status_text": STATUS_TEXT_ZH.get(order[3], order[3]),
                        "created_at": order[4],
                        "completed_at": order[5] or "",
                        "remark": order[6]
                    }
                    
                    # 如果URL没有激活码，但session有，使用session中的激活码
                    if not code:
                        code = last_code
                        code_info = get_activation_code(code)
            
            return render_template('redeem.html', 
                                   code=code,
                                   orders=orders, 
                                   status_text=STATUS_TEXT_ZH,
                                   username=session.get('username'),
                                   is_admin=session.get('is_admin'),
                                   balance=get_user_balance(session.get('user_id', 0)),
                                   order_info=order_info,
                                   code_info=code_info)
        except Exception as e:
            logger.error(f"加载兑换页面失败: {str(e)}", exc_info=True)
            return render_template('redeem.html', 
                                   code=code,
                                   error='加载数据失败', 
                                   username=session.get('username'),
                                   is_admin=session.get('is_admin'))

    @app.route('/redeem/<code>', methods=['GET'])
    def redeem_with_code(code):
        """带激活码的兑换链接"""
        return redirect(url_for('redeem_page', code=code))

    @app.route('/api/verify-code', methods=['POST'])
    def verify_activation_code():
        """验证激活码"""
        try:
            code = request.json.get('code', '')
            
            if not code:
                return jsonify({"success": False, "message": "请输入激活码"}), 400
            
            # 获取激活码信息
            code_info = get_activation_code(code)
            
            # 检查激活码是否存在
            if not code_info:
                logger.warning(f"无效的激活码: {code}")
                return jsonify({"success": False, "message": "无效的激活码"}), 400
            
            # 检查激活码是否已使用
            if code_info['is_used']:
                # 查找使用此激活码创建的订单
                order_query = execute_query(
                    "SELECT id, status FROM orders WHERE remark LIKE %s ORDER BY id DESC LIMIT 1", 
                    (f"%通过激活码兑换: {code}%",), 
                    fetch=True
                )
                
                if order_query and len(order_query) > 0:
                    order_id = order_query[0][0]
                    order_status = order_query[0][1]
                    status_text = STATUS_TEXT_ZH.get(order_status, order_status)
                    logger.warning(f"激活码已被使用: {code}, 关联订单 #{order_id}, 状态: {status_text}")
                    return jsonify({
                        "success": False, 
                        "message": f"此激活码已被使用，关联订单 #{order_id}，状态: {status_text}"
                    }), 400
                else:
                    logger.warning(f"激活码已被使用: {code}, 但未找到关联订单")
                    return jsonify({"success": False, "message": "此激活码已被使用"}), 400
            
            # 返回成功和套餐信息
            return jsonify({
                "success": True, 
                "package": code_info['package'],
                "message": "有效的激活码"
            })
                
        except Exception as e:
            logger.error(f"验证激活码失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "message": "验证失败，请稍后再试"}), 500

    @app.route('/redeem', methods=['POST'])
    def process_redeem():
        """处理激活码兑换请求"""
        try:
            # 从JSON获取数据
            data = request.json
            code = data.get('code', '')
            account = data.get('account', '')
            password = data.get('password', '')
            remark = data.get('remark', '')
            
            if not code:
                return jsonify({"success": False, "error": "请输入激活码"}), 400
            
            if not account or not password:
                return jsonify({"success": False, "error": "请输入账号和密码"}), 400
            
            # 获取激活码信息
            code_info = get_activation_code(code)
            
            # 检查激活码是否存在
            if not code_info:
                logger.warning(f"无效的激活码: {code}")
                return jsonify({"success": False, "error": "无效的激活码"}), 400
            
            # 检查激活码是否已使用 - 使用数据库事务确保原子性
            if code_info['is_used']:
                # 查找使用此激活码创建的订单
                order_query = execute_query(
                    "SELECT id, status FROM orders WHERE remark LIKE %s ORDER BY id DESC LIMIT 1", 
                    (f"%通过激活码兑换: {code}%",), 
                    fetch=True
                )
                
                if order_query and len(order_query) > 0:
                    order_id = order_query[0][0]
                    order_status = order_query[0][1]
                    status_text = STATUS_TEXT_ZH.get(order_status, order_status)
                    logger.warning(f"激活码已被使用: {code}, 关联订单 #{order_id}, 状态: {status_text}")
                    return jsonify({
                        "success": False, 
                        "error": f"此激活码已被使用，关联订单 #{order_id}，状态: {status_text}"
                    }), 400
                else:
                    logger.warning(f"激活码已被使用: {code}, 但未找到关联订单")
                    return jsonify({"success": False, "error": "此激活码已被使用"}), 400
            
            # 用户ID和用户名 - 如果已登录则使用登录信息，否则使用临时值
            user_id = session.get('user_id', 0)  # 未登录用户使用0作为ID
            username = session.get('username', '未登录用户')
            
            # 创建订单记录（状态为已提交，而非已完成）
            now = get_china_time()
            order_id = None
            
            # 使用数据库事务确保原子性操作
            conn = None
            cursor = None
            try:
                conn = get_postgres_connection()
                cursor = conn.cursor()
                conn.autocommit = False

                # 1. 先检查激活码是否仍然可用
                cursor.execute(
                    "SELECT id, is_used FROM activation_codes WHERE code = %s FOR UPDATE",
                    (code,)
                )
                code_check = cursor.fetchone()
                if not code_check or code_check[1] == 1:
                    conn.rollback()
                    return jsonify({"success": False, "error": "此激活码已被使用或不存在"}), 400

                # 2. 创建订单
                cursor.execute("""
                    INSERT INTO orders (account, password, package, remark, status, created_at, user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    account,
                    password,
                    code_info['package'],
                    f"通过激活码兑换: {code}",
                    STATUS['SUBMITTED'],
                    now,
                    user_id
                ))
                order_id = cursor.fetchone()[0]

                # 3. 标记激活码为已使用
                cursor.execute("""
                    UPDATE activation_codes
                    SET is_used = 1, used_at = %s, used_by = %s
                    WHERE id = %s
                """, (now, user_id if user_id > 0 else None, code_info['id']))

                conn.commit()

                # 记录成功日志
                logger.info(f"用户 {username} 成功兑换激活码 {code}, 套餐: {code_info['package']}, 订单ID: {order_id}")

                # 将激活码和订单ID保存到session，以便刷新页面后仍能显示
                session['last_redeemed_code'] = code
                session['last_order_id'] = order_id

            except Exception as e:
                if conn:
                    conn.rollback()
                logger.error(f"激活码兑换事务失败: {str(e)}", exc_info=True)
                return jsonify({"success": False, "error": f"处理激活码兑换失败: {str(e)}"}), 500
            finally:
                if cursor:
                    cursor.close()
                if conn:
                    conn.close()
            
            # 获取完整的订单信息
            order = {
                "id": order_id,
                "account": account,
                "password": password,
                "package": code_info['package'],
                "status": STATUS['SUBMITTED'],
                "status_text": STATUS_TEXT_ZH.get(STATUS['SUBMITTED'], STATUS['SUBMITTED']),
                "created_at": now,
                "completed_at": None,  # 未完成
                "remark": f"通过激活码兑换: {code}",
                "creator": username,
                "accepted_by": "",
                "can_cancel": True  # 已提交的订单可以取消
            }
            
            # 如果用户已登录并成功完成兑换，可以选择重定向到仪表板
            redirect_url = url_for('dashboard') if 'user_id' in session else None
            
            # 返回成功消息和订单数据
            return jsonify({
                "success": True, 
                "message": f"激活码兑换成功，订单已提交，等待处理!",
                "orders": [order],  # 返回包含单个订单的数组
                "redirect": redirect_url,
                "redirect_delay": 3000  # 延迟3秒后重定向，给用户足够时间查看结果
            })
                
        except Exception as e:
            logger.error(f"处理激活码兑换请求失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": "处理请求失败，请稍后再试"}), 500

    register_activation_routes(app, admin_required)
