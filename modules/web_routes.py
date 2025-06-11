import os
import time
from datetime import datetime
from functools import wraps

from flask import Flask, request, render_template, jsonify, session, redirect, url_for

from modules.constants import STATUS, STATUS_TEXT_ZH, WEB_PRICES, PLAN_OPTIONS
from modules.database import execute_query, hash_password

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
                
                return redirect(url_for('index'))
            else:
                return render_template('login.html', error='用户名或密码错误')
        
        return render_template('login.html')

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            confirm_password = request.form.get('confirm_password')
            
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
        orders = execute_query("SELECT id, account, package, status, created_at FROM orders ORDER BY id DESC LIMIT 5", fetch=True)
        return render_template('index.html', orders=orders, prices=WEB_PRICES, plan_options=PLAN_OPTIONS)

    @app.route('/', methods=['POST'])
    @login_required
    def create_order():
        account = request.form.get('account')
        password = request.form.get('password')
        package = request.form.get('package', '1')
        
        if not account or not password:
            return render_template('index.html', error='账号和密码不能为空', prices=WEB_PRICES, plan_options=PLAN_OPTIONS)
        
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        execute_query("""
            INSERT INTO orders (account, password, package, status, created_at, web_user_id, user_id) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (account, password, package, STATUS['SUBMITTED'], timestamp, session.get('username'), session.get('user_id')))
        
        orders = execute_query("SELECT id, account, package, status, created_at FROM orders ORDER BY id DESC LIMIT 5", fetch=True)
        return render_template('index.html', orders=orders, success='订单已提交成功！', prices=WEB_PRICES, plan_options=PLAN_OPTIONS)

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
            SELECT id, account, package, status, created_at, accepted_at, completed_at 
            FROM orders 
            {user_filter}
            ORDER BY id DESC LIMIT ? OFFSET ?
        """, params + [limit, offset], fetch=True)
        
        # 格式化数据
        formatted_orders = []
        for order in orders:
            oid, account, package, status, created_at, accepted_at, completed_at = order
            formatted_orders.append({
                "id": oid,
                "account": account,
                "package": package,
                "status": status,
                "status_text": STATUS_TEXT_ZH.get(status, status),
                "created_at": created_at,
                "accepted_at": accepted_at or "",
                "completed_at": completed_at or ""
            })
        
        return jsonify({
            "orders": formatted_orders,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "has_more": len(formatted_orders) == limit
            }
        })

    @app.route('/orders/cancel/<int:oid>', methods=['POST'])
    @login_required
    def cancel_order(oid):
        """取消订单"""
        # 查找订单
        order = execute_query("SELECT status, user_id FROM orders WHERE id = ?", (oid,), fetch=True)
        if not order:
            return jsonify({"error": "订单不存在"}), 404
        
        status, order_user_id = order[0]
        
        # 检查是否可以取消（只有submitted状态的订单可以取消）
        if status != STATUS['SUBMITTED']:
            return jsonify({"error": "只有等待处理的订单可以取消"}), 400
        
        # 检查权限（管理员或订单所有者）
        if not session.get('is_admin') and session.get('user_id') != order_user_id:
            return jsonify({"error": "没有权限取消此订单"}), 403
        
        # 执行取消
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        execute_query("UPDATE orders SET status=?, completed_at=? WHERE id=?",
                     (STATUS['CANCELLED'], timestamp, oid))
        
        return jsonify({"success": True, "message": "订单已取消"}) 