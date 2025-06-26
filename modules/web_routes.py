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
            
            # 查询卖家昵称
            seller_info = execute_query(
                "SELECT nickname, first_name, username FROM sellers WHERE telegram_id = ?",
                (preferred_seller,),
                fetch=True
            )
            display_name = None
            if seller_info:
                nickname, first_name, username = seller_info[0]
                display_name = nickname or first_name or username
            if not display_name:
                display_name = "指定卖家"
            remark = f"[指定接单人:{display_name}] {remark}"
            logger.info(f"用户指定接单人: {preferred_seller} ({display_name})")
        
        logger.info(f"收到订单提交请求: 二维码={file_path}, 套餐={package}, 指定接单人={preferred_seller or '无'}")
        
        if not account:
            logger.warning("订单提交失败: 二维码保存失败")
            return jsonify({"success": False, "error": "二维码保存失败，请重试"}), 400
        
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
                    "can_cancel": o[4] == STATUS['SUBMITTED'] and (session.get('is_admin') or session.get('user_id') == o[6])
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
                    'remark': remark  # 添加备注信息
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
                "credit_limit": credit_limit
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
        """获取用户最近的订单"""
        # 获取查询参数
        limit = int(request.args.get('limit', 1000))  # 增加默认值以支持加载更多订单
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
            if seller_display and not isinstance(seller_display, str):
                seller_display = str(seller_display)
            
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
        """获取所有订单"""
        # 获取查询参数
        limit = int(request.args.get('limit', 1000))  # 增加默认值以支持加载更多订单
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
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL查询，使用COALESCE获取web_user_id或从users表联查username
            orders = execute_query(f"""
                SELECT o.id, o.account, o.password, o.package, o.status, o.remark, o.created_at, o.accepted_at, o.completed_at, 
                       COALESCE(o.web_user_id, u.username) as creator, o.accepted_by, o.accepted_by_username, o.accepted_by_first_name, o.refunded
                FROM orders o
                LEFT JOIN users u ON o.user_id = u.id
                {where_clause}
                ORDER BY o.id DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset], fetch=True)
            
            # 查询订单总数
            count = execute_query(f"""
                SELECT COUNT(*) FROM orders {where_clause}
            """, params, fetch=True)[0][0]
        else:
            # SQLite查询
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
                    "username": accepted_by_username or str(accepted_by),
                    "name": accepted_by_first_name or str(accepted_by)
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
                "creator": creator or "N/A",
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
        """管理员批量删除订单"""
        data = request.json
        order_ids = data.get('order_ids')

        if not order_ids or not isinstance(order_ids, list):
            return jsonify({"success": False, "error": "无效的订单ID列表"}), 400

        try:
            # 获取订单总数
            total_count = execute_query("SELECT COUNT(*) FROM orders", fetch=True)[0][0]
            if len(order_ids) == total_count:
                # 全部删除，直接truncate并重置自增ID
                if DATABASE_URL.startswith('postgres'):
                    import psycopg2
                    from urllib.parse import urlparse
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
                    cur.execute("TRUNCATE TABLE order_notifications RESTART IDENTITY;")
                    cur.execute("TRUNCATE TABLE orders RESTART IDENTITY;")
                    conn.commit()
                    cur.close()
                    conn.close()
                else:
                    # SQLite等其他数据库的处理
                    execute_query("DELETE FROM order_notifications")
                    execute_query("DELETE FROM orders")
                deleted_count = total_count
            else:
                # 普通批量删除
                order_ids_int = [int(oid) for oid in order_ids]
                placeholders = ','.join(['?'] * len(order_ids_int))
                result = execute_query(
                    f"DELETE FROM orders WHERE id IN ({placeholders})",
                    order_ids_int,
                    fetch=False,
                    return_cursor=True
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
            check_seller_activity(seller_id)
            
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
                        "SELECT id, account, package, status, created_at, completed_at, remark FROM orders WHERE remark LIKE ? ORDER BY id DESC LIMIT 1", 
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
                    "SELECT id, account, package, status, created_at, completed_at, remark FROM orders WHERE id = ?", 
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
                    "SELECT id, status FROM orders WHERE remark LIKE ? ORDER BY id DESC LIMIT 1", 
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
                    "SELECT id, status FROM orders WHERE remark LIKE ? ORDER BY id DESC LIMIT 1", 
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
            try:
                if DATABASE_URL.startswith('postgres'):
                    # PostgreSQL事务
                    import psycopg2
                    from urllib.parse import urlparse
                    url = urlparse(DATABASE_URL)
                    conn = psycopg2.connect(
                        dbname=url.path[1:],
                        user=url.username,
                        password=url.password,
                        host=url.hostname,
                        port=url.port
                    )
                    cursor = conn.cursor()
                    
                    # 开始事务
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
                    
                    # 提交事务
                    conn.commit()
                    
                else:
                    # SQLite事务
                    conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db"))
                    cursor = conn.cursor()
                    
                    # 开始事务
                    conn.execute("BEGIN TRANSACTION")
                    
                    # 1. 先检查激活码是否仍然可用
                    cursor.execute(
                        "SELECT id, is_used FROM activation_codes WHERE code = ?",
                        (code,)
                    )
                    code_check = cursor.fetchone()
                    if not code_check or code_check[1] == 1:
                        conn.rollback()
                        conn.close()
                        return jsonify({"success": False, "error": "此激活码已被使用或不存在"}), 400
                    
                    # 2. 创建订单
                    cursor.execute("""
                        INSERT INTO orders (account, password, package, remark, status, created_at, user_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        account,
                        password,
                        code_info['package'],
                        f"通过激活码兑换: {code}",
                        STATUS['SUBMITTED'],
                        now,
                        user_id
                    ))
                    order_id = cursor.lastrowid
                    
                    # 3. 标记激活码为已使用
                    cursor.execute("""
                        UPDATE activation_codes
                        SET is_used = 1, used_at = ?, used_by = ?
                        WHERE id = ?
                    """, (now, user_id if user_id > 0 else None, code_info['id']))
                    
                    # 提交事务
                    conn.commit()
                    conn.close()
                
                # 记录成功日志
                logger.info(f"用户 {username} 成功兑换激活码 {code}, 套餐: {code_info['package']}, 订单ID: {order_id}")
                
                # 将激活码和订单ID保存到session，以便刷新页面后仍能显示
                session['last_redeemed_code'] = code
                session['last_order_id'] = order_id
                
            except Exception as e:
                # 回滚事务
                if 'conn' in locals():
                    if DATABASE_URL.startswith('postgres'):
                        conn.rollback()
                    else:
                        conn.rollback()
                        conn.close()
                logger.error(f"激活码兑换事务失败: {str(e)}", exc_info=True)
                return jsonify({"success": False, "error": f"处理激活码兑换失败: {str(e)}"}), 500
            
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

    # 管理员激活码管理页面
    @app.route('/admin/activation-codes', methods=['GET'])
    @login_required
    @admin_required
    def admin_activation_codes():
        """管理员管理激活码页面"""
        return render_template('admin_activation_codes.html')

    @app.route('/admin/api/activation-codes', methods=['GET'])
    @login_required
    @admin_required
    def admin_api_get_activation_codes():
        """获取激活码列表"""
        limit = int(request.args.get('limit', 100))
        offset = int(request.args.get('offset', 0))
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
            
        # 将条件传递给数据库函数
        codes = get_admin_activation_codes(limit, offset, conditions, params)
        return jsonify({"success": True, "codes": codes})

    @app.route('/admin/api/activation-codes', methods=['POST'])
    @login_required
    @admin_required
    def admin_api_create_activation_code():
        """创建新激活码"""
        package = request.json.get('package')
        count = int(request.json.get('count', 1))
        
        if not package:
            return jsonify({"success": False, "message": "请选择套餐"}), 400
        
        if count < 1 or count > 100:
            return jsonify({"success": False, "message": "生成数量必须在1-100之间"}), 400
        
        user_id = session.get('user_id')
        codes = create_activation_code(package, user_id, count)
        
        return jsonify({
            "success": True, 
            "message": f"成功生成{len(codes)}个激活码",
            "codes": codes
        })

    @app.route('/admin/api/activation-codes/batch-delete', methods=['POST'])
    @login_required
    @admin_required
    def admin_api_batch_delete_activation_codes():
        """批量删除激活码"""
        try:
            data = request.json
            code_ids = data.get('code_ids', [])
            
            if not code_ids:
                return jsonify({"success": False, "message": "未选择任何激活码"}), 400
                
            # 构建占位符
            if DATABASE_URL.startswith('postgres'):
                placeholders = ','.join(['%s'] * len(code_ids))
                query = f"DELETE FROM activation_codes WHERE id IN ({placeholders}) AND is_used = 0"
            else:
                placeholders = ','.join(['?'] * len(code_ids))
                query = f"DELETE FROM activation_codes WHERE id IN ({placeholders}) AND is_used = 0"
            
            # 执行删除
            result = execute_query(query, code_ids, return_cursor=True)
            deleted_count = result.rowcount if result else 0
            
            logger.info(f"管理员删除了 {deleted_count} 个激活码")
            return jsonify({
                "success": True, 
                "deleted_count": deleted_count,
                "message": f"成功删除 {deleted_count} 个未使用的激活码"
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

    @app.route('/orders/confirm/<int:oid>', methods=['POST'])
    @login_required
    def confirm_order(oid):
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
            # 即使已经是完成状态，也返回成功，以便前端可以正确显示"已确认"按钮
            return jsonify({"success": True, "message": "订单已是完成状态", "already_completed": True})
        
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