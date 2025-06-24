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

from flask import Flask, request, render_template, jsonify, session, redirect, url_for, flash, send_file

from modules.constants import STATUS, STATUS_TEXT_ZH, WEB_PRICES, PLAN_OPTIONS, REASON_TEXT_ZH, DATABASE_URL
from modules.database import (
    execute_query, hash_password, get_all_sellers, add_seller, remove_seller, toggle_seller_status,
    get_user_balance, get_user_credit_limit, set_user_credit_limit, set_user_balance, refund_order, 
    create_order_with_deduction_atomic, get_user_recharge_requests, create_recharge_request,
    get_pending_recharge_requests, approve_recharge_request, reject_recharge_request, toggle_seller_admin,
    get_balance_records, get_activation_code, mark_activation_code_used, create_activation_code,
    get_admin_activation_codes, get_user_custom_prices, set_user_custom_price, delete_user_custom_price,
    get_active_sellers, update_seller_nickname, check_seller_activity
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
                
                # 检查是否有待处理的激活码
                if 'pending_activation_code' in session:
                    code = session.pop('pending_activation_code')
                    
                    # 如果同时有账号密码，直接跳转到激活码页面
                    if 'pending_account' in session and 'pending_password' in session:
                        account = session.pop('pending_account')
                        password = session.pop('pending_password')
                        return redirect(url_for('redeem_page', code=code))
                    
                    return redirect(url_for('redeem_page', code=code))
                
                return redirect(url_for('index'))
            else:
                logger.warning(f"用户 {username} 登录失败 - 密码错误")
                return render_template('login.html', error='用户名或密码错误')
        
        # 检查是否有激活码参数
        code = request.args.get('code')
        if code:
            session['pending_activation_code'] = code
        
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
        logger.info("处理图片上传请求")
        
        try:
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
                    os.remove(temp_path)  # 清理临时文件
                    return jsonify({"success": False, "error": "图片保存失败，文件大小为0"}), 500
                
                # 清理临时文件
                os.remove(temp_path)
            except Exception as e:
                logger.error(f"保存图片失败: {str(e)}")
                return jsonify({"success": False, "error": f"保存图片失败: {str(e)}"}), 500
            
            # 获取表单数据
            account = request.form.get('account', '').strip()
            password = request.form.get('password', '').strip()
            package = request.form.get('package', 'default_package').strip()  # 设置默认值
            
            # 获取用户ID
            user_id = session.get('user_id')
            if not user_id:
                logger.warning("订单提交失败: 用户未登录")
                return jsonify({"success": False, "error": "用户未登录"}), 401
            
            # 获取用户自定义价格
            custom_prices = get_user_custom_prices(user_id)
            
            # 确定订单金额
            plan_key = package.split('-')[0] if '-' in package else package
            amount = float(custom_prices.get(plan_key, WEB_PRICES.get(plan_key, 0.0)))
            
            if amount <= 0:
                logger.warning(f"订单提交失败: 无效的套餐金额 - {package}")
                return jsonify({"success": False, "error": "无效的套餐金额"}), 400
            
            # 创建订单 - 使用原子操作
            try:
                order_id = create_order_with_deduction_atomic(
                    user_id=user_id,
                    account=account,
                    password=password,
                    package=package,
                    amount=amount,
                    qr_code_path=file_path,
                    created_at=get_china_time()
                )
                
                if order_id is None:
                    logger.warning("订单创建失败: 余额不足")
                    return jsonify({"success": False, "error": "余额不足，无法创建订单"}), 400
                
                logger.info(f"订单创建成功: ID={order_id}, 用户ID={user_id}, 套餐={package}")
                
                # 发送通知到Telegram机器人
                try:
                    from modules.telegram_bot import send_order_notification
                    send_order_notification(notification_queue, order_id, account, package, amount, file_path)
                except Exception as e:
                    logger.error(f"发送Telegram通知失败: {str(e)}")
                
                return jsonify({"success": True, "order_id": order_id, "message": "订单创建成功"})
            except Exception as e:
                logger.error(f"创建订单失败: {str(e)}")
                return jsonify({"success": False, "error": f"创建订单失败: {str(e)}"}), 500
        except Exception as e:
            logger.error(f"处理订单请求时发生错误: {str(e)}")
            return jsonify({"success": False, "error": f"处理订单请求时发生错误: {str(e)}"}), 500

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
        limit = int(request.args.get('limit', 1000))  # 默认返回1000条订单
        offset = int(request.args.get('offset', 0))
        user_filter = ""
        params = []
        
        # 非管理员只能看到自己的订单
        if not session.get('is_admin'):
            user_filter = "WHERE user_id = ?"
            params.append(session.get('user_id'))
        
        # 查询订单 - 简化查询，移除不需要的字段
        orders = execute_query(f"""
            SELECT id, account, package, status, created_at, accepted_at, completed_at,
                   remark, user_id, accepted_by, accepted_by_username, accepted_by_first_name
            FROM orders 
            {user_filter}
            ORDER BY id DESC LIMIT ? OFFSET ?
        """, params + [limit, offset], fetch=True)
        
        logger.info(f"查询到 {len(orders)} 条订单记录")
        
        # 格式化数据
        formatted_orders = []
        for order in orders:
            oid, account, package, status, created_at, accepted_at, completed_at, remark, user_id, accepted_by, accepted_by_username, accepted_by_first_name = order
            
            # 优先使用昵称，其次是用户名，最后是ID
            seller_display = accepted_by_first_name or accepted_by_username or accepted_by
            if seller_display and not isinstance(seller_display, str):
                seller_display = str(seller_display)
            
            # 如果是失败状态，翻译失败原因
            translated_remark = remark
            if status == STATUS['FAILED'] and remark:
                translated_remark = REASON_TEXT_ZH.get(remark, remark)
            
            # 检查是否为图片路径
            is_image = account and (account.startswith('static/uploads/') or account.startswith('/static/uploads/'))
            
            order_data = {
                "id": oid,
                "account": account,  # 返回实际路径，让前端显示图片
                "package": package,
                "status": status,
                "status_text": STATUS_TEXT_ZH.get(status, status),
                "created_at": created_at,
                "accepted_at": accepted_at or "",
                "completed_at": completed_at or "",
                "remark": translated_remark or "",
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
            
            # 计算总消费额
            today_consumption = 0
            for order in today_orders:
                package = order[0]
                # 从常量获取套餐价格
                if package in WEB_PRICES:
                    today_consumption += WEB_PRICES[package]
            
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
        
        success = set_user_credit_limit(user_id, credit_limit)
        
        if success:
            logger.info(f"管理员设置用户ID={user_id}的透支额度为{credit_limit}")
            return jsonify({"success": True, "credit_limit": credit_limit})
        else:
            return jsonify({"error": "更新透支额度失败"}), 500
            
    @app.route('/admin/api/users/<int:user_id>/custom-prices', methods=['GET'])
    @login_required
    @admin_required
    def admin_get_user_custom_prices(user_id):
        """获取用户定制价格（仅限管理员）"""
        try:
            # 获取用户信息
            user = execute_query("SELECT username FROM users WHERE id=?", (user_id,), fetch=True)
            if not user:
                return jsonify({"error": "用户不存在"}), 404
                
            username = user[0][0]
            
            # 获取用户定制价格
            custom_prices = get_user_custom_prices(user_id)
            
            # 准备返回数据
            return jsonify({
                "success": True,
                "user_id": user_id,
                "username": username,
                "custom_prices": custom_prices,
                "default_prices": WEB_PRICES
            })
        except Exception as e:
            logger.error(f"获取用户定制价格失败: {str(e)}", exc_info=True)
            return jsonify({"error": f"获取失败: {str(e)}"}), 500
            
    @app.route('/admin/api/users/<int:user_id>/custom-prices', methods=['POST'])
    @login_required
    @admin_required
    def admin_set_user_custom_price(user_id):
        """设置用户定制价格（仅限管理员）"""
        if not request.is_json:
            return jsonify({"error": "请求必须为JSON格式"}), 400
            
        data = request.get_json()
        package = data.get('package')
        price = data.get('price')
        
        if not package:
            return jsonify({"error": "缺少package字段"}), 400
            
        if price is None:
            return jsonify({"error": "缺少price字段"}), 400
            
        try:
            price = float(price)
        except ValueError:
            return jsonify({"error": "price必须为数字"}), 400
            
        # 价格验证
        if price <= 0:
            return jsonify({"error": "价格必须大于0"}), 400
            
        # 检查套餐是否有效
        if package not in WEB_PRICES:
            return jsonify({"error": f"无效的套餐: {package}"}), 400
            
        # 检查用户是否存在
        user = execute_query("SELECT username FROM users WHERE id=?", (user_id,), fetch=True)
        if not user:
            return jsonify({"error": "用户不存在"}), 404
            
        admin_id = session.get('user_id')
        
        try:
            # 如果价格为0，则删除定制价格，使用默认价格
            if price == 0:
                success = delete_user_custom_price(user_id, package)
                message = "已删除定制价格，将使用默认价格"
            else:
                success = set_user_custom_price(user_id, package, price, admin_id)
                message = "定制价格设置成功"
                
            if not success:
                return jsonify({"error": "设置定制价格失败"}), 500
                
            # 获取更新后的用户定制价格
            custom_prices = get_user_custom_prices(user_id)
            
            return jsonify({
                "success": True,
                "message": message,
                "custom_prices": custom_prices
            })
        except Exception as e:
            logger.error(f"设置用户定制价格失败: {str(e)}", exc_info=True)
            return jsonify({"error": f"设置失败: {str(e)}"}), 500

    @app.route('/admin/api/orders')
    @login_required
    @admin_required
    def admin_api_orders():
        try:
            # 获取分页参数
            page = int(request.args.get('page', 1))
            per_page = int(request.args.get('per_page', 20))
            offset = (page - 1) * per_page
            
            # 获取搜索参数
            search = request.args.get('search', '').strip()
            
            # 构建查询条件
            conditions = []
            params = []
            
            if search:
                conditions.append("(o.id LIKE ? OR o.account LIKE ? OR u.username LIKE ?)")
                search_param = f"%{search}%"
                params.extend([search_param, search_param, search_param])
            
            where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
            
            # 查询订单 - 关联users表获取创建者用户名，并关联sellers表获取接单人昵称
            query = f"""
                SELECT o.id, o.account, o.package, o.status, o.created_at, 
                       u.username as creator, s.nickname as accepter
                FROM orders o
                LEFT JOIN users u ON o.user_id = u.id
                LEFT JOIN sellers s ON o.accepted_by = s.telegram_id
                {where_clause}
                ORDER BY o.id DESC
                LIMIT ? OFFSET ?
            """
            params.extend([per_page, offset])
            orders = execute_query(query, params, fetch=True)
            
            # 查询总数用于分页
            count_query = f"""
                SELECT COUNT(*) 
                FROM orders o
                LEFT JOIN users u ON o.user_id = u.id
                {where_clause}
            """
            total_count = execute_query(count_query, params[:-2], fetch=True)[0][0]
            
            return jsonify({
                "success": True,
                "data": [{
                    "id": order[0],
                    "account": order[1],
                    "package": order[2],
                    "status": order[3],
                    "created_at": order[4],
                    "creator": order[5],
                    "accepter": order[6] if order[6] else ""
                } for order in orders],
                "total": total_count,
                "page": page,
                "per_page": per_page
            })
        except Exception as e:
            logger.error(f"获取订单列表失败: {str(e)}")
            return jsonify({"success": False, "error": f"获取订单列表失败: {str(e)}"}), 500
        
    @app.route('/admin/api/sellers', methods=['GET'])
    @login_required
    @admin_required
    def admin_api_get_sellers():
        sellers = get_all_sellers()
        return jsonify([{
            "telegram_id": s[0],
            "username": s[1],
            "first_name": s[2],
            "nickname": s[3],
            "is_active": bool(s[4]),
            "added_at": s[5],
            "added_by": s[6],
            "is_admin": bool(s[7])
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
            
        # 不允许修改超级管理员的身份
        if str(telegram_id) == "1878943383":
            return jsonify({"error": "Cannot modify superadmin status"}), 403
            
        if toggle_seller_admin(telegram_id):
            return jsonify({"success": True})
        else:
            return jsonify({"error": "Operation failed"}), 500

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
                       o.accepted_by_username, o.accepted_by_first_name, u.username as creator_name
                FROM orders o
                LEFT JOIN users u ON o.user_id = u.id
                WHERE o.id = %s
            """, (order_id,), fetch=True)
        else:
            # SQLite查询
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
        
        # 根据不同数据库处理返回格式
        if DATABASE_URL.startswith('postgres'):
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
                "accepted_by": o[12] or o[13] or "",  # 优先使用昵称，其次是用户名
                "creator": o[10] or o[14] or "N/A",   # web_user_id或creator_name
                "user_id": o[11]
            })
        else:
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
                "accepted_by": o[12] or o[13] or "",  # 优先使用昵称，其次是用户名
                "creator": o[10] or "N/A",           # web_user_id
                "user_id": o[11]
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
        """批量删除订单（仅限管理员）"""
        data = request.json
        
        if not data or 'order_ids' not in data:
            return jsonify({"error": "缺少order_ids参数"}), 400
            
        order_ids = data['order_ids']
        
        if not isinstance(order_ids, list) or len(order_ids) == 0:
            return jsonify({"error": "order_ids必须是非空列表"}), 400
            
        try:
            # 构建SQL的IN子句
            order_ids_str = ','.join(['?'] * len(order_ids))
            
            # 先获取订单信息用于日志
            orders_info = execute_query(
                f"SELECT id, account, status FROM orders WHERE id IN ({order_ids_str})",
                order_ids,
                fetch=True
            )
            
            # 删除订单
            execute_query(
                f"DELETE FROM orders WHERE id IN ({order_ids_str})",
                order_ids
            )
            
            logger.info(f"管理员批量删除了{len(order_ids)}个订单: {order_ids}")
            
            # 记录详细的订单信息
            for order in orders_info:
                oid, account, status = order
                logger.info(f"已删除订单: ID={oid}, 账号={account}, 状态={status}")
            
            return jsonify({"success": True, "message": f"成功删除{len(order_ids)}个订单"})
        except Exception as e:
            logger.error(f"批量删除订单失败: {str(e)}", exc_info=True)
            return jsonify({"error": f"删除失败: {str(e)}"}), 500

    @app.route('/api/notifications')
    @login_required
    def api_get_notifications():
        """获取用户通知"""
        user_id = session.get('user_id')
        
        # 获取未读通知数量和最新的几条通知
        try:
            # 获取未读通知数量
            unread_count = execute_query(
                "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0",
                (user_id,),
                fetch=True
            )[0][0]
            
            # 获取最新的10条通知
            notifications = execute_query(
                "SELECT id, type, content, created_at, is_read FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 10",
                (user_id,),
                fetch=True
            )
            
            # 格式化通知
            formatted_notifications = []
            for notif in notifications:
                notif_id, notif_type, content_str, created_at, is_read = notif
                
                try:
                    content = json.loads(content_str)
                except:
                    content = {"message": content_str}
                
                formatted_notifications.append({
                    "id": notif_id,
                    "type": notif_type,
                    "content": content,
                    "created_at": created_at,
                    "is_read": bool(is_read)
                })
            
            return jsonify({
                "success": True,
                "unread_count": unread_count,
                "notifications": formatted_notifications
            })
        except Exception as e:
            logger.error(f"获取通知失败: {str(e)}", exc_info=True)
            return jsonify({"error": f"获取通知失败: {str(e)}"}), 500

    @app.route('/api/notifications/mark-read', methods=['POST'])
    @login_required
    def api_mark_notifications_read():
        """标记通知为已读"""
        user_id = session.get('user_id')
        data = request.json
        
        if not data:
            return jsonify({"error": "缺少参数"}), 400
            
        # 如果提供了notification_ids，则只标记指定的通知
        notification_ids = data.get('notification_ids', [])
        
        try:
            if notification_ids:
                # 标记指定的通知为已读
                ids_str = ','.join(['?'] * len(notification_ids))
                params = notification_ids + [user_id]
                execute_query(
                    f"UPDATE notifications SET is_read = 1 WHERE id IN ({ids_str}) AND user_id = ?",
                    params
                )
                logger.info(f"已标记用户{user_id}的通知{notification_ids}为已读")
            else:
                # 标记所有通知为已读
                execute_query(
                    "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0",
                    (user_id,)
                )
                logger.info(f"已标记用户{user_id}的所有通知为已读")
            
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"标记通知已读失败: {str(e)}", exc_info=True)
            return jsonify({"error": f"标记通知已读失败: {str(e)}"}), 500
    
    @app.route('/orders/user_confirm/<int:oid>', methods=['POST'])
    @login_required
    def confirm_order_user(oid):
        """用户确认收货，强制将订单状态置为已完成"""
        user_id = session.get('user_id')
        is_admin = session.get('is_admin', 0)

        # 查询订单
        order = execute_query("""
            SELECT id, user_id, status, accepted_by
            FROM orders WHERE id=?
        """, (oid,), fetch=True)

        if not order:
            logger.error(f"确认收货失败: 订单 {oid} 不存在")
            return jsonify({"error": "订单不存在"}), 404

        order_id, order_user_id, status, accepted_by = order[0]

        # 权限：只能确认自己的订单，或管理员
        if user_id != order_user_id and not is_admin:
            logger.warning(f"用户 {user_id} 尝试确认不属于自己的订单 {oid}")
            return jsonify({"error": "权限不足"}), 403

        # 允许确认已提交或已接单状态的订单，但已完成状态不需要再确认
        if status == STATUS['COMPLETED']:
            logger.info(f"订单 {oid} 已是完成状态，无需再次确认")
            return jsonify({"success": True, "message": "订单已是完成状态"})
        
        # 只有已提交、已接单、正在质疑状态的订单可以确认收货
        if status not in [STATUS['SUBMITTED'], STATUS['ACCEPTED'], STATUS['DISPUTING']]:
            logger.warning(f"订单 {oid} 状态为 {status}，不允许确认收货")
            return jsonify({"error": "订单状态不允许确认收货"}), 400

        try:
            # 更新状态
            timestamp = get_china_time()
            execute_query("UPDATE orders SET status=?, completed_at=? WHERE id= ?", 
                         (STATUS['COMPLETED'], timestamp, oid))
            logger.info(f"用户 {user_id} 确认订单 {oid} 收货成功，状态已更新为已完成")

            # 只发送一个通知，直接更新原始订单消息
            try:
                # 使用 order_status_change 类型，这会在 TG 中更新原始消息
                notification_queue.put({
                    'type': 'order_status_change',
                    'order_id': oid,
                    'status': STATUS['COMPLETED'],
                    'handler_id': user_id,
                    'update_original': True  # 标记需要更新原始消息
                })
                logger.info(f"已将订单 {oid} 确认收货通知添加到队列")
            except Exception as e:
                logger.error(f"添加订单状态变更通知到队列失败: {e}", exc_info=True)

            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"确认订单 {oid} 收货时发生错误: {e}", exc_info=True)
            return jsonify({"error": "服务器错误，请稍后重试"}), 500

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
            logger.error(f"批量删除激活码失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "message": f"操作失败: {str(e)}"}), 500

    @app.route('/admin/api/activation-codes/export', methods=['GET'])
    @login_required
    @admin_required
    def admin_api_export_activation_codes():
        """导出激活码到TXT文件"""
        try:
            # 获取查询参数
            is_used = request.args.get('is_used')
            package = request.args.get('package')
            
            # 构建查询条件
            conditions = []
            params = []
            
            if is_used is not None:
                is_used = int(is_used)
                conditions.append("is_used = ?")
                params.append(is_used)
                
            if package:
                conditions.append("package = ?")
                params.append(package)
                
            where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
            
            # 查询激活码
            query = f"SELECT code, package FROM activation_codes{where_clause} ORDER BY created_at DESC"
            codes = execute_query(query, params, fetch=True)
            
            if not codes:
                return jsonify({"success": False, "message": "没有找到符合条件的激活码"}), 404
                
            # 创建文本内容
            current_time = datetime.now().strftime("%Y%m%d%H%M%S")
            filename = f"activation_codes_{current_time}.txt"
            
            # 构建响应
            text_content = ""
            for code_data in codes:
                code, package = code_data
                text_content += f"{code} - {package}个月\n"
                
            # 创建响应
            response = app.response_class(
                response=text_content,
                status=200,
                mimetype='text/plain'
            )
            response.headers["Content-Disposition"] = f"attachment; filename={filename}"
            
            logger.info(f"管理员导出了 {len(codes)} 个激活码")
            return response
            
        except Exception as e:
            logger.error(f"导出激活码失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "message": f"导出失败: {str(e)}"}), 500 

    @app.route('/admin/api/sellers/<int:telegram_id>', methods=['PUT'])
    @login_required
    @admin_required
    def admin_api_update_seller(telegram_id):
        """更新卖家信息，目前仅支持修改nickname"""
        data = request.get_json()
        nickname = data.get('nickname')
        if nickname is None:
            return jsonify({"error": "Missing nickname"}), 400
        
        try:
            update_seller_nickname(telegram_id, nickname)
            return jsonify({"success": True})
        except Exception as e:
            logger.error(f"更新卖家 {telegram_id} 昵称失败: {e}")
            return jsonify({"error": "Update failed"}), 500