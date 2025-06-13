import os
import time
import logging
import asyncio
from functools import wraps
from datetime import datetime, timedelta
import pytz

from flask import Flask, request, render_template, jsonify, session, redirect, url_for, flash

from modules.constants import STATUS, STATUS_TEXT_ZH, WEB_PRICES, PLAN_OPTIONS, REASON_TEXT_ZH
from modules.database import (
    execute_query, hash_password, get_all_sellers, add_seller, remove_seller, toggle_seller_status,
    get_user_balance, get_user_credit_limit, set_user_balance, set_user_credit_limit, refund_order, 
    create_order_with_deduction_atomic, get_user_recharge_requests, create_recharge_request,
    get_pending_recharge_requests, approve_recharge_request, reject_recharge_request
)
import modules.constants as constants

# 设置日志
logger = logging.getLogger(__name__)

# 中国时区
CN_TIMEZONE = pytz.timezone('Asia/Shanghai')

# 获取中国时间的函数
def get_china_time():
    """获取当前中国时间（UTC+8）"""
    utc_now = datetime.now(pytz.utc)
    china_now = utc_now.astimezone(CN_TIMEZONE)
    return china_now.strftime("%Y-%m-%d %H:%M:%S")

# ===== 登录装饰器 =====
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ===== Web路由 =====
def register_routes(app, notification_queue):
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            
            if not username or not password:
                return render_template('login.html', error='请填写用户名和密码')
                
            # 验证用户
            hashed_password = hash_password(password)
            user = execute_query("SELECT id, username, is_admin FROM users WHERE username=? AND password_hash=?",
                            (username, hashed_password), fetch=True)
            
            if user:
                user_id, username, is_admin = user[0]
                session['user_id'] = user_id
                session['username'] = username
                session['is_admin'] = is_admin
                
                # 更新最后登录时间
                execute_query("UPDATE users SET last_login=? WHERE id=?",
                            (get_china_time(), user_id))
                
                logger.info(f"用户 {username} 登录成功")
                return redirect(url_for('index'))
            else:
                logger.warning(f"用户 {username} 登录失败 - 密码错误")
                return render_template('login.html', error='用户名或密码错误')
        
        return render_template('login.html')

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            confirm_password = request.form.get('password_confirm')  # 修正字段名称
            
            # 验证输入
            if not username or not password or not confirm_password:
                return render_template('register.html', error='请填写所有字段')
                
            if password != confirm_password:
                return render_template('register.html', error='两次密码输入不一致')
            
            # 检查用户名是否已存在
            existing_user = execute_query("SELECT id FROM users WHERE username=?", (username,), fetch=True)
            if existing_user:
                return render_template('register.html', error='用户名已存在')
            
            # 创建用户
            hashed_password = hash_password(password)
            execute_query("""
                INSERT INTO users (username, password_hash, is_admin, created_at) 
                VALUES (?, ?, 0, ?)
            """, (username, hashed_password, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            
            return redirect(url_for('login'))
        
        return render_template('register.html')

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

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
                print(f"DEBUG: 新创建的订单ID: {new_order_id}")
            
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
                    'package': package,
                    'web_user_id': username
                })
                logger.info(f"已将订单 #{new_order_id} 加入通知队列")
                print(f"DEBUG: 已将订单 #{new_order_id} 加入通知队列")
            else:
                logger.warning("无法获取新创建的订单ID，无法发送通知")
                print("WARNING: 无法获取新创建的订单ID，无法发送通知")
            
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

    @app.route('/orders/stats/web/<user_id>')
    @login_required
    def web_user_stats(user_id):
        """显示指定用户的订单统计"""
        # 只允许管理员访问他人的统计，或者用户查看自己的
        if session.get('username') != user_id and not session.get('is_admin'):
            return jsonify({"error": "权限不足"}), 403
        
        # 查询提交和完成的订单
        submitted_counts = execute_query("""
            SELECT package, COUNT(*) as count
            FROM orders
            WHERE web_user_id = ? AND status = ?
            GROUP BY package
        """, (user_id, STATUS['SUBMITTED']), fetch=True)
        
        completed_counts = execute_query("""
            SELECT package, COUNT(*) as count
            FROM orders
            WHERE web_user_id = ? AND status = ?
            GROUP BY package
        """, (user_id, STATUS['COMPLETED']), fetch=True)
        
        failed_counts = execute_query("""
            SELECT package, COUNT(*) as count
            FROM orders
            WHERE web_user_id = ? AND status = ?
            GROUP BY package
        """, (user_id, STATUS['FAILED']), fetch=True)
        
        cancelled_counts = execute_query("""
            SELECT package, COUNT(*) as count
            FROM orders
            WHERE web_user_id = ? AND status = ?
            GROUP BY package
        """, (user_id, STATUS['CANCELLED']), fetch=True)
        
        # 重新组织数据
        stats = {}
        for pkg in WEB_PRICES.keys():
            stats[pkg] = {
                "submitted": 0, 
                "completed": 0, 
                "failed": 0,
                "cancelled": 0,
                "price": WEB_PRICES.get(pkg, 0)
            }
        
        # 填充数据
        for pkg, count in submitted_counts:
            stats[pkg]["submitted"] = count
            
        for pkg, count in completed_counts:
            stats[pkg]["completed"] = count
            
        for pkg, count in failed_counts:
            stats[pkg]["failed"] = count
            
        for pkg, count in cancelled_counts:
            stats[pkg]["cancelled"] = count
        
        # 计算总额
        total_submitted = sum(s["submitted"] for s in stats.values())
        total_completed = sum(s["completed"] for s in stats.values())
        total_failed = sum(s["failed"] for s in stats.values())
        total_cancelled = sum(s["cancelled"] for s in stats.values())
        
        total_amount = sum(s["completed"] * s["price"] for s in stats.values())
        
        return jsonify({
            "user": user_id,
            "stats": {k: v for k, v in stats.items()},
            "total": {
                "submitted": total_submitted,
                "completed": total_completed,
                "failed": total_failed,
                "cancelled": total_cancelled,
                "amount": total_amount
            }
        })

    @app.route('/orders/recent')
    @login_required
    def orders_recent():
        """获取用户最近的订单"""
        # 获取查询参数
        limit = int(request.args.get('limit', 10))
        offset = int(request.args.get('offset', 0))
        user_filter = ""
        params = []
        
        # 非管理员只能看到自己的订单
        if not session.get('is_admin'):
            user_filter = "WHERE user_id = ?"
            params.append(session.get('user_id'))
        
        # 查询订单
        orders = execute_query(f"""
            SELECT id, account, password, package, status, created_at, accepted_at, completed_at,
                   remark, web_user_id, user_id, accepted_by, accepted_by_username, accepted_by_first_name
            FROM orders 
            {user_filter}
            ORDER BY id DESC LIMIT ? OFFSET ?
        """, params + [limit, offset], fetch=True)
        
        logger.info(f"查询到 {len(orders)} 条订单记录")
        
        # 格式化数据
        formatted_orders = []
        for order in orders:
            oid, account, password, package, status, created_at, accepted_at, completed_at, remark, web_user_id, user_id, accepted_by, accepted_by_username, accepted_by_first_name = order
            
            # 优先使用昵称，其次是用户名，最后是ID
            seller_display = accepted_by_first_name or accepted_by_username or accepted_by
            
            # 如果是失败状态，翻译失败原因
            translated_remark = remark
            if status == STATUS['FAILED'] and remark:
                translated_remark = REASON_TEXT_ZH.get(remark, remark)
            
            order_data = {
                "id": oid,
                "account": account,
                "password": password,
                "package": package,
                "status": status,
                "status_text": STATUS_TEXT_ZH.get(status, status),
                "created_at": created_at,
                "accepted_at": accepted_at or "",
                "completed_at": completed_at or "",
                "remark": translated_remark or "",
                "creator": web_user_id,
                "accepted_by": seller_display or "",
                "can_cancel": status == STATUS['SUBMITTED'] and (session.get('is_admin') or session.get('user_id') == user_id)
            }
            formatted_orders.append(order_data)
        
        # 直接返回订单列表，而不是嵌套在orders字段中
        return jsonify(formatted_orders)

    @app.route('/orders/cancel/<int:oid>', methods=['POST'])
    @login_required
    def cancel_order(oid):
        """取消订单"""
        user_id = session.get('user_id')
        is_admin = session.get('is_admin', 0)
        
        # 获取订单信息
        order = execute_query("""
            SELECT id, user_id, status, package, refunded 
            FROM orders 
            WHERE id=?
        """, (oid,), fetch=True)
        
        if not order:
            return jsonify({"error": "订单不存在"}), 404
            
        order_id, order_user_id, status, package, refunded = order[0]
        
        # 验证权限：只能取消自己的订单，或者管理员可以取消任何人的订单
        if user_id != order_user_id and not is_admin:
            return jsonify({"error": "权限不足"}), 403
            
        # 只能取消"已提交"状态的订单
        if status != STATUS['SUBMITTED']:
            return jsonify({"error": "只能取消待处理的订单"}), 400
            
        # 更新订单状态为已取消
        execute_query("UPDATE orders SET status=? WHERE id=?", 
                      (STATUS['CANCELLED'], oid))
        
        logger.info(f"订单已取消: ID={oid}")
        
        # 如果订单未退款，执行退款操作
        if not refunded:
            success, result = refund_order(oid)
            if success:
                logger.info(f"订单退款成功: ID={oid}, 新余额={result}")
            else:
                logger.warning(f"订单退款失败: ID={oid}, 原因={result}")
        
        return jsonify({"success": True})

    @app.route('/orders/dispute/<int:oid>', methods=['POST'])
    @login_required
    def dispute_order(oid):
        """质疑已完成的订单（用户发现充值未成功）"""
        user_id = session.get('user_id')
        is_admin = session.get('is_admin', 0)
        
        # 获取订单信息
        order = execute_query("""
            SELECT id, user_id, status, package, accepted_by, account, password
            FROM orders 
            WHERE id=?
        """, (oid,), fetch=True)
        
        if not order:
            return jsonify({"error": "订单不存在"}), 404
            
        order_id, order_user_id, status, package, accepted_by, account, password = order[0]
        
        # 验证权限：只能质疑自己的订单，或者管理员可以质疑任何人的订单
        if user_id != order_user_id and not is_admin:
            return jsonify({"error": "权限不足"}), 403
            
        # 只能质疑"已完成"状态的订单
        if status != STATUS['COMPLETED']:
            return jsonify({"error": "只能质疑已完成的订单"}), 400
            
        # 更新订单状态为正在质疑
        execute_query("UPDATE orders SET status=? WHERE id=?", 
                      (STATUS['DISPUTING'], oid))
        
        logger.info(f"订单已被质疑: ID={oid}, 用户ID={user_id}")
        
        # 如果有接单人，尝试通过Telegram通知接单人
        if accepted_by:
            logger.info(f"订单 {oid} 有接单人 {accepted_by}，准备发送TG通知。")
            notification_queue.put({
                'type': 'dispute',
                'order_id': oid,
                'seller_id': accepted_by,
                'account': account,
                'password': password,
                'package': package
            })
            logger.info(f"已将订单 {oid} 的质疑通知任务放入队列。")
        
        return jsonify({"success": True})

    @app.route('/orders/urge/<int:oid>', methods=['POST'])
    @login_required
    def urge_order(oid):
        """催促已接单但未完成的订单（超过20分钟未处理）"""
        user_id = session.get('user_id')
        is_admin = session.get('is_admin', 0)
        
        # 获取订单信息
        order = execute_query("""
            SELECT id, user_id, status, package, accepted_by, accepted_at, account, password
            FROM orders 
            WHERE id=?
        """, (oid,), fetch=True)
        
        if not order:
            return jsonify({"error": "订单不存在"}), 404
            
        order_id, order_user_id, status, package, accepted_by, accepted_at, account, password = order[0]
        
        # 验证权限：只能催促自己的订单，或者管理员可以催促任何人的订单
        if user_id != order_user_id and not is_admin:
            return jsonify({"error": "权限不足"}), 403
            
        # 只能催促"已接单"状态的订单
        if status != STATUS['ACCEPTED']:
            return jsonify({"error": "只能催促已接单的订单"}), 400
            
        # 检查是否已经过了20分钟
        if accepted_at:
            accepted_time = datetime.strptime(accepted_at, "%Y-%m-%d %H:%M:%S")
            # 将接单时间转换为aware datetime
            if accepted_time.tzinfo is None:
                accepted_time = CN_TIMEZONE.localize(accepted_time)
            
            # 获取当前中国时间
            now = datetime.now(CN_TIMEZONE)
            
            # 如果接单时间不足20分钟，不允许催单
            if now - accepted_time < timedelta(minutes=20):
                return jsonify({"error": "接单未满20分钟，暂不能催单"}), 400
        
        logger.info(f"订单催促: ID={oid}, 用户ID={user_id}")
        
        # 如果有接单人，尝试通过Telegram通知接单人
        if accepted_by:
            logger.info(f"订单 {oid} 有接单人 {accepted_by}，准备发送催单通知。")
            notification_queue.put({
                'type': 'urge',
                'order_id': oid,
                'seller_id': accepted_by,
                'account': account,
                'password': password,
                'package': package,
                'accepted_at': accepted_at
            })
            logger.info(f"已将订单 {oid} 的催单通知任务放入队列。")
            return jsonify({"success": True})
        else:
            return jsonify({"error": "该订单没有接单人信息，无法催单"}), 400

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

    @app.route('/admin/api/users')
    @login_required
    @admin_required
    def admin_api_users():
        """获取所有用户列表（仅限管理员）"""
        users = execute_query("""
            SELECT id, username, is_admin, created_at, last_login, balance, credit_limit 
            FROM users ORDER BY created_at DESC
        """, fetch=True)
        
        return jsonify([{
            "id": user[0],
            "username": user[1],
            "is_admin": bool(user[2]),
            "created_at": user[3],
            "last_login": user[4],
            "balance": user[5] if len(user) > 5 else 0,
            "credit_limit": user[6] if len(user) > 6 else 0
        } for user in users])
    
    @app.route('/admin/api/users/<int:user_id>/balance', methods=['POST'])
    @login_required
    @admin_required
    def admin_update_user_balance(user_id):
        """更新用户余额（仅限管理员）"""
        data = request.json
        
        if not data or 'balance' not in data:
            return jsonify({"error": "缺少余额参数"}), 400
        
        try:
            balance = float(data['balance'])
        except (ValueError, TypeError):
            return jsonify({"error": "余额必须是数字"}), 400
        
        # 不允许设置负余额
        if balance < 0:
            balance = 0
        
        success, new_balance = set_user_balance(user_id, balance)
        
        if success:
            logger.info(f"管理员设置用户ID={user_id}的余额为{new_balance}")
            return jsonify({"success": True, "balance": new_balance})
        else:
            return jsonify({"error": "更新余额失败"}), 500

    @app.route('/admin/api/users/<int:user_id>/credit', methods=['POST'])
    @login_required
    @admin_required
    def admin_update_user_credit(user_id):
        """更新用户透支额度（仅限管理员）"""
        data = request.json
        
        if not data or 'credit_limit' not in data:
            return jsonify({"error": "缺少透支额度参数"}), 400
        
        try:
            credit_limit = float(data['credit_limit'])
        except (ValueError, TypeError):
            return jsonify({"error": "透支额度必须是数字"}), 400
        
        # 不允许设置负透支额度
        if credit_limit < 0:
            credit_limit = 0
        
        success, new_credit_limit = set_user_credit_limit(user_id, credit_limit)
        
        if success:
            logger.info(f"管理员设置用户ID={user_id}的透支额度为{new_credit_limit}")
            return jsonify({"success": True, "credit_limit": new_credit_limit})
        else:
            return jsonify({"error": "更新透支额度失败"}), 500

    @app.route('/admin/api/orders')
    @login_required
    @admin_required
    def admin_api_orders():
        """获取所有订单"""
        # 获取查询参数
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))
        status = request.args.get('status')
        search = request.args.get('search', '')
        
        # 构建查询条件
        conditions = []
        params = []
        
        if status:
            conditions.append("status = ?")
            params.append(status)
        
        if search:
            conditions.append("(account LIKE ? OR web_user_id LIKE ? OR id LIKE ?)")
            search_param = f"%{search}%"
            params.extend([search_param, search_param, search_param])
        
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        
        # 查询订单
        orders = execute_query(f"""
            SELECT id, account, password, package, status, remark, created_at, accepted_at, completed_at, 
                   web_user_id as creator, accepted_by, accepted_by_username, accepted_by_first_name, refunded
            FROM orders
            {where_clause}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """, params + [limit, offset], fetch=True)
        
        # 查询订单总数
        count = execute_query(f"""
            SELECT COUNT(*) FROM orders {where_clause}
        """, params, fetch=True)[0][0]
        
        # 格式化订单数据
        formatted_orders = []
        for order in orders:
            order_id, account, password, package, status, remark, created_at, accepted_at, completed_at, creator, accepted_by, accepted_by_username, accepted_by_first_name, refunded = order
            
            # 格式化卖家信息
            seller_info = None
            if accepted_by:
                seller_info = {
                    "telegram_id": accepted_by,
                    "username": accepted_by_username,
                    "name": accepted_by_first_name
                }
            
            formatted_orders.append({
                "id": order_id,
                "account": account,
                "password": password,
                "package": package,
                "status": status,
                "status_text": STATUS_TEXT_ZH.get(status, status),
                "remark": remark,
                "created_at": created_at,
                "accepted_at": accepted_at,
                "completed_at": completed_at,
                "creator": creator,
                "seller": seller_info,
                "refunded": bool(refunded)
            })
        
        return jsonify({
            "orders": formatted_orders,
            "total": count
        })
        
    @app.route('/admin/api/sellers', methods=['GET'])
    @login_required
    @admin_required
    def admin_api_get_sellers():
        sellers = get_all_sellers()
        return jsonify([{
            "telegram_id": s[0], "username": s[1], "first_name": s[2],
            "is_active": s[3], "added_at": s[4], "added_by": s[5]
        } for s in sellers])

    @app.route('/admin/api/sellers', methods=['POST'])
    @login_required
    @admin_required
    def admin_api_add_seller():
        data = request.json
        telegram_id = data.get('telegram_id')
        if not telegram_id:
            return jsonify({"error": "Telegram ID 不能为空"}), 400
        
        add_seller(
            telegram_id, 
            data.get('username'), 
            data.get('first_name'), 
            session['username']
        )
        return jsonify({"success": True})

    @app.route('/admin/api/sellers/<int:telegram_id>', methods=['DELETE'])
    @login_required
    @admin_required
    def admin_api_remove_seller(telegram_id):
        remove_seller(telegram_id)
        return jsonify({"success": True})

    @app.route('/admin/api/sellers/<int:telegram_id>/toggle', methods=['POST'])
    @login_required
    @admin_required
    def admin_api_toggle_seller(telegram_id):
        toggle_seller_status(telegram_id)
        return jsonify({"success": True})

    # 获取单个订单详情的API
    @app.route('/admin/api/orders/<int:order_id>')
    @login_required
    @admin_required
    def admin_api_order_detail(order_id):
        """获取单个订单的详细信息"""
        order = execute_query("""
            SELECT id, account, password, package, status, remark, created_at, 
                   accepted_at, completed_at, accepted_by, web_user_id, user_id,
                   accepted_by_username, accepted_by_first_name
            FROM orders 
            WHERE id = ?
        """, (order_id,), fetch=True)
        
        if not order:
            return jsonify({"error": "订单不存在"}), 404
            
        o = order[0]
        return jsonify({
            "id": o[0],
            "account": o[1],
            "password": o[2],
            "package": o[3],
            "status": o[4],
            "status_text": STATUS_TEXT_ZH.get(o[4], o[4]),
            "remark": o[5],
            "created_at": o[6],
            "accepted_at": o[7],
            "completed_at": o[8],
            "accepted_by": o[9],
            "web_user_id": o[10],
            "user_id": o[11],
            "accepted_by_username": o[12],
            "accepted_by_first_name": o[13]
        })
    
    # 编辑订单的API
    @app.route('/admin/api/orders/<int:order_id>', methods=['PUT'])
    @login_required
    @admin_required
    def admin_api_edit_order(order_id):
        """管理员编辑订单"""
        data = request.json
        
        # 获取当前订单信息
        order = execute_query("SELECT status, user_id, package, refunded FROM orders WHERE id=?", (order_id,), fetch=True)
        if not order:
            return jsonify({"error": "订单不存在"}), 404
        
        current_status, user_id, current_package, refunded = order[0]
        
        # 获取新状态
        new_status = data.get('status')
        
        # 更新订单信息
        execute_query("""
            UPDATE orders 
            SET account=?, password=?, package=?, status=?, remark=? 
            WHERE id=?
        """, (
            data.get('account'), 
            data.get('password'), 
            data.get('package'), 
            new_status, 
            data.get('remark', ''),
            order_id
        ))
        
        # 处理状态变更的退款逻辑
        if current_status != new_status and new_status in [STATUS['CANCELLED'], STATUS['FAILED']] and not refunded:
            # 订单状态改为已取消或失败，且未退款，执行退款
            refund_order(order_id)
        
        return jsonify({"success": True})

    @app.route('/admin/api/orders/batch-delete', methods=['POST'])
    @login_required
    @admin_required
    def admin_api_batch_delete_orders():
        """管理员批量删除订单"""
        data = request.json
        order_ids = data.get('order_ids')

        if not order_ids or not isinstance(order_ids, list):
            return jsonify({"success": False, "error": "无效的订单ID列表"}), 400

        try:
            # 将ID转换为整数，防止SQL注入
            order_ids_int = [int(oid) for oid in order_ids]
            
            # 创建占位符字符串
            placeholders = ','.join(['?'] * len(order_ids_int))
            
            # 执行删除
            # 注意：execute_query 需要返回受影响的行数，如果它不返回，则此代码需要调整
            # 假设 execute_query 可以返回 cursor 来获取 rowcount
            result = execute_query(
                f"DELETE FROM orders WHERE id IN ({placeholders})",
                order_ids_int,
                fetch=False, # 假设 fetch=False 用于执行非查询语句
                return_cursor=True # 需要一个方法来获取rowCount
            )
            
            deleted_count = result.rowcount if result else 0

            logger.info(f"管理员 {session.get('username')} 删除了 {deleted_count} 个订单: {order_ids}")
            
            return jsonify({"success": True, "deleted_count": deleted_count})
        except (ValueError, TypeError):
            return jsonify({"success": False, "error": "订单ID必须是有效的数字"}), 400
        except Exception as e:
            logger.error(f"批量删除订单时出错: {e}", exc_info=True)
            return jsonify({"success": False, "error": "服务器内部错误"}), 500 

    # ===== 充值相关路由 =====
    @app.route('/recharge', methods=['GET'])
    @login_required
    def recharge_page():
        """显示充值页面"""
        user_id = session.get('user_id')
        balance = get_user_balance(user_id)
        
        # 获取用户的充值记录
        recharge_history = get_user_recharge_requests(user_id)
        
        return render_template('recharge.html',
                              username=session.get('username'),
                              is_admin=session.get('is_admin'),
                              balance=balance,
                              recharge_history=recharge_history)
    
    @app.route('/recharge', methods=['POST'])
    @login_required
    def submit_recharge():
        """提交充值请求"""
        try:
            user_id = session.get('user_id')
            amount = request.form.get('amount')
            payment_method = request.form.get('payment_method')
            payment_command = request.form.get('payment_command', '')
            details = None

            if payment_method == '支付宝口令红包':
                details = payment_command
            
            logger.info(f"收到充值请求: 用户ID={user_id}, 金额={amount}, 支付方式={payment_method}, 详情={details}")
            
            # 验证输入
            try:
                amount = float(amount)
                if amount <= 0:
                    return jsonify({"success": False, "error": "充值金额必须大于0"}), 400
            except ValueError:
                return jsonify({"success": False, "error": "请输入有效的金额"}), 400
            
            if not payment_method:
                payment_method = "未指定"
            
            # 处理上传的支付凭证
            proof_image = None
            if 'proof_image' in request.files:
                file = request.files['proof_image']
                if file and file.filename:
                    try:
                        # 确保上传目录存在
                        current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                        upload_dir = os.path.join(current_dir, 'static', 'uploads')
                        logger.info(f"上传目录路径: {upload_dir}")
                        
                        if not os.path.exists(upload_dir):
                            try:
                                os.makedirs(upload_dir)
                                logger.info(f"创建上传目录: {upload_dir}")
                            except Exception as mkdir_error:
                                logger.error(f"创建上传目录失败: {str(mkdir_error)}", exc_info=True)
                                return jsonify({"success": False, "error": f"创建上传目录失败: {str(mkdir_error)}"}), 500
                        
                        # 生成唯一文件名
                        filename = f"{int(time.time())}_{file.filename}"
                        file_path = os.path.join(upload_dir, filename)
                        
                        # 保存文件
                        file.save(file_path)
                        logger.info(f"已保存文件到: {file_path}")
                        
                        # 确保URL路径正确
                        proof_image = f"/static/uploads/{filename}"
                        logger.info(f"设置凭证URL: {proof_image}")
                        
                        # 验证文件是否成功保存
                        if not os.path.exists(file_path):
                            logger.error(f"文件保存失败，路径不存在: {file_path}")
                            return jsonify({"success": False, "error": "文件保存失败，请重试"}), 500
                    except Exception as e:
                        logger.error(f"保存充值凭证失败: {str(e)}", exc_info=True)
                        return jsonify({"success": False, "error": f"保存充值凭证失败: {str(e)}"}), 500
            
            # 创建充值请求
            logger.info(f"正在创建充值请求: 用户ID={user_id}, 金额={amount}, 支付方式={payment_method}")
            request_id, success, message = create_recharge_request(user_id, amount, payment_method, proof_image, details)
            
            if success:
                # 发送通知到TG管理员
                username = session.get('username')
                notification_queue.put({
                    'type': 'recharge_request',
                    'request_id': request_id,
                    'username': username,
                    'amount': amount,
                    'payment_method': payment_method,
                    'proof_image': proof_image,
                    'details': details
                })
                logger.info(f"充值请求 #{request_id} 已提交成功，已加入通知队列")
                
                return jsonify({
                    "success": True,
                    "message": "充值请求已提交，请等待管理员审核"
                })
            else:
                logger.error(f"创建充值请求失败: {message}")
                return jsonify({"success": False, "error": message}), 500
        except Exception as e:
            logger.error(f"处理充值请求时出错: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": f"处理充值请求时出错: {str(e)}"}), 500
    
    @app.route('/admin/recharge-requests', methods=['GET'])
    @login_required
    @admin_required
    def admin_recharge_requests():
        """管理员查看充值请求列表"""
        pending_requests = get_pending_recharge_requests()
        
        return render_template('admin_recharge.html',
                              username=session.get('username'),
                              is_admin=session.get('is_admin'),
                              pending_requests=pending_requests)
    
    @app.route('/admin/api/recharge/<int:request_id>/approve', methods=['POST'])
    @login_required
    @admin_required
    def approve_recharge(request_id):
        """批准充值请求"""
        admin_id = session.get('user_id')
        
        success, message = approve_recharge_request(request_id, admin_id)
        
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
    
    @app.route('/admin/api/recharge/<int:request_id>/reject', methods=['POST'])
    @login_required
    @admin_required
    def reject_recharge(request_id):
        """拒绝充值请求"""
        admin_id = session.get('user_id')
        
        success, message = reject_recharge_request(request_id, admin_id)
        
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400 