import os
import time
import logging
from functools import wraps
from datetime import datetime
import pytz
import json
import random
import string

from flask import Flask, request, render_template, jsonify, session, redirect, url_for, flash, send_file

from modules.constants import STATUS, STATUS_TEXT_ZH, PLAN_OPTIONS
from modules.database import (
    execute_query, hash_password, get_user_balance, create_order_with_deduction_atomic
)

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
            
            # 获取用户余额
            user_id = session.get('user_id')
            balance = get_user_balance(user_id)
            
            return render_template('index.html', 
                                   orders=orders, 
                                   plan_options=PLAN_OPTIONS,
                                   username=session.get('username'),
                                   is_admin=session.get('is_admin'),
                                   balance=balance)
        except Exception as e:
            logger.error(f"获取订单失败: {str(e)}", exc_info=True)
            return render_template('index.html', 
                                   error='获取订单失败', 
                                   plan_options=PLAN_OPTIONS,
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
            os.makedirs(save_path, exist_ok=True)
            
            # 完整的保存路径
            full_save_path = os.path.join(save_path, unique_filename)
            
            # 将临时文件移动到目标位置
            shutil.move(temp_path, full_save_path)
            
            logger.info(f"二维码图片已保存到: {full_save_path}")
            
            # 数据库中存储的路径（相对路径）
            db_path = os.path.join('uploads', timestamp, unique_filename)
            
        except Exception as e:
            logger.error(f"保存二维码图片失败: {str(e)}")
            return jsonify({"success": False, "error": f"保存二维码图片失败: {str(e)}"}), 500
        
        try:
            # 获取表单数据
            package_type = request.form.get('package', '12')  # 默认为1年会员
            remark = request.form.get('remark', '')
            
            # 获取用户信息
            user_id = session.get('user_id')
            username = session.get('username')
            
            # 创建订单并扣款（原子操作）
            result = create_order_with_deduction_atomic(
                user_id=user_id,
                account=db_path,  # 使用保存的图片路径作为账号标识
                package=package_type,
                remark=remark
            )
            
            if not result['success']:
                return jsonify({"success": False, "error": result['error']}), 400
                
            order_id = result['order_id']
            new_balance = result['new_balance']
            
            # 将订单通知添加到队列
            notification_queue.put({
                'type': 'new_order',
                'order_id': order_id,
                'username': username,
                'package': package_type,
                'qr_code_path': db_path,
                'time': get_china_time()
            })
            
            logger.info(f"订单 #{order_id} 创建成功，新余额: {new_balance}")
            
            # 获取最新的5个订单
            orders = execute_query(
                "SELECT id, account, package, status, created_at FROM orders WHERE creator_id=? ORDER BY id DESC LIMIT 5", 
                (user_id,), 
                fetch=True
            )
            
            return jsonify({
                "success": True, 
                "message": "订单提交成功！请等待处理",
                "order_id": order_id,
                "new_balance": new_balance,
                "orders": [
                    {
                        "id": order[0],
                        "account": order[1],
                        "package": order[2],
                        "status": order[3],
                        "status_text": STATUS_TEXT_ZH.get(order[3], order[3]),
                        "created_at": order[4]
                    } for order in orders
                ] if orders else []
            })
            
        except Exception as e:
            logger.error(f"创建订单失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": f"创建订单失败: {str(e)}"}), 500

    @app.route('/orders/recent')
    @login_required
    def orders_recent():
        # 获取最近的订单
        try:
            user_id = session.get('user_id')
            is_admin = session.get('is_admin', False)
            
            if is_admin:
                # 管理员可以看到所有订单
                orders = execute_query(
                    "SELECT id, account, package, status, created_at, creator_id FROM orders ORDER BY id DESC LIMIT 20", 
                    fetch=True
                )
            else:
                # 普通用户只能看到自己的订单
                orders = execute_query(
                    "SELECT id, account, package, status, created_at, creator_id FROM orders WHERE creator_id=? ORDER BY id DESC LIMIT 20", 
                    (user_id,), 
                    fetch=True
                )
            
            # 将订单转换为JSON格式
            result = []
            for order in orders:
                order_id, account, package, status, created_at, creator_id = order
                
                # 获取创建者用户名
                creator = None
                if creator_id:
                    creator_data = execute_query("SELECT username FROM users WHERE id=?", (creator_id,), fetch=True)
                    creator = creator_data[0][0] if creator_data else None
                
                result.append({
                    "id": order_id,
                    "account": account,
                    "package": package,
                    "status": status,
                    "status_text": STATUS_TEXT_ZH.get(status, status),
                    "created_at": created_at,
                    "creator": creator
                })
            
            return jsonify({"success": True, "orders": result})
            
        except Exception as e:
            logger.error(f"获取最近订单失败: {str(e)}")
            return jsonify({"success": False, "error": str(e)}), 500
            
    # 订单详情查看路由
    @app.route('/<path:filename>')
    @login_required
    def uploaded_file(filename):
        """提供上传的文件访问"""
        try:
            if 'uploads' in filename:
                return send_file(os.path.join('static', filename))
            return "文件不存在", 404
        except Exception as e:
            logger.error(f"访问上传文件失败: {str(e)}")
            return "访问文件失败", 500