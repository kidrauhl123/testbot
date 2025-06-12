import os
import time
import logging
from datetime import datetime
from functools import wraps
import asyncio

from flask import Flask, request, render_template, jsonify, session, redirect, url_for

from modules.constants import STATUS, STATUS_TEXT_ZH, WEB_PRICES, PLAN_OPTIONS, REASON_TEXT_ZH
from modules.database import execute_query, hash_password, get_all_sellers, add_seller, remove_seller, toggle_seller_status
from modules.database import check_balance_for_package, update_user_balance, get_user_balance, set_user_balance
from modules.database import get_user_credit_limit, set_user_credit_limit, refund_order
from modules.telegram_bot import bot_application, check_and_push_orders
import modules.constants as constants

# 设置日志
logger = logging.getLogger(__name__)

# ===== 登录装饰器 =====
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ===== Web路由 =====
def register_routes(app):
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
                            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id))
                
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
        # 记录请求内容
        logger.info("收到POST请求到根路径")
        logger.info(f"请求表单数据: {request.form}")
        logger.info(f"请求头: {request.headers}")
        
        account = request.form.get('account')
        password = request.form.get('password')
        package = request.form.get('package', '1')
        remark = request.form.get('remark', '')
        
        logger.info(f"收到订单提交请求: 账号={account}, 套餐={package}")
        
        if not account or not password:
            logger.warning("订单提交失败: 账号或密码为空")
            return jsonify({
                "success": False,
                "error": "账号和密码不能为空"
            }), 400
        
        try:
            # 获取当前用户信息
            user_id = session.get('user_id')
            username = session.get('username')
            
            logger.info(f"当前会话信息: user_id={user_id}, username={username}")
            
            # 检查用户余额是否足够
            sufficient, balance, price, credit_limit = check_balance_for_package(user_id, package)
            
            if not sufficient:
                logger.warning(f"订单提交失败: 用户余额不足 (用户={username}, 余额={balance}, 透支额度={credit_limit}, 价格={price})")
                return jsonify({
                    "success": False,
                    "error": f'余额和透支额度不足，当前余额: {balance}，透支额度: {credit_limit}，套餐价格: {price}',
                    "balance": balance,
                    "credit_limit": credit_limit
                }), 400
            
            # 记录当前时间
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            
            logger.debug(f"准备插入订单: 用户={username}, 时间={timestamp}")
            
            # 插入订单
            execute_query("""
                INSERT INTO orders (account, password, package, remark, status, created_at, web_user_id, user_id, notified, refunded) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (account, password, package, remark, STATUS['SUBMITTED'], timestamp, username, user_id, 0, 0))
            
            # 扣除用户余额
            success, new_balance = update_user_balance(user_id, -price)
            if not success:
                logger.error(f"余额扣除失败: 用户={username}, 金额={price}")
                return jsonify({
                    "success": False,
                    "error": f'扣款失败，订单未提交，请联系管理员',
                    "balance": balance,
                    "credit_limit": credit_limit
                }), 400
            else:
                logger.info(f"余额扣除成功: 用户={username}, 金额={price}, 新余额={new_balance}")
            
            logger.info(f"订单提交成功: 用户={username}, 套餐={package}")
            
            # 获取最新订单列表
            orders = execute_query("SELECT id, account, package, status, created_at FROM orders ORDER BY id DESC LIMIT 5", fetch=True)
            logger.info(f"查询到的最新订单: {orders}")
            
            # 计算是否使用了透支额度
            used_credit = 0
            if balance < price:
                # 如果原始余额小于价格，说明使用了透支额度
                if balance >= 0:
                    # 余额为正数，使用的透支额度 = 价格 - 余额
                    used_credit = price - balance
                else:
                    # 余额为负数，使用的透支额度 = 价格
                    used_credit = price
            
            success_message = '订单已提交成功！'
            if used_credit > 0:
                success_message = f'订单已提交成功！已使用透支额度 {used_credit} 元'
            
            return jsonify({
                "success": True,
                "message": success_message,
                "balance": new_balance,
                "credit_limit": credit_limit,
                "orders": orders,
                "used_credit": used_credit
            })
        except Exception as e:
            logger.error(f"创建订单失败: {str(e)}", exc_info=True)
            return jsonify({
                "success": False,
                "error": f'订单提交失败: {str(e)}'
            }), 500

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
            SELECT id, account, package, status, created_at, accepted_at, completed_at,
                   remark, web_user_id, user_id, accepted_by, accepted_by_username, accepted_by_first_name
            FROM orders 
            {user_filter}
            ORDER BY id DESC LIMIT ? OFFSET ?
        """, params + [limit, offset], fetch=True)
        
        logger.info(f"查询到 {len(orders)} 条订单记录")
        
        # 格式化数据
        formatted_orders = []
        for order in orders:
            oid, account, package, status, created_at, accepted_at, completed_at, remark, web_user_id, user_id, accepted_by, accepted_by_username, accepted_by_first_name = order
            
            # 优先使用昵称，其次是用户名，最后是ID
            seller_display = accepted_by_first_name or accepted_by_username or accepted_by
            
            # 如果是失败状态，翻译失败原因
            translated_remark = remark
            if status == STATUS['FAILED'] and remark:
                translated_remark = REASON_TEXT_ZH.get(remark, remark)
            
            order_data = {
                "id": oid,
                "account": account,
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

    # 添加一个测试路由
    @app.route('/test')
    def test_route():
        logger.info("访问测试路由")
        return jsonify({
            'status': 'ok',
            'message': '服务器正常运行',
            'time': time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    # 添加一个路由用于手动触发订单检查
    @app.route('/check-orders')
    def manual_check_orders():
        logger.info("手动触发订单检查")
        
        try:
            # 导入asyncio和机器人实例
            from modules.telegram_bot import bot_application
            
            # 检查机器人实例
            if bot_application is None:
                return jsonify({
                    'status': 'error',
                    'message': 'Telegram机器人实例未初始化'
                })
            
            # 创建事件循环并执行订单检查
            asyncio.run(check_and_push_orders())
            
            return jsonify({
                'status': 'ok',
                'message': '订单检查已触发',
                'time': time.strftime("%Y-%m-%d %H:%M:%S")
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
        orders = execute_query("""
            SELECT o.id, o.account, o.package, o.status, u.username, o.accepted_by_first_name, o.created_at, o.remark
            FROM orders o
            LEFT JOIN users u ON o.user_id = u.id
            ORDER BY o.id DESC
        """, fetch=True)

        formatted_orders = []
        for row in orders:
            # 如果是失败状态，翻译失败原因
            remark = row[7] or ""
            if row[3] == STATUS['FAILED'] and remark:
                translated_remark = REASON_TEXT_ZH.get(remark, remark)
            else:
                translated_remark = remark
                
            formatted_orders.append({
                "id": row[0],
                "account": row[1],
                "package": row[2],
                "status": row[3],
                "status_text": STATUS_TEXT_ZH.get(row[3], row[3]),
                "creator": row[4],
                "accepted_by": row[5],
                "created_at": row[6],
                "remark": translated_remark
            })
        return jsonify(formatted_orders)
        
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
                   accepted_by, web_user_id, user_id, accepted_at, completed_at
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
            "remark": o[5],
            "created_at": o[6],
            "accepted_by": o[7],
            "web_user_id": o[8],
            "user_id": o[9],
            "accepted_at": o[10],
            "completed_at": o[11]
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