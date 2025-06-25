import os
import time
import logging
import asyncio
import uuid
import base64
from functools import wraps
from datetime import datetime, timedelta
import pytz
import re
from io import BytesIO

from flask import Flask, request, render_template, jsonify, session, redirect, url_for, flash

from modules.constants import STATUS, STATUS_TEXT_ZH, WEB_PRICES, PLAN_OPTIONS, REASON_TEXT_ZH
from modules.database import (
    execute_query, hash_password, get_all_sellers, add_seller, remove_seller, toggle_seller_status,
    get_user_balance, get_user_credit_limit, set_user_balance, set_user_credit_limit, refund_order, 
    create_order_with_deduction_atomic, get_user_recharge_requests, create_recharge_request,
    get_pending_recharge_requests, approve_recharge_request, reject_recharge_request, toggle_seller_admin,
    is_admin_seller, create_order, get_order_details
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

# ===== 管理员装饰器 =====
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'is_admin' not in session or not session['is_admin']:
            flash('您没有管理员权限。', 'error')
            return redirect(url_for('index'))
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
            user = execute_query("SELECT id, username, is_admin FROM users WHERE username=%s AND password_hash=%s",
                            (username, hashed_password), fetch=True)
            
            if user:
                user_id, username, is_admin = user[0]
                session['user_id'] = user_id
                session['username'] = username
                session['is_admin'] = is_admin
                
                # 更新最后登录时间
                execute_query("UPDATE users SET last_login=%s WHERE id=%s",
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
        
        try:
            orders = execute_query(
                """
                SELECT id, qr_code_path, status, created_at, paid_at, confirmed_at 
                FROM orders 
                ORDER BY id DESC LIMIT 10
                """, 
                fetch=True
            )
            
            # 格式化订单数据
            formatted_orders = []
            for order in orders:
                status = order[2]
                status_text = STATUS_TEXT_ZH.get(status, status)
                
                formatted_orders.append({
                    'id': order[0],
                    'qr_code_path': order[1],
                    'status': status,
                    'status_text': status_text,
                    'created_at': order[3],
                    'paid_at': order[4],
                    'confirmed_at': order[5]
                })
            
            return render_template('index.html', 
                                orders=formatted_orders,
                                username=session.get('username'),
                                is_admin=session.get('is_admin'))
        except Exception as e:
            logger.error(f"获取订单失败: {str(e)}", exc_info=True)
            return render_template('index.html', 
                                error='获取订单失败', 
                                username=session.get('username'),
                                is_admin=session.get('is_admin'))

    @app.route('/', methods=['POST'])
    @login_required
    def create_order():
        try:
            # 检查是否上传了文件
            if 'qr_image' in request.files and request.files['qr_image'].filename:
                qr_image = request.files['qr_image']
                
                # 生成唯一文件名
                filename = f"{uuid.uuid4()}.png"
                uploads_dir = os.path.join(app.static_folder, 'uploads')
                if not os.path.exists(uploads_dir):
                    os.makedirs(uploads_dir)
                
                # 保存文件
                file_path = os.path.join(uploads_dir, filename)
                qr_image.save(file_path)
                
                # 存储相对路径
                relative_path = f"/static/uploads/{filename}"
            elif 'qr_base64' in request.form and request.form['qr_base64']:
                # 处理Base64图片数据
                base64_data = request.form['qr_base64']
                
                # 移除可能的Base64前缀
                if ',' in base64_data:
                    base64_data = base64_data.split(',', 1)[1]
                
                # 生成唯一文件名
                filename = f"{uuid.uuid4()}.png"
                uploads_dir = os.path.join(app.static_folder, 'uploads')
                if not os.path.exists(uploads_dir):
                    os.makedirs(uploads_dir)
                
                # 保存Base64解码后的图片
                file_path = os.path.join(uploads_dir, filename)
                with open(file_path, "wb") as f:
                    f.write(base64.b64decode(base64_data))
                
                # 存储相对路径
                relative_path = f"/static/uploads/{filename}"
            else:
                return jsonify({"success": False, "error": "请上传二维码图片"}), 400
            
            # 创建订单
            order_id = create_order(relative_path)
            
            # 向队列添加通知
            notification_data = {
                'type': 'new_order',
                'order_id': order_id,
                'qr_code_path': relative_path
            }
            notification_queue.put(notification_data)
            
            logger.info(f"创建了新订单: ID={order_id}, 图片路径={relative_path}")
            
            return jsonify({
                "success": True,
                "message": "订单已提交成功！",
                "order_id": order_id
            })
            
        except Exception as e:
            logger.error(f"创建订单失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": f"创建订单失败: {str(e)}"}), 500

    @app.route('/orders/<int:order_id>')
    @login_required
    def order_detail(order_id):
        try:
            order = get_order_details(order_id)
            
            if not order:
                flash('订单不存在', 'error')
                return redirect(url_for('index'))
            
            return render_template('order_detail.html', 
                                order=order,
                                status_text=STATUS_TEXT_ZH.get(order['status'], order['status']),
                                username=session.get('username'),
                                is_admin=session.get('is_admin'))
        except Exception as e:
            logger.error(f"获取订单详情失败: {str(e)}", exc_info=True)
            flash(f'获取订单详情失败: {str(e)}', 'error')
            return redirect(url_for('index'))

    @app.route('/admin')
    @login_required
    @admin_required
    def admin_dashboard():
        return render_template('admin.html', 
                            username=session.get('username'),
                            is_admin=session.get('is_admin'))

    @app.route('/admin/sellers', methods=['GET'])
    @login_required
    @admin_required
    def admin_sellers():
        try:
            sellers = get_all_sellers()
            return render_template('admin_sellers.html',
                                sellers=sellers,
                                username=session.get('username'),
                                is_admin=session.get('is_admin'))
        except Exception as e:
            logger.error(f"获取卖家列表失败: {str(e)}", exc_info=True)
            flash(f'获取卖家列表失败: {str(e)}', 'error')
            return redirect(url_for('admin_dashboard'))

    @app.route('/admin/sellers/add', methods=['POST'])
    @login_required
    @admin_required
    def admin_add_seller():
        try:
            telegram_id = request.form.get('telegram_id')
            username = request.form.get('username')
            first_name = request.form.get('first_name')
            
            if not telegram_id or not telegram_id.isdigit():
                flash('Telegram ID必须是数字', 'error')
                return redirect(url_for('admin_sellers'))
            
            if not username:
                username = f"seller_{telegram_id}"
            
            if not first_name:
                first_name = username
            
            added_by = session.get('username', 'admin')
            
            if add_seller(int(telegram_id), username, first_name, added_by):
                flash(f'成功添加卖家: {username}', 'success')
            else:
                flash('添加卖家失败', 'error')
            
            return redirect(url_for('admin_sellers'))
        except Exception as e:
            logger.error(f"添加卖家失败: {str(e)}", exc_info=True)
            flash(f'添加卖家失败: {str(e)}', 'error')
            return redirect(url_for('admin_sellers'))

    @app.route('/admin/sellers/<int:telegram_id>/toggle', methods=['POST'])
    @login_required
    @admin_required
    def admin_toggle_seller(telegram_id):
        try:
            if toggle_seller_status(telegram_id):
                flash('成功切换卖家状态', 'success')
            else:
                flash('切换卖家状态失败', 'error')
            
            return redirect(url_for('admin_sellers'))
        except Exception as e:
            logger.error(f"切换卖家状态失败: {str(e)}", exc_info=True)
            flash(f'切换卖家状态失败: {str(e)}', 'error')
            return redirect(url_for('admin_sellers'))

    @app.route('/admin/sellers/<int:telegram_id>/toggle_admin', methods=['POST'])
    @login_required
    @admin_required
    def admin_toggle_seller_admin(telegram_id):
        try:
            if toggle_seller_admin(telegram_id):
                flash('成功切换卖家管理员状态', 'success')
            else:
                flash('切换卖家管理员状态失败', 'error')
            
            return redirect(url_for('admin_sellers'))
        except Exception as e:
            logger.error(f"切换卖家管理员状态失败: {str(e)}", exc_info=True)
            flash(f'切换卖家管理员状态失败: {str(e)}', 'error')
            return redirect(url_for('admin_sellers'))

    @app.route('/admin/sellers/<int:telegram_id>/remove', methods=['POST'])
    @login_required
    @admin_required
    def admin_remove_seller(telegram_id):
        try:
            if remove_seller(telegram_id):
                flash('成功移除卖家', 'success')
            else:
                flash('移除卖家失败', 'error')
            
            return redirect(url_for('admin_sellers'))
        except Exception as e:
            logger.error(f"移除卖家失败: {str(e)}", exc_info=True)
            flash(f'移除卖家失败: {str(e)}', 'error')
            return redirect(url_for('admin_sellers'))

    @app.route('/orders/status')
    @login_required
    def check_order_status():
        try:
            order_id = request.args.get('order_id')
            
            if not order_id or not order_id.isdigit():
                return jsonify({"success": False, "error": "无效的订单ID"}), 400
            
            order = get_order_details(int(order_id))
            
            if not order:
                return jsonify({"success": False, "error": "订单不存在"}), 404
            
            return jsonify({
                "success": True,
                "order": {
                    "id": order["id"],
                    "status": order["status"],
                    "status_text": STATUS_TEXT_ZH.get(order["status"], order["status"]),
                    "created_at": order["created_at"],
                    "paid_at": order["paid_at"],
                    "confirmed_at": order["confirmed_at"],
                    "feedback": order["feedback"]
                }
            })
        except Exception as e:
            logger.error(f"检查订单状态失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": f"检查订单状态失败: {str(e)}"}), 500

    @app.route('/orders/recent')
    @login_required
    def get_recent_orders():
        try:
            limit = request.args.get('limit', 5, type=int)
            
            orders = execute_query(
                """
                SELECT id, qr_code_path, status, created_at, paid_at, confirmed_at 
                FROM orders 
                ORDER BY id DESC LIMIT %s
                """, 
                (limit,),
                fetch=True
            )
            
            # 格式化订单数据
            formatted_orders = []
            for order in orders:
                status = order[2]
                status_text = STATUS_TEXT_ZH.get(status, status)
                
                formatted_orders.append({
                    'id': order[0],
                    'qr_code_path': order[1],
                    'status': status,
                    'status_text': status_text,
                    'created_at': order[3],
                    'paid_at': order[4],
                    'confirmed_at': order[5]
                })
            
            return jsonify({
                "success": True,
                "orders": formatted_orders
            })
        except Exception as e:
            logger.error(f"获取最近订单失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": f"获取最近订单失败: {str(e)}"}), 500

    # 返回所有注册的路由
    return app 