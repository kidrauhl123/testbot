import os
import time
import logging
import asyncio
from functools import wraps
from datetime import datetime, timedelta
import pytz
import sqlite3
import json
import random
import string
import psycopg2
from urllib.parse import urlparse

from flask import Flask, request, render_template, jsonify, session, redirect, url_for, flash, send_file, send_from_directory

from modules.constants import STATUS, STATUS_TEXT_ZH, REASON_TEXT_ZH, DATABASE_URL, CONFIRM_STATUS, CONFIRM_STATUS_TEXT_ZH
from modules.database import (
    execute_query, hash_password, get_unnotified_orders,
    get_order_details, get_all_sellers, get_active_sellers, toggle_seller_status, 
    remove_seller, toggle_seller_admin,
    update_seller_nickname, select_active_seller, check_seller_activity,
    get_seller_completed_orders, get_seller_pending_orders, check_seller_completed_orders,
    get_seller_today_confirmed_orders_by_user, get_admin_sellers,
    get_user_today_confirmed_count, get_all_today_confirmed_count, create_order_with_deduction_atomic,
    add_seller, check_all_sellers_full, delete_old_orders, get_today_valid_orders_count
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
            
            return render_template('index.html', 
                                   orders=orders, 
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
        logger.info("处理图片上传请求")
        
        # 优先检查正常文件上传
        if 'qr_code' in request.files and request.files['qr_code'].filename != '':
            qr_code = request.files['qr_code']
            logger.info(f"接收到文件上传: {qr_code.filename}")
        # 然后检查base64数据（来自粘贴或拖放）
        elif 'qr_code_base64' in request.form and request.form['qr_code_base64']:
            # 处理Base64图片数据
            try:
                import base64
                from io import BytesIO
                from werkzeug.datastructures import FileStorage
                
                # 解析Base64数据
                base64_data = request.form['qr_code_base64'].split(',')[1] if ',' in request.form['qr_code_base64'] else request.form['qr_code_base64']
                image_data = base64.b64decode(base64_data)
                
                # 创建文件对象
                qr_code = FileStorage(
                    stream=BytesIO(image_data),
                    filename='pasted_image.png',
                    content_type='image/png',
                )
                logger.info("成功从Base64数据创建文件对象")
            except Exception as e:
                logger.error(f"处理Base64图片数据失败: {str(e)}")
                return jsonify({"success": False, "error": f"处理粘贴的图片失败: {str(e)}"}), 400
        else:
            logger.warning("订单提交失败: 未上传二维码图片")
            return jsonify({"success": False, "error": "请上传油管二维码图片"}), 400
            
        # 保存二维码图片
        import uuid, os
        from datetime import datetime
        import imghdr  # 用于验证图片格式
        
        # 检查是否是有效的图片文件
        try:
            # 先保存到临时文件
            temp_path = os.path.join('static', 'temp_upload.png')
            qr_code.save(temp_path)
            
            # 验证文件是否为图片
            img_type = imghdr.what(temp_path)
            if not img_type:
                os.remove(temp_path)  # 清理临时文件
                logger.warning("订单提交失败: 上传的文件不是有效的图片")
                return jsonify({"success": False, "error": "请上传有效的图片文件"}), 400
                
            # 生成唯一文件名
            file_ext = f".{img_type}" if img_type else ".png"
            unique_filename = f"{uuid.uuid4().hex}{file_ext}"
            timestamp = datetime.now().strftime("%Y%m%d")
            save_path = os.path.join('static', 'uploads', timestamp)
            
            # 确保保存目录存在
            if not os.path.exists(save_path):
                os.makedirs(save_path, exist_ok=True)
                # 确保目录权限正确
                os.chmod(save_path, 0o755)
                
            file_path = os.path.join(save_path, unique_filename)
            
            # 直接复制文件而不是移动
            import shutil
            shutil.copy2(temp_path, file_path)
            
            # 确保图片权限正确
            os.chmod(file_path, 0o644)
            
            # 验证文件是否成功保存
            if not os.path.exists(file_path):
                logger.error(f"图片保存失败，目标文件不存在: {file_path}")
                return jsonify({"success": False, "error": "图片保存失败，请重试"}), 500
                
            file_size = os.path.getsize(file_path)
            if file_size == 0:
                logger.error(f"图片保存失败，文件大小为0: {file_path}")
                os.remove(file_path)  # 删除空文件
                return jsonify({"success": False, "error": "图片保存失败，文件大小为0"}), 500
                
            logger.info(f"图片成功保存到: {file_path}，大小: {file_size} 字节")
            
        except Exception as e:
            logger.error(f"保存图片时出错: {str(e)}")
            return jsonify({"success": False, "error": f"保存图片时出错: {str(e)}"}), 500
        
        # 使用文件路径作为账号，密码设为空(因为现在依靠二维码)
        account = file_path
        password = ""
        package = request.form.get('package', '12')  # 默认为12个月
        remark = request.form.get('remark', '')
        
        # 获取指定的接单人
        preferred_seller = request.form.get('preferred_seller', '')
        if preferred_seller:
            # 检查该卖家是否已有3个未确认订单
            unconfirmed_orders_query = """
                SELECT COUNT(*) FROM orders 
                WHERE accepted_by = ? 
                AND status = ? 
                AND completed_at IS NULL
            """
            unconfirmed_count = execute_query(
                unconfirmed_orders_query, 
                (preferred_seller, STATUS['ACCEPTED']), 
                fetch=True
            )[0][0]
            
            if unconfirmed_count >= 3:
                logger.warning(f"订单提交失败: 卖家 {preferred_seller} 已有 {unconfirmed_count} 个未确认订单")
                return jsonify({
                    "success": False, 
                    "error": "该卖家已有3个未确认订单，请选择其他卖家或等待卖家完成现有订单"
                }), 400
            
            # 记录日志但不再修改备注
            logger.info(f"用户指定接单人: {preferred_seller}")
        
        logger.info(f"收到订单提交请求: 二维码={file_path}, 套餐={package}, 指定接单人={preferred_seller or '无'}")
        
        if not account:
            logger.warning("订单提交失败: 二维码保存失败")
            return jsonify({"success": False, "error": "二维码保存失败，请重试"}), 400
        
        try:
            user_id = session.get('user_id')
            username = session.get('username')
            
            # 检查是否所有卖家都已达到最大接单量
            if check_all_sellers_full():
                logger.warning("订单提交失败: 所有卖家都已达到最大接单量")
                return jsonify({
                    "success": False,
                    "error": "当前所有卖家已达到最大接单量，请稍后再试"
                }), 400
            
            # 创建订单
            try:
                result = create_order_with_deduction_atomic(account, password, package, remark, username, user_id)
                if isinstance(result, tuple):
                    success, message = result[0], result[1]
                else:
                    success, message = result, ''
            except Exception as e:
                success = False
                message = str(e)
            
            if not success:
                logger.warning(f"订单创建失败: {message} (用户={username})")
                return jsonify({
                    "success": False,
                    "error": message
                }), 400

            logger.info(f"订单提交成功: 用户={username}, 套餐={package}")
            
            # 获取最新订单列表并格式化
            orders_raw = execute_query("SELECT id, account, password, package, status, created_at FROM orders ORDER BY id DESC LIMIT 5", fetch=True)
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
                    "can_cancel": o[4] == STATUS['SUBMITTED'] and (session.get('is_admin') or session.get('user_id') == o[0])
                })
            
            # 触发立即通知卖家 - 获取新创建的订单ID并加入通知队列
            if new_order_id:
                # 加入通知队列，通知类型为new_order
                # 获取指定的接单人
                preferred_seller = request.form.get('preferred_seller', '')
                # 直接使用相对路径
                logger.info(f"添加到通知队列的图片路径: {file_path}")
                
                notification_queue.put({
                    'type': 'new_order',
                    'order_id': new_order_id,
                    'account': file_path,  # 使用相对路径
                    'password': '',  # 不再使用密码
                    'package': package,
                    'preferred_seller': preferred_seller,
                    'remark': remark,  # 添加备注信息
                    'creator': username  # 添加创建者用户名
                })
                logger.info(f"已将订单 #{new_order_id} 加入通知队列")
                print(f"DEBUG: 已将订单 #{new_order_id} 加入通知队列")
            else:
                logger.warning("无法获取新创建的订单ID，无法发送通知")
                print("WARNING: 无法获取新创建的订单ID，无法发送通知")
            
            return jsonify({
                "success": True,
                "message": '订单已提交成功！'
            })
            
        except Exception as e:
            logger.error(f"创建订单时出错: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": f"创建订单时出错: {str(e)}"}), 500

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
        """获取最近订单的API接口"""
        try:
            # 获取参数
            limit = int(request.args.get('limit', 20))
            offset = int(request.args.get('offset', 0))
            
            # 限制最大获取数量，但允许设置更大的值以支持加载所有订单
            limit = min(limit, 1500)
            
            # 根据用户权限，决定查询所有订单还是仅当前用户的订单
            is_admin = session.get('is_admin')
            user_id = session.get('user_id')
            
            # 格式化订单数据
            formatted_orders = []
            
            # 根据数据库类型使用不同的查询
            if DATABASE_URL.startswith('postgres'):
                if is_admin:
                    # 管理员可查看所有订单
                    orders = execute_query("""
                        SELECT id, account, password, package, status, created_at, 
                        accepted_at, completed_at, remark, web_user_id, user_id, 
                        accepted_by, accepted_by_username, accepted_by_first_name, accepted_by_nickname, buyer_confirmed
                        FROM orders 
                        ORDER BY id DESC LIMIT ? OFFSET ?
                    """, (limit, offset), fetch=True)
                else:
                    # 普通用户只能查看自己的订单
                    orders = execute_query("""
                        SELECT id, account, password, package, status, created_at, 
                        accepted_at, completed_at, remark, web_user_id, user_id, 
                        accepted_by, accepted_by_username, accepted_by_first_name, accepted_by_nickname, buyer_confirmed
                        FROM orders 
                        WHERE user_id = ? 
                        ORDER BY id DESC LIMIT ? OFFSET ?
                    """, (user_id, limit, offset), fetch=True)
                
                for order in orders:
                    oid, account, password, package, status, created_at, accepted_at, completed_at, remark, web_user_id, user_id, accepted_by, accepted_by_username, accepted_by_first_name, accepted_by_nickname, buyer_confirmed = order
                    
                    # 优先使用自定义昵称，其次是用户名称，最后是ID
                    seller_display = accepted_by_nickname or accepted_by_first_name or accepted_by_username or accepted_by
                    if seller_display and not isinstance(seller_display, str):
                        seller_display = str(seller_display)
                    
                    # 如果是失败状态，翻译失败原因
                    translated_remark = remark
                    if status == STATUS['FAILED'] and remark:
                        translated_remark = REASON_TEXT_ZH.get(remark, remark)
                    
                    # 使用web_user_id作为username
                    username = web_user_id
                    
                    # 获取确认状态，如果没有则根据buyer_confirmed设置默认值
                    confirm_status = execute_query(
                        "SELECT confirm_status FROM orders WHERE id=?", 
                        (oid,), 
                        fetch=True
                    )
                    
                    # 如果confirm_status存在，使用它；否则根据buyer_confirmed设置默认值
                    if confirm_status and confirm_status[0][0]:
                        confirm_status = confirm_status[0][0]
                    else:
                        confirm_status = CONFIRM_STATUS['CONFIRMED'] if buyer_confirmed else CONFIRM_STATUS['PENDING']
                    
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
                        "accepted_by": seller_display or "",
                        "buyer_confirmed": bool(buyer_confirmed),
                        "confirm_status": confirm_status,
                        "can_cancel": status == STATUS['SUBMITTED'] and (session.get('is_admin') or session.get('user_id') == user_id),
                        "username": username or ""
                    }
                    formatted_orders.append(order_data)
            else:
                if is_admin:
                    # 管理员可查看所有订单
                    orders = execute_query("""
                        SELECT id, account, password, package, status, created_at, 
                        accepted_at, completed_at, remark, web_user_id, user_id, 
                        accepted_by, accepted_by_username, accepted_by_first_name, accepted_by_nickname, buyer_confirmed,
                        username
                        FROM orders 
                        ORDER BY id DESC LIMIT ? OFFSET ?
                    """, (limit, offset), fetch=True)
                else:
                    # 普通用户只能查看自己的订单
                    orders = execute_query("""
                        SELECT id, account, password, package, status, created_at, 
                        accepted_at, completed_at, remark, web_user_id, user_id, 
                        accepted_by, accepted_by_username, accepted_by_first_name, accepted_by_nickname, buyer_confirmed,
                        username
                        FROM orders 
                        WHERE user_id = ? 
                        ORDER BY id DESC LIMIT ? OFFSET ?
                    """, (user_id, limit, offset), fetch=True)
                
                for order in orders:
                    oid, account, password, package, status, created_at, accepted_at, completed_at, remark, web_user_id, user_id, accepted_by, accepted_by_username, accepted_by_first_name, accepted_by_nickname, buyer_confirmed, username = order
                    
                    # 优先使用自定义昵称，其次是用户名称，最后是ID
                    seller_display = accepted_by_nickname or accepted_by_first_name or accepted_by_username or accepted_by
                    if seller_display and not isinstance(seller_display, str):
                        seller_display = str(seller_display)
                    
                    # 如果是失败状态，翻译失败原因
                    translated_remark = remark
                    if status == STATUS['FAILED'] and remark:
                        translated_remark = REASON_TEXT_ZH.get(remark, remark)
                    
                    # 获取确认状态，如果没有则根据buyer_confirmed设置默认值
                    confirm_status = execute_query(
                        "SELECT confirm_status FROM orders WHERE id=?", 
                        (oid,), 
                        fetch=True
                    )
                    
                    # 如果confirm_status存在，使用它；否则根据buyer_confirmed设置默认值
                    if confirm_status and confirm_status[0][0]:
                        confirm_status = confirm_status[0][0]
                    else:
                        confirm_status = CONFIRM_STATUS['CONFIRMED'] if buyer_confirmed else CONFIRM_STATUS['PENDING']
                    
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
                        "accepted_by": seller_display or "",
                        "buyer_confirmed": bool(buyer_confirmed),
                        "confirm_status": confirm_status,
                        "can_cancel": status == STATUS['SUBMITTED'] and (session.get('is_admin') or session.get('user_id') == user_id),
                        "username": username or web_user_id or ""
                    }
                    formatted_orders.append(order_data)
            
            # 直接返回订单列表，而不是嵌套在orders字段中
            return jsonify({"success": True, "orders": formatted_orders})
        except Exception as e:
            logger.error(f"获取最近订单失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": "服务器内部错误"}), 500

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
        """催促已接单但未完成的订单（功能已下线）"""
        return jsonify({"error": "催单功能已下线"}), 404
        # 以下旧代码保留以防日后恢复
        # user_id = session.get('user_id')

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
        
        return render_template('dashboard.html', 
                              username=username, 
                              is_admin=is_admin)

    @app.route('/admin/api/users')
    @login_required
    @admin_required
    def admin_api_users():
        """获取所有用户列表（仅限管理员）"""
        # 获取所有用户基础信息
        users = execute_query("""
            SELECT id, username, is_admin, created_at, last_login, balance, credit_limit 
            FROM users ORDER BY created_at DESC
        """, fetch=True)
        
        # 获取今日日期
        today = datetime.now().strftime("%Y-%m-%d") + "%"
        
        # 为每个用户计算今日消费
        user_data = []
        for user in users:
            user_id = user[0]
            username = user[1]
            
            # 查询该用户今日已完成订单的消费总额
            today_orders = execute_query("""
                SELECT package FROM orders 
                WHERE web_user_id = ? AND created_at LIKE ? AND status = 'completed'
            """, (username, today), fetch=True)
            
            # 计算总消费额（功能已移除，设置为0）
            today_consumption = 0
            
            user_data.append({
                "id": user_id,
                "username": username,
                "is_admin": bool(user[2]),
                "created_at": user[3],
                "last_login": user[4],
                "balance": user[5] if len(user) > 5 else 0,
                "credit_limit": user[6] if len(user) > 6 else 0,
                "today_consumption": today_consumption
            })
        
        return jsonify(user_data)
    
    # 删除余额更新API
    @app.route('/admin/api/users/<int:user_id>/balance', methods=['POST'])
    @login_required
    @admin_required
    def admin_update_user_balance(user_id):
        """设置用户余额（为保持兼容性而添加）"""
        return jsonify({
            "success": True
        })

    # 删除透支额度更新API
    @app.route('/admin/api/users/<int:user_id>/credit', methods=['POST'])
    @login_required
    @admin_required
    def admin_update_user_credit(user_id):
        """设置用户透支额度（为保持兼容性而添加）"""
        return jsonify({
            "success": True
        })
            
    # 删除用户定制价格获取API
            
    # 删除用户定制价格设置API

    @app.route('/admin/api/orders')
    @login_required
    @admin_required
    def admin_api_orders():
        """获取所有订单列表，支持分页"""
        # 获取查询参数
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 50))  # 默认每页50条
        offset = (page - 1) * per_page
        limit = per_page
        
        status = request.args.get('status')
        search = request.args.get('search', '')
        seller_id = request.args.get('seller_id')  # 按接单人筛选
        
        # 构建查询条件
        conditions = []
        params = []
        
        if status:
            conditions.append("status = ?")
            params.append(status)
        
        if search:
            conditions.append("(account LIKE ? OR web_user_id LIKE ? OR id::text LIKE ? OR remark LIKE ?)")
            search_param = f"%{search}%"
            params.extend([search_param, search_param, search_param, search_param])
            
        if seller_id:
            conditions.append("accepted_by = ?")
            params.append(seller_id)
        
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        
        # 查询订单总数(先查总数以提高分页性能)
        count_query = f"SELECT COUNT(*) FROM orders{where_clause}"
        if DATABASE_URL.startswith('postgres'):
            count_query = count_query.replace('?', '%s')
        count = execute_query(count_query, params, fetch=True)[0][0]
        
        # 查询订单
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL查询
            query_params = params + [limit, offset]
            orders = execute_query(f"""
                SELECT o.id, o.account, o.password, o.package, o.status, o.remark, o.created_at, o.accepted_at, o.completed_at, 
                       COALESCE(o.web_user_id, u.username) as creator, o.accepted_by, o.accepted_by_username, o.accepted_by_first_name, o.accepted_by_nickname, o.refunded, o.buyer_confirmed,
                       o.user_id
                FROM orders o
                LEFT JOIN users u ON o.user_id = u.id
                {where_clause}
                ORDER BY o.id DESC
                LIMIT %s OFFSET %s
            """, query_params, fetch=True)
        else:
            # SQLite查询
            query_params = params + [limit, offset]
            orders = execute_query(f"""
                SELECT id, account, password, package, status, remark, created_at, accepted_at, completed_at, 
                       web_user_id as creator, accepted_by, accepted_by_username, accepted_by_first_name, accepted_by_nickname, refunded, buyer_confirmed,
                       user_id
                FROM orders
                {where_clause}
                ORDER BY id DESC
                LIMIT ? OFFSET ?
            """, query_params, fetch=True)
        
        # 格式化订单数据
        formatted_orders = []
        for order in orders:
            order_id, account, password, package, status, remark, created_at, accepted_at, completed_at, creator, accepted_by, accepted_by_username, accepted_by_first_name, accepted_by_nickname, refunded, buyer_confirmed, user_id = order
            
            # 格式化卖家信息
            seller_info = None
            if accepted_by:
                # 优先使用自定义昵称，其次是TG名称，最后是ID
                display_name = accepted_by_nickname or accepted_by_first_name or accepted_by_username or str(accepted_by)
                seller_info = {
                    "telegram_id": accepted_by,
                    "username": accepted_by_username or str(accepted_by),
                    "name": display_name
                }
            
            # 获取确认状态
            confirm_status_result = execute_query(
                "SELECT confirm_status FROM orders WHERE id=?", 
                (order_id,), 
                fetch=True
            )
            
            # 如果confirm_status存在，使用它；否则根据buyer_confirmed设置默认值
            if confirm_status_result and confirm_status_result[0][0]:
                confirm_status = confirm_status_result[0][0]
            else:
                confirm_status = CONFIRM_STATUS['CONFIRMED'] if buyer_confirmed else CONFIRM_STATUS['PENDING']
            
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
                "creator": creator or "N/A",
                "created_by": creator or "N/A",  # 保持前端兼容
                "user_id": user_id,
                "seller": seller_info,
                "accepted_by": accepted_by,
                "accepted_by_nickname": accepted_by_nickname,
                "refunded": bool(refunded),
                "buyer_confirmed": bool(buyer_confirmed),
                "confirm_status": confirm_status
            })
        
        # 计算是否有更多数据
        has_more = (page * per_page) < count
        
        return jsonify({
            "orders": formatted_orders,
            "total": count,
            "page": page,
            "per_page": per_page,
            "has_more": has_more,
            "total_pages": (count + per_page - 1) // per_page  # 向上取整
        })
        
    @app.route('/admin/api/sellers', methods=['GET'])
    @login_required
    @admin_required
    def admin_api_get_sellers():
        """获取所有卖家列表"""
        sellers = get_all_sellers()
        
        # 增强卖家信息
        enhanced_sellers = []
        for s in sellers:
            # 转换为字典
            seller = {
                "telegram_id": s[0],
                "username": s[1],
                "first_name": s[2],
                "nickname": s[3],
                "is_active": bool(s[4]),
                "added_at": s[5],
                "added_by": s[6],
                "is_admin": bool(s[7]),
                "distribution_level": s[8] if len(s) > 8 and s[8] is not None else 1,
                "max_concurrent_orders": s[9] if len(s) > 9 and s[9] is not None else 5
            }
            
            # 获取已完成订单数和未完成订单数
            telegram_id = seller["telegram_id"]
            completed_orders = get_seller_completed_orders(str(telegram_id))
            pending_orders = get_seller_pending_orders(str(telegram_id))
            
            # 添加到卖家信息中
            seller['completed_orders'] = completed_orders
            seller['pending_orders'] = pending_orders
            
            enhanced_sellers.append(seller)
        
        return jsonify(enhanced_sellers)

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
            data.get('nickname'),
            session.get('username')
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

    @app.route('/admin/api/sellers/toggle_admin', methods=['POST'])
    @login_required
    @admin_required
    def admin_api_toggle_seller_admin():
        """切换卖家的管理员身份"""
        data = request.get_json()
        telegram_id = data.get('telegram_id')
        
        if not telegram_id:
            return jsonify({"error": "Missing telegram_id"}), 400
            
        # 切换 is_admin 状态
        if DATABASE_URL.startswith('postgres'):
            # 对于PostgreSQL，使用is_admin IS NOT TRUE来判断非管理员
            new_status_query = "UPDATE sellers SET is_admin = (is_admin IS NOT TRUE) WHERE telegram_id = %s RETURNING is_admin"
            params = (telegram_id,)
        else:
            # 对于SQLite
            new_status_query = "UPDATE sellers SET is_admin = (1 - is_admin) WHERE telegram_id = ?"
            params = (telegram_id,)

        # 执行更新
        result = execute_query(new_status_query, params, fetch=True)
        
        if not result:
            return jsonify({"error": "Failed to toggle admin status or user not found"}), 404

        new_status = result[0][0]
        action = "promoted to admin" if new_status else "demoted to regular seller"
        logger.info(f"Successfully {action} for seller {telegram_id}")
        return jsonify({"success": True, "is_admin": new_status})

    # 获取单个订单详情的API
    @app.route('/admin/api/orders/<int:order_id>')
    @login_required
    @admin_required
    def admin_api_order_detail(order_id):
        """获取单个订单的详细信息"""
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL查询，使用联合查询获取用户名
            order = execute_query("""
                SELECT o.id, o.account, o.password, o.package, o.status, o.remark, o.created_at, 
                       o.accepted_at, o.completed_at, o.accepted_by, o.web_user_id, o.user_id,
                       o.accepted_by_username, o.accepted_by_first_name, o.accepted_by_nickname, u.username as creator_name
                FROM orders o
                LEFT JOIN users u ON o.user_id = u.id
                WHERE o.id = %s
            """, (order_id,), fetch=True)
        else:
            # SQLite查询
            order = execute_query("""
                SELECT id, account, password, package, status, remark, created_at, 
                       accepted_at, completed_at, accepted_by, web_user_id, user_id,
                       accepted_by_username, accepted_by_first_name, accepted_by_nickname
                FROM orders 
                WHERE id = ?
            """, (order_id,), fetch=True)
        
        if not order:
            return jsonify({"error": "订单不存在"}), 404
            
        o = order[0]
        
        # 根据不同数据库处理返回格式，优先使用卖家昵称
        if DATABASE_URL.startswith('postgres'):
            # 如果是PostgreSQL，o[14]是creator_name，其他字段索引不变
            seller_name = o[14] or o[13] or o[12] or o[9]  # 优先使用昵称，然后first_name，然后username，最后用id
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
                "accepted_by_name": seller_name,
                "creator": o[10] or o[15] or "未知"  # web_user_id或creator_name
            })
        else:
            # SQLite情况
            seller_name = o[14] or o[13] or o[12] or o[9]  # 优先使用昵称，然后first_name，然后username，最后用id
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
                "accepted_by_name": seller_name,
                "creator": o[10] or "未知"  # web_user_id
            })
    
    # 编辑订单的API
    @app.route('/admin/api/orders/<int:order_id>', methods=['PUT'])
    @login_required
    @admin_required
    def admin_api_edit_order(order_id):
        """管理员编辑订单"""
        data = request.json
        
        # 获取当前订单信息
        order = execute_query("SELECT status, user_id, package, refunded, password, remark FROM orders WHERE id=?", (order_id,), fetch=True)
        if not order:
            return jsonify({"error": "订单不存在"}), 404
        
        current_status, user_id, current_package, refunded, current_password, current_remark = order[0]
        
        # 获取新状态
        new_status = data.get('status')
        
        # 确保package字段有值
        package = data.get('package')
        if not package:
            package = current_package  # 如果前端没有提供package，则使用当前值
            
        # 确保密码字段有值
        password = data.get('password')
        if password is None:
            password = current_password  # 如果前端没有提供密码，则使用当前值
            
        # 确保备注字段有值
        remark = data.get('remark')
        if remark is None:
            remark = current_remark  # 如果前端没有提供备注，则使用当前值
        
        # 更新订单信息
        execute_query("""
            UPDATE orders 
            SET account=?, password=?, package=?, status=?, remark=? 
            WHERE id=?
        """, (
            data.get('account'), 
            password, 
            package, 
            new_status, 
            remark,
            order_id
        ))
        
        return jsonify({"success": True})

    @app.route('/admin/api/orders/batch-delete', methods=['POST'])
    @login_required
    @admin_required
    def admin_api_batch_delete_orders():
        """管理员批量删除订单"""
        try:
            data = request.json
            if not data:
                logger.error("批量删除订单失败：请求中没有JSON数据")
                return jsonify({"success": False, "error": "无效的请求数据"}), 400
                
            order_ids = data.get('order_ids')
            if not order_ids or not isinstance(order_ids, list):
                logger.error(f"批量删除订单失败：无效的订单ID列表 {order_ids}")
                return jsonify({"success": False, "error": "无效的订单ID列表"}), 400

            # 获取订单总数
            total_count = execute_query("SELECT COUNT(*) FROM orders", fetch=True)[0][0]
            deleted_count = 0
            
            if len(order_ids) == total_count:
                # 全部删除，直接truncate并重置自增ID
                try:
                    if DATABASE_URL.startswith('postgres'):
                        url = urlparse(DATABASE_URL)
                        conn = psycopg2.connect(
                            dbname=url.path[1:],
                            user=url.username,
                            password=url.password,
                            host=url.hostname,
                            port=url.port
                        )
                        cur = conn.cursor()
                        # 先删除关联表数据，再删除主表数据
                        cur.execute("TRUNCATE TABLE order_notifications, orders RESTART IDENTITY CASCADE;")
                        conn.commit()
                        cur.close()
                        conn.close()
                    else:
                        # SQLite等其他数据库的处理
                        execute_query("DELETE FROM order_notifications")
                        execute_query("DELETE FROM orders")
                    deleted_count = total_count
                except Exception as e:
                    logger.error(f"全部删除订单时出错: {e}", exc_info=True)
                    return jsonify({"success": False, "error": f"删除订单时出错: {str(e)}"}), 500
            else:
                # 普通批量删除
                try:
                    # 确保所有ID都是整数
                    order_ids_int = []
                    for oid in order_ids:
                        try:
                            order_ids_int.append(int(oid))
                        except (ValueError, TypeError) as e:
                            logger.warning(f"跳过无效的订单ID: {oid}, 错误: {e}")
                            continue
                    
                    if not order_ids_int:
                        return jsonify({"success": False, "error": "没有有效的订单ID"}), 400
                    
                    # 直接使用数据库连接执行删除操作
                    deleted_count = 0    
                    if DATABASE_URL.startswith('postgres'):
                        try:
                            # PostgreSQL处理
                            url = urlparse(DATABASE_URL)
                            conn = psycopg2.connect(
                                dbname=url.path[1:],
                                user=url.username,
                                password=url.password,
                                host=url.hostname,
                                port=url.port
                            )
                            cursor = conn.cursor()
                            # 使用IN语法，更兼容
                            placeholders = ','.join(['%s'] * len(order_ids_int))
                            query = f"DELETE FROM orders WHERE id IN ({placeholders})"
                            cursor.execute(query, order_ids_int)
                            deleted_count = cursor.rowcount
                            conn.commit()
                            cursor.close()
                            conn.close()
                            
                            # 删除关联的通知记录
                            try:
                                conn = psycopg2.connect(
                                    dbname=url.path[1:],
                                    user=url.username,
                                    password=url.password,
                                    host=url.hostname,
                                    port=url.port
                                )
                                cursor = conn.cursor()
                                placeholders = ','.join(['%s'] * len(order_ids_int))
                                query = f"DELETE FROM order_notifications WHERE order_id IN ({placeholders})"
                                cursor.execute(query, order_ids_int)
                                conn.commit()
                                cursor.close()
                                conn.close()
                            except Exception as notif_err:
                                logger.warning(f"删除通知记录时出错: {notif_err}")
                                # 继续执行，不终止操作
                        except Exception as e:
                            logger.error(f"PostgreSQL批量删除错误: {e}", exc_info=True)
                            return jsonify({"success": False, "error": f"删除订单时出错: {str(e)}"}), 500
                    else:
                        try:
                            # SQLite处理
                            db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db")
                            conn = sqlite3.connect(db_path)
                            cursor = conn.cursor()
                            placeholders = ','.join(['?'] * len(order_ids_int))
                            query = f"DELETE FROM orders WHERE id IN ({placeholders})"
                            cursor.execute(query, order_ids_int)
                            deleted_count = cursor.rowcount
                            conn.commit()
                            cursor.close()
                            conn.close()
                            
                            # 删除关联的通知记录
                            try:
                                conn = sqlite3.connect(db_path)
                                cursor = conn.cursor()
                                placeholders = ','.join(['?'] * len(order_ids_int))
                                query = f"DELETE FROM order_notifications WHERE order_id IN ({placeholders})"
                                cursor.execute(query, order_ids_int)
                                conn.commit()
                                cursor.close()
                                conn.close()
                            except Exception as notif_err:
                                logger.warning(f"删除通知记录时出错: {notif_err}")
                                # 继续执行，不终止操作
                        except Exception as e:
                            logger.error(f"SQLite批量删除错误: {e}", exc_info=True)
                            return jsonify({"success": False, "error": f"删除订单时出错: {str(e)}"}), 500
                except Exception as e:
                    logger.error(f"批量删除特定订单时出错: {e}", exc_info=True)
                    return jsonify({"success": False, "error": f"删除订单时出错: {str(e)}"}), 500

            logger.info(f"管理员 {session.get('username')} 删除了 {deleted_count} 个订单: {order_ids}")
            return jsonify({"success": True, "deleted_count": deleted_count})
        except Exception as e:
            logger.error(f"批量删除订单时出错(未捕获异常): {e}", exc_info=True)
            return jsonify({"success": False, "error": f"服务器内部错误: {str(e)}"}), 500

    # 删除充值相关路由

    # 删除余额明细记录API
    @app.route('/api/balance/records')
    @login_required
    def api_balance_records():
        """获取余额明细记录（为保持兼容性而添加）"""
        return jsonify({
            "success": True,
            "records": []
        })
        
    @app.route('/api/active-sellers')
    @login_required
    def api_active_sellers():
        """获取活跃的卖家列表，供下单时选择接单人使用"""
        sellers = get_active_sellers()
        
        # 格式化最后活跃时间
        for seller in sellers:
            if seller['last_active_at']:
                # 计算距离现在的时间差
                try:
                    last_active = datetime.strptime(seller['last_active_at'], "%Y-%m-%d %H:%M:%S")
                    now = datetime.now()
                    diff = now - last_active
                    
                    if diff.days > 0:
                        seller['active_status'] = f"{diff.days}天前活跃"
                    elif diff.seconds > 3600:
                        seller['active_status'] = f"{diff.seconds // 3600}小时前活跃"
                    elif diff.seconds > 60:
                        seller['active_status'] = f"{diff.seconds // 60}分钟前活跃"
                    else:
                        seller['active_status'] = "刚刚活跃"
                except:
                    seller['active_status'] = "未知"
            else:
                seller['active_status'] = "从未活跃"
        
        return jsonify({
            "success": True,
            "sellers": sellers
        })
        
    @app.route('/api/all-sellers')
    @login_required
    def api_all_sellers():
        """获取所有卖家列表（包括非活跃的），供订单筛选使用"""
        # 获取所有卖家
        sellers_raw = get_all_sellers()
        
        # 格式化卖家数据
        sellers = []
        for s in sellers_raw:
            telegram_id, username, first_name, nickname, is_active = s[0], s[1], s[2], s[3], s[4]
            # 如果没有设置昵称，则使用first_name或username作为默认昵称
            display_name = nickname or first_name or f"卖家 {telegram_id}"
            sellers.append({
                "id": telegram_id,
                "name": display_name
            })
        
        return jsonify({
            "success": True,
            "sellers": sellers
        })
        
    @app.route('/api/check-seller-activity/<int:seller_id>', methods=['POST'])
    @login_required
    def check_seller_activity_api(seller_id):
        """发送卖家活跃度检查请求"""
        try:
            # 检查卖家是否存在且活跃
            if DATABASE_URL.startswith('postgres'):
                seller = execute_query(
                    "SELECT telegram_id FROM sellers WHERE telegram_id = ? AND is_active = TRUE", 
                    (seller_id,), 
                    fetch=True
                )
            else:
                seller = execute_query(
                    "SELECT telegram_id FROM sellers WHERE telegram_id = ? AND is_active = 1", 
                    (seller_id,), 
                    fetch=True
                )
            
            if not seller:
                return jsonify({"success": False, "message": "卖家不存在或未激活"}), 404
            
            # 记录检查请求
            check_seller_activity(str(seller_id))
            
            # 发送通知
            notification_queue.put({
                'type': 'activity_check',
                'seller_id': seller_id
            })
            
            return jsonify({
                "success": True,
                "message": "活跃度检查请求已发送"
            })
        except Exception as e:
            logger.error(f"发送卖家活跃度检查请求失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "message": f"操作失败: {str(e)}"}), 500

    # 删除用户价格API

    @app.route('/admin/api/sellers/<int:telegram_id>', methods=['PUT'])
    @login_required
    @admin_required
    def admin_api_update_seller(telegram_id):
        """更新卖家信息"""
        data = request.get_json()
        nickname = data.get('nickname')
        distribution_level = data.get('distribution_level')
        max_concurrent_orders = data.get('max_concurrent_orders')
        
        try:
            if nickname is not None:
                update_seller_nickname(telegram_id, nickname)
                
            if distribution_level is not None:
                # 确保分流等级是合法的整数
                level = int(distribution_level)
                if level < 1:
                    level = 1
                if level > 10:
                    level = 10
                    
                # 更新分流等级
                if DATABASE_URL.startswith('postgres'):
                    execute_query("UPDATE sellers SET distribution_level = %s WHERE telegram_id = %s", (level, telegram_id))
                else:
                    execute_query("UPDATE sellers SET distribution_level = ? WHERE telegram_id = ?", (level, telegram_id))
                
                logger.info(f"更新卖家 {telegram_id} 分流等级为 {level}")
            
            if max_concurrent_orders is not None:
                # 确保最大接单数是合法的整数
                max_orders = int(max_concurrent_orders)
                if max_orders < 1:
                    max_orders = 1
                if max_orders > 20:
                    max_orders = 20
                    
                # 更新最大接单数
                if DATABASE_URL.startswith('postgres'):
                    execute_query("UPDATE sellers SET max_concurrent_orders = %s WHERE telegram_id = %s", (max_orders, telegram_id))
                else:
                    execute_query("UPDATE sellers SET max_concurrent_orders = ? WHERE telegram_id = ?", (max_orders, telegram_id))
                
                logger.info(f"更新卖家 {telegram_id} 最大接单数为 {max_orders}")
                
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"更新卖家 {telegram_id} 信息失败: {e}")
            return jsonify({"error": "Update failed"}), 500

    @app.route('/orders/confirm/<int:oid>', methods=['POST'])
    @login_required
    def confirm_order(oid):
        """买家确认订单已完成"""
        user_id = session.get('user_id')
        
        # 获取确认状态
        data = request.get_json()
        confirm_status = data.get('status', CONFIRM_STATUS['CONFIRMED']) if data else CONFIRM_STATUS['CONFIRMED']
        
        # 检查订单是否存在并属于当前用户
        order = execute_query("SELECT status, user_id, accepted_by FROM orders WHERE id=?", (oid,), fetch=True)
        if not order:
            return jsonify({"error": "订单不存在"}), 404
            
        status, order_user_id, accepted_by = order[0]
        
        # 检查订单是否属于当前用户
        if order_user_id != user_id and not session.get('is_admin'):
            return jsonify({"error": "您无权确认该订单"}), 403
        
        # 不再检查订单状态，允许任何状态的订单都可以确认
        
        try:
            # 更新buyer_confirmed字段和confirm_status字段
            timestamp = get_china_time()
            # 如果是确认收到，则设置buyer_confirmed为TRUE，否则设置为FALSE
            buyer_confirmed = True if confirm_status == CONFIRM_STATUS['CONFIRMED'] else False
            
            execute_query(
                "UPDATE orders SET buyer_confirmed=?, confirm_status=?, buyer_confirmed_at=? WHERE id=?", 
                (buyer_confirmed, confirm_status, timestamp, oid)
            )
            
            logger.info(f"用户 {user_id} 更新订单 {oid} 确认状态为 {confirm_status}，buyer_confirmed设置为{buyer_confirmed}，时间: {timestamp}")

            # 发送通知
            try:
                notification_queue.put({
                    'type': 'buyer_confirmed',
                    'order_id': oid,
                    'handler_id': user_id,
                    'confirm_status': confirm_status
                })
                logger.info(f"已将订单 {oid} 买家确认通知添加到队列")
            except Exception as e:
                logger.error(f"添加买家确认通知到队列失败: {e}", exc_info=True)

            return jsonify({
                "success": True, 
                "confirm_status": confirm_status,
                "confirm_text": CONFIRM_STATUS_TEXT_ZH.get(confirm_status, confirm_status)
            })
        except Exception as e:
            logger.error(f"确认订单 {oid} 时发生错误: {e}", exc_info=True)
            return jsonify({"error": "服务器错误，请稍后重试"}), 500
            
    @app.route('/orders/update-remark/<int:oid>', methods=['POST'])
    @login_required
    def update_order_remark(oid):
        """更新订单备注"""
        user_id = session.get('user_id')
        is_admin = session.get('is_admin', 0)
        
        # 获取新备注
        data = request.json
        new_remark = data.get('remark', '')
        
        # 查询订单
        order = execute_query("""
            SELECT id, user_id
            FROM orders WHERE id=?
        """, (oid,), fetch=True)
        
        if not order:
            logger.error(f"更新备注失败: 订单 {oid} 不存在")
            return jsonify({"error": "订单不存在"}), 404
            
        order_id, order_user_id = order[0]
        
        # 权限：只能更新自己的订单备注，或管理员
        if user_id != order_user_id and not is_admin:
            logger.warning(f"用户 {user_id} 尝试更新不属于自己的订单 {oid} 的备注")
            return jsonify({"error": "权限不足"}), 403
            
        try:
            # 更新备注
            execute_query("UPDATE orders SET remark=? WHERE id=?", (new_remark, oid))
            logger.info(f"用户 {user_id} 更新订单 {oid} 的备注成功")
            
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"更新订单 {oid} 备注时发生错误: {e}", exc_info=True)
            return jsonify({"error": "服务器错误，请稍后重试"}), 500

    @app.route('/static/uploads/<path:filename>')
    def serve_uploads(filename):
        return send_from_directory('static/uploads', filename)

    @app.route('/api/today-stats')
    @login_required
    def today_stats():
        try:
            user_id = session.get('user_id')
            username = session.get('username')
            
            # 获取用户今日有效订单数
            user_valid_count = get_today_valid_orders_count(user_id)
            
            # 如果是管理员，还要获取全站今日有效订单数
            all_valid_count = 0
            if session.get('is_admin'):
                all_valid_count = get_today_valid_orders_count()  # 不传user_id，获取全站数据
                
            return jsonify({
                'success': True,
                'user_today_confirmed': user_valid_count,  # 保持字段名以兼容前端
                'all_today_confirmed': all_valid_count    # 保持字段名以兼容前端
            })
        except Exception as e:
            logger.error(f"获取今日统计失败: {str(e)}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/check-duplicate-remark', methods=['POST'])
    @login_required
    def check_duplicate_remark():
        """检查当前用户今日订单中是否有重复的备注"""
        try:
            user_id = session.get('user_id')
            remark = request.json.get('remark', '')
            
            if not remark or remark.strip() == '':
                return jsonify({
                    'success': True,
                    'duplicate': False,
                    'message': '备注为空，无需检查重复'
                })
                
            # 检查备注是否重复
            from modules.database import check_duplicate_remark
            is_duplicate = check_duplicate_remark(user_id, remark)
                
            return jsonify({
                'success': True,
                'duplicate': is_duplicate,
                'message': '发现重复备注，请确认是否继续提交' if is_duplicate else '备注没有重复'
            })
        except Exception as e:
            logger.error(f"检查备注重复失败: {str(e)}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500
    
    @app.route('/api/debug-stats')
    @login_required
    def debug_stats():
        """调试统计功能"""
        try:
            from datetime import datetime
            import pytz
            
            user_id = session.get('user_id')
            is_admin = session.get('is_admin', 0)
            
            if not is_admin:
                return jsonify({"error": "仅管理员可访问"}), 403
                
            today = datetime.now(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d")
            
            # 获取数据库类型
            db_type = "PostgreSQL" if DATABASE_URL.startswith('postgres') else "SQLite"
            
            # 获取今日充值成功的订单
            if DATABASE_URL.startswith('postgres'):
                query = """
                    SELECT id, status, updated_at, created_at
                    FROM orders 
                    WHERE status = 'completed' 
                    AND to_char(updated_at::timestamp, 'YYYY-MM-DD') = %s
                """
                params = (today,)
            else:
                query = """
                    SELECT id, status, updated_at, created_at
                    FROM orders 
                    WHERE status = 'completed' 
                    AND updated_at LIKE ?
                """
                params = (f"{today}%",)
                
            orders = execute_query(query, params, fetch=True)
            
            # 获取统计数据
            user_confirmed_count = get_user_today_confirmed_count(user_id)
            all_confirmed_count = get_all_today_confirmed_count()
            
            return jsonify({
                'success': True,
                'debug_info': {
                    'database_type': db_type,
                    'today': today,
                    'query': query,
                    'params': params,
                    'orders_count': len(orders) if orders else 0,
                    'orders': [dict(zip(['id', 'status', 'updated_at', 'created_at'], order)) for order in orders] if orders else []
                },
                'stats': {
                    'user_today_confirmed': user_confirmed_count,
                    'all_today_confirmed': all_confirmed_count
                }
            })
        except Exception as e:
            logger.error(f"调试统计功能失败: {str(e)}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/debug-orders')
    @login_required
    def debug_orders():
        """直接查询今日订单数据"""
        try:
            from datetime import datetime
            import pytz
            
            user_id = session.get('user_id')
            is_admin = session.get('is_admin', 0)
            
            if not is_admin:
                return jsonify({"error": "仅管理员可访问"}), 403
                
            today = datetime.now(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d")
            
            # 直接查询所有订单，不做任何过滤
            all_orders = execute_query(
                "SELECT id, status, updated_at, created_at FROM orders ORDER BY id DESC LIMIT 10",
                fetch=True
            )
            
            # 尝试不同的日期匹配方式
            if DATABASE_URL.startswith('postgres'):
                # 使用不同的日期匹配方法
                methods = [
                    {
                        "name": "to_char方法",
                        "query": "SELECT id, status, updated_at, created_at FROM orders WHERE status = 'completed' AND to_char(updated_at::timestamp, 'YYYY-MM-DD') = %s",
                        "params": (today,)
                    },
                    {
                        "name": "日期截取方法",
                        "query": "SELECT id, status, updated_at, created_at FROM orders WHERE status = 'completed' AND substring(updated_at, 1, 10) = %s",
                        "params": (today,)
                    },
                    {
                        "name": "简单LIKE方法",
                        "query": "SELECT id, status, updated_at, created_at FROM orders WHERE status = 'completed' AND updated_at LIKE %s",
                        "params": (f"{today}%",)
                    }
                ]
            else:
                # SQLite方法
                methods = [
                    {
                        "name": "LIKE方法",
                        "query": "SELECT id, status, updated_at, created_at FROM orders WHERE status = 'completed' AND updated_at LIKE ?",
                        "params": (f"{today}%",)
                    },
                    {
                        "name": "substr方法",
                        "query": "SELECT id, status, updated_at, created_at FROM orders WHERE status = 'completed' AND substr(updated_at, 1, 10) = ?",
                        "params": (today,)
                    }
                ]
            
            results = {}
            for method in methods:
                try:
                    orders = execute_query(method["query"], method["params"], fetch=True)
                    results[method["name"]] = {
                        "count": len(orders) if orders else 0,
                        "orders": [dict(zip(['id', 'status', 'updated_at', 'created_at'], order)) for order in orders] if orders else []
                    }
                except Exception as e:
                    results[method["name"]] = {"error": str(e)}
            
            return jsonify({
                'success': True,
                'today': today,
                'all_orders': [dict(zip(['id', 'status', 'updated_at', 'created_at'], order)) for order in all_orders] if all_orders else [],
                'results': results
            })
        except Exception as e:
            logger.error(f"调试订单数据失败: {str(e)}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500

    # 添加一个API接口用于手动清理旧订单
    @app.route('/admin/api/cleanup-old-orders', methods=['POST'])
    @login_required
    @admin_required
    def admin_cleanup_old_orders():
        """手动清理旧订单"""
        try:
            data = request.get_json()
            days = data.get('days', 3)  # 默认删除3天前的订单
            
            # 验证days参数
            try:
                days = int(days)
                if days < 1:
                    return jsonify({"success": False, "error": "天数必须大于0"}), 400
            except (ValueError, TypeError):
                return jsonify({"success": False, "error": "天数必须是有效的整数"}), 400
            
            # 执行删除操作
            deleted_count = delete_old_orders(days)
            
            # 记录操作日志
            logger.info(f"管理员 {session.get('username')} 手动清理了 {days} 天前的订单，共删除 {deleted_count} 条记录")
            
            return jsonify({
                "success": True, 
                "message": f"成功删除 {deleted_count} 条 {days} 天前的订单记录", 
                "deleted_count": deleted_count
            })
        except Exception as e:
            logger.error(f"手动清理旧订单时出错: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": f"服务器内部错误: {str(e)}"}), 500

    @app.route('/api/quick-orders')
    @login_required
    def api_quick_orders():
        """获取轻量级订单数据的API接口，优化首页加载性能"""
        try:
            # 获取参数
            limit = int(request.args.get('limit', 20))
            limit = min(limit, 1500)  # 增加最大限制到1500，支持显示更多订单
            page = int(request.args.get('page', 1))  # 添加分页支持
            offset = (page - 1) * limit
            
            # 根据用户权限决定查询范围
            is_admin = session.get('is_admin')
            user_id = session.get('user_id')
            
            # 精简SQL查询，添加completed_at字段
            fields = "id, account, status, created_at, completed_at, remark, accepted_by, accepted_by_nickname, confirm_status"
            
            if DATABASE_URL.startswith('postgres'):
                if is_admin:
                    # 管理员查看所有订单
                    orders = execute_query(f"""
                        SELECT {fields}, web_user_id as username
                        FROM orders 
                        ORDER BY id DESC LIMIT %s OFFSET %s
                    """, (limit, offset), fetch=True)
                else:
                    # 普通用户只看自己的订单
                    orders = execute_query(f"""
                        SELECT {fields}, web_user_id as username
                        FROM orders 
                        WHERE user_id = %s 
                        ORDER BY id DESC LIMIT %s OFFSET %s
                    """, (user_id, limit, offset), fetch=True)
            else:
                if is_admin:
                    orders = execute_query(f"""
                        SELECT {fields}, web_user_id as username
                        FROM orders 
                        ORDER BY id DESC LIMIT ? OFFSET ?
                    """, (limit, offset), fetch=True)
                else:
                    orders = execute_query(f"""
                        SELECT {fields}, web_user_id as username
                        FROM orders 
                        WHERE user_id = ? 
                        ORDER BY id DESC LIMIT ? OFFSET ?
                    """, (user_id, limit, offset), fetch=True)
            
            # 格式化数据，只返回必要字段
            formatted_orders = []
            for order in orders:
                oid, account, status, created_at, completed_at, remark, accepted_by, accepted_by_nickname, confirm_status, username = order
                
                order_data = {
                    "id": oid,
                    "account": account,
                    "status": status,
                    "status_text": STATUS_TEXT_ZH.get(status, status),
                    "created_at": created_at,
                    "completed_at": completed_at or "",  # 添加completed_at
                    "remark": remark or "",
                    "accepted_by": accepted_by_nickname or accepted_by or "",
                    "confirm_status": confirm_status or "pending",
                    "username": username or ""
                }
                formatted_orders.append(order_data)
            
            return jsonify({
                "success": True, 
                "orders": formatted_orders,
                "timestamp": int(time.time()),
                "page": page,
                "limit": limit
            })
        except Exception as e:
            logger.error(f"获取快速订单数据失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": "服务器内部错误"}), 500

def ensure_orders_columns():
    """确保orders表包含所有必需的列，比如buyer_confirmed_at"""
    try:
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL的检查和添加列的逻辑
            execute_query("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='buyer_confirmed_at') THEN
                        ALTER TABLE orders ADD COLUMN buyer_confirmed_at TEXT;
                    END IF;
                END$$;
            """)
        else:
            # SQLite的检查和添加列的逻辑
            # 获取表信息
            cursor = execute_query("PRAGMA table_info(orders)", fetch=True, return_cursor=True)
            columns = [row[1] for row in cursor.fetchall()]
            # 检查列是否存在
            if 'buyer_confirmed_at' not in columns:
                execute_query("ALTER TABLE orders ADD COLUMN buyer_confirmed_at TEXT")
        logger.info("Checked and ensured 'buyer_confirmed_at' column exists in 'orders' table.")
    except Exception as e:
        logger.error(f"检查或添加 'buyer_confirmed_at' 列时出错: {e}", exc_info=True)

def ensure_sellers_columns():
    """确保PostgreSQL sellers表存在所需字段"""
    if DATABASE_URL.startswith('postgres'):
        url = urlparse(DATABASE_URL)
        conn = psycopg2.connect(
            dbname=url.path[1:],
            user=url.username,
            password=url.password,
            host=url.hostname,
            port=url.port
        )
        cur = conn.cursor()
        # 确保连接成功
        conn.commit()
        cur.close()
        conn.close()

# 在模块加载时自动执行
ensure_orders_columns()
ensure_sellers_columns()