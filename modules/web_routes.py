import os
import time
import logging
import base64
import re
from io import BytesIO
from datetime import datetime
import pytz
import uuid
from PIL import Image

from flask import Flask, request, render_template, jsonify, redirect, url_for, flash, session, send_from_directory

from modules.constants import STATUS, STATUS_TEXT_ZH, RECHARGE_PRICES, PLAN_OPTIONS
from modules.database import (
    execute_query, create_order, get_order_details, update_order_status,
    get_all_sellers, add_seller, remove_seller, toggle_seller_status, toggle_seller_admin
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

# ===== Web路由 =====
def register_routes(app, notification_queue):
    @app.route('/', methods=['GET'])
    def index():
        """显示订单创建表单和系统信息"""
        logger.info("访问首页")
        
        try:
            # 获取最近订单
            orders_raw = execute_query(
                """
                SELECT id, customer_name, package, status, created_at, paid_at, confirmed_at
                FROM orders
                ORDER BY id DESC
                LIMIT 5
                """,
                fetch=True
            )
            
            orders = []
            for o in orders_raw:
                orders.append({
                    "id": o[0],
                    "customer_name": o[1] or "匿名用户",
                    "package": o[2],
                    "status": o[3],
                    "status_text": STATUS_TEXT_ZH.get(o[3], o[3]),
                    "created_at": o[4],
                    "paid_at": o[5] or "",
                    "confirmed_at": o[6] or ""
                })
            
            return render_template(
                'index.html',
                orders=orders,
                prices=RECHARGE_PRICES,
                plan_options=PLAN_OPTIONS
            )
        except Exception as e:
            logger.error(f"获取订单失败: {str(e)}", exc_info=True)
            return render_template(
                'index.html',
                error='获取订单失败',
                prices=RECHARGE_PRICES,
                plan_options=PLAN_OPTIONS
            )

    @app.route('/submit_order', methods=['POST'])
    def create_new_order():
        """创建新订单"""
        customer_name = request.form.get('customer_name', '')
        package = request.form.get('package', 'default')
        qr_image_data = request.form.get('qr_image_data', '')
        
        logger.info(f"收到订单提交请求: 客户={customer_name}, 套餐={package}")
        
        if not qr_image_data:
            logger.warning("订单提交失败: 未上传二维码图片")
            return jsonify({"success": False, "message": "请上传二维码图片"}), 400
        
        try:
            # 解析base64图片数据
            image_data = re.sub(r'^data:image/.+;base64,', '', qr_image_data)
            image_bytes = base64.b64decode(image_data)
            
            # 创建目录（如果不存在）
            uploads_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'uploads')
            if not os.path.exists(uploads_dir):
                os.makedirs(uploads_dir)
            
            # 保存图片到文件
            image_filename = f"qr_{uuid.uuid4()}.png"
            image_path = os.path.join(uploads_dir, image_filename)
            
            # 使用PIL处理图片，确保是有效的图片文件
            try:
                image = Image.open(BytesIO(image_bytes))
                image.save(image_path)
            except Exception as img_error:
                logger.error(f"图片处理失败: {str(img_error)}", exc_info=True)
                return jsonify({"success": False, "message": "上传的图片无效，请确保上传的是有效的二维码图片"}), 400
            
            # 创建订单
            order_id = create_order(
                customer_name,
                package,
                image_path
            )
            
            if not order_id:
                logger.warning("订单创建失败")
                return jsonify({"success": False, "message": "创建订单失败，请重试"}), 500
            
            logger.info(f"订单提交成功: ID={order_id}, 套餐={package}")
            
            # 获取最新订单列表
            orders_raw = execute_query(
                """
                SELECT id, customer_name, package, status, created_at, paid_at, confirmed_at
                FROM orders
                ORDER BY id DESC
                LIMIT 5
                """,
                fetch=True
            )
            
            orders = []
            for o in orders_raw:
                orders.append({
                    "id": o[0],
                    "customer_name": o[1] or "匿名用户",
                    "package": o[2],
                    "status": o[3],
                    "status_text": STATUS_TEXT_ZH.get(o[3], o[3]),
                    "created_at": o[4],
                    "paid_at": o[5] or "",
                    "confirmed_at": o[6] or ""
                })
            
            # 将订单加入通知队列
            notification_queue.put({
                'type': 'new_order',
                'order_id': order_id
            })
            logger.info(f"已将订单 #{order_id} 加入通知队列")
            
            # 返回成功信息
            return jsonify({
                "success": True,
                "message": "订单已提交成功！我们的客服将尽快处理。",
                "order_id": order_id,
                "orders": orders
            })
            
        except Exception as e:
            logger.error(f"创建订单时发生意外错误: {str(e)}", exc_info=True)
            return jsonify({"success": False, "message": "服务器内部错误，请联系管理员。"}), 500

    @app.route('/order/<int:order_id>', methods=['GET'])
    def order_status(order_id):
        """显示订单状态页面"""
        try:
            order = get_order_details(order_id)
            
            if not order:
                flash('订单不存在', 'error')
                return redirect(url_for('index'))
            
            # 检查是否需要上传新二维码
            need_new_qr = order['status'] == STATUS['NEED_NEW_QR']
            
            return render_template(
                'order_status.html',
                order={
                    "id": order['id'],
                    "customer_name": order['customer_name'] or "匿名用户",
                    "package": order['package'],
                    "status": order['status'],
                    "status_text": STATUS_TEXT_ZH.get(order['status'], order['status']),
                    "message": order['message'],
                    "created_at": order['created_at'],
                    "paid_at": order['paid_at'] or "",
                    "confirmed_at": order['confirmed_at'] or ""
                },
                need_new_qr=need_new_qr,
                prices=RECHARGE_PRICES
            )
        except Exception as e:
            logger.error(f"获取订单详情失败: {str(e)}", exc_info=True)
            flash('获取订单详情失败', 'error')
            return redirect(url_for('index'))

    @app.route('/order/<int:order_id>/update_qr', methods=['POST'])
    def update_qr_code(order_id):
        """更新订单的二维码"""
        qr_image_data = request.form.get('qr_image_data', '')
        
        if not qr_image_data:
            return jsonify({"success": False, "message": "请上传二维码图片"}), 400
        
        try:
            # 获取订单详情
            order = get_order_details(order_id)
            
            if not order:
                return jsonify({"success": False, "message": "订单不存在"}), 404
            
            # 确保订单状态正确
            if order['status'] != STATUS['NEED_NEW_QR']:
                return jsonify({"success": False, "message": "此订单当前不需要更新二维码"}), 400
            
            # 解析base64图片数据
            image_data = re.sub(r'^data:image/.+;base64,', '', qr_image_data)
            image_bytes = base64.b64decode(image_data)
            
            # 创建目录（如果不存在）
            uploads_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'uploads')
            if not os.path.exists(uploads_dir):
                os.makedirs(uploads_dir)
            
            # 保存图片到文件
            image_filename = f"qr_{uuid.uuid4()}.png"
            image_path = os.path.join(uploads_dir, image_filename)
            
            # 使用PIL处理图片，确保是有效的图片文件
            try:
                image = Image.open(BytesIO(image_bytes))
                image.save(image_path)
            except Exception as img_error:
                logger.error(f"图片处理失败: {str(img_error)}", exc_info=True)
                return jsonify({"success": False, "message": "上传的图片无效，请确保上传的是有效的二维码图片"}), 400
            
            # 更新订单状态和二维码
            execute_query(
                """
                UPDATE orders 
                SET status = %s, qr_image = %s, message = NULL, notified = 0
                WHERE id = %s
                """,
                (STATUS['SUBMITTED'], image_path, order_id)
            )
            
            # 将订单加入通知队列
            notification_queue.put({
                'type': 'new_order',
                'order_id': order_id
            })
            
            return jsonify({
                "success": True,
                "message": "二维码已更新成功！我们的客服将尽快处理。"
            })
            
        except Exception as e:
            logger.error(f"更新二维码时发生意外错误: {str(e)}", exc_info=True)
            return jsonify({"success": False, "message": "服务器内部错误，请联系管理员。"}), 500

    @app.route('/uploads/<filename>')
    def uploaded_file(filename):
        """提供上传的文件访问"""
        uploads_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'uploads')
        return send_from_directory(uploads_dir, filename)

    @app.route('/check_order/<int:order_id>', methods=['GET'])
    def check_order_api(order_id):
        """API端点：返回订单状态信息"""
        try:
            order = get_order_details(order_id)
            
            if not order:
                return jsonify({"success": False, "message": "订单不存在"}), 404
            
            # 格式化返回数据
            order_data = {
                "id": order['id'],
                "customer_name": order['customer_name'] or "",
                "package": order['package'],
                "status": order['status'],
                "message": order['message'],
                "created_at": order['created_at'],
                "paid_at": order['paid_at'] or "",
                "confirmed_at": order['confirmed_at'] or ""
            }
            
            return jsonify({
                "success": True,
                "order": order_data
            })
        except Exception as e:
            logger.error(f"获取订单详情API失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "message": "获取订单详情失败"}), 500

    # ===== 管理员路由 =====
    def admin_required(f):
        """管理员权限检查装饰器"""
        from functools import wraps
        
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # 检查管理员 cookie 或会话
            is_admin = session.get('is_admin', False)
            if not is_admin:
                flash('需要管理员权限', 'error')
                return redirect(url_for('admin_login'))
            return f(*args, **kwargs)
        return decorated_function
    
    @app.route('/admin/login', methods=['GET', 'POST'])
    def admin_login():
        """管理员登录页面"""
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            
            from modules.database import hash_password
            
            # 检查是否是超级管理员
            from modules.constants import ADMIN_USERNAME, ADMIN_PASSWORD
            
            if username == ADMIN_USERNAME and hash_password(password) == hash_password(ADMIN_PASSWORD):
                session['is_admin'] = True
                session['username'] = username
                flash('登录成功', 'success')
                return redirect(url_for('admin_dashboard'))
            else:
                flash('用户名或密码错误', 'error')
        
        return render_template('admin_login.html')
    
    @app.route('/admin/logout')
    def admin_logout():
        """管理员登出"""
        session.pop('is_admin', None)
        session.pop('username', None)
        flash('已退出登录', 'success')
        return redirect(url_for('admin_login'))
    
    @app.route('/admin')
    @admin_required
    def admin_dashboard():
        """管理员仪表板页面"""
        return render_template('admin_dashboard.html', username=session.get('username'))
    
    @app.route('/admin/orders')
    @admin_required
    def admin_orders():
        """管理员订单列表页面"""
        try:
            orders_raw = execute_query(
                """
                SELECT id, customer_name, package, status, message, 
                       created_at, paid_at, confirmed_at, 
                       seller_id, seller_username, seller_first_name
                FROM orders
                ORDER BY id DESC
                """,
                fetch=True
            )
            
            orders = []
            for o in orders_raw:
                orders.append({
                    "id": o[0],
                    "customer_name": o[1] or "匿名用户",
                    "package": o[2],
                    "status": o[3],
                    "status_text": STATUS_TEXT_ZH.get(o[3], o[3]),
                    "message": o[4],
                    "created_at": o[5],
                    "paid_at": o[6] or "",
                    "confirmed_at": o[7] or "",
                    "seller_id": o[8],
                    "seller_username": o[9],
                    "seller_first_name": o[10]
                })
            
            return render_template('admin_orders.html', orders=orders, username=session.get('username'))
        except Exception as e:
            logger.error(f"获取订单列表失败: {str(e)}", exc_info=True)
            flash('获取订单列表失败', 'error')
            return redirect(url_for('admin_dashboard'))
    
    @app.route('/admin/sellers')
    @admin_required
    def admin_sellers():
        """管理员卖家管理页面"""
        try:
            sellers = get_all_sellers()
            return render_template('admin_sellers.html', sellers=sellers, username=session.get('username'))
        except Exception as e:
            logger.error(f"获取卖家列表失败: {str(e)}", exc_info=True)
            flash('获取卖家列表失败', 'error')
            return redirect(url_for('admin_dashboard'))
    
    @app.route('/admin/sellers/add', methods=['POST'])
    @admin_required
    def admin_add_seller():
        """添加新卖家"""
        telegram_id = request.form.get('telegram_id')
        username = request.form.get('username')
        first_name = request.form.get('first_name')
        
        if not telegram_id or not username or not first_name:
            flash('请填写所有字段', 'error')
            return redirect(url_for('admin_sellers'))
        
        # 确保telegram_id是整数
        try:
            telegram_id = int(telegram_id)
        except ValueError:
            flash('Telegram ID必须是一个有效的数字', 'error')
            return redirect(url_for('admin_sellers'))
        
        success = add_seller(
            telegram_id,
            username,
            first_name,
            session.get('username', 'admin')
        )
        
        if success:
            flash('卖家添加成功', 'success')
        else:
            flash('卖家添加失败', 'error')
        
        return redirect(url_for('admin_sellers'))
    
    @app.route('/admin/sellers/<telegram_id>/toggle', methods=['POST'])
    @admin_required
    def admin_toggle_seller(telegram_id):
        """切换卖家激活状态"""
        try:
            # 确保telegram_id是整数
            telegram_id = int(telegram_id)
            success = toggle_seller_status(telegram_id)
            
            if success:
                flash('卖家状态已更新', 'success')
            else:
                flash('卖家状态更新失败', 'error')
        except ValueError:
            flash('无效的卖家ID', 'error')
        
        return redirect(url_for('admin_sellers'))
    
    @app.route('/admin/sellers/<telegram_id>/toggle_admin', methods=['POST'])
    @admin_required
    def admin_toggle_seller_admin(telegram_id):
        """切换卖家管理员状态"""
        try:
            # 确保telegram_id是整数
            telegram_id = int(telegram_id)
            success = toggle_seller_admin(telegram_id)
            
            if success:
                flash('卖家管理员状态已更新', 'success')
            else:
                flash('卖家管理员状态更新失败', 'error')
        except ValueError:
            flash('无效的卖家ID', 'error')
        
        return redirect(url_for('admin_sellers'))
    
    @app.route('/admin/sellers/<telegram_id>/remove', methods=['POST'])
    @admin_required
    def admin_remove_seller(telegram_id):
        """删除卖家"""
        try:
            # 确保telegram_id是整数
            telegram_id = int(telegram_id)
            success = remove_seller(telegram_id)
            
            if success:
                flash('卖家已删除', 'success')
            else:
                flash('卖家删除失败', 'error')
        except ValueError:
            flash('无效的卖家ID', 'error')
        
        return redirect(url_for('admin_sellers'))
        
    @app.route('/admin/api/stats')
    @admin_required
    def admin_api_stats():
        """获取系统统计数据"""
        try:
            # 获取订单统计
            total_orders = execute_query("SELECT COUNT(*) FROM orders", fetch=True)[0][0]
            pending_orders = execute_query(
                "SELECT COUNT(*) FROM orders WHERE status = %s OR status = %s", 
                (STATUS['SUBMITTED'], STATUS['NEED_NEW_QR']), 
                fetch=True
            )[0][0]
            completed_orders = execute_query(
                "SELECT COUNT(*) FROM orders WHERE status = %s", 
                (STATUS['CONFIRMED'],), 
                fetch=True
            )[0][0]
            
            # 获取活跃卖家数
            active_sellers = execute_query(
                "SELECT COUNT(*) FROM sellers WHERE is_active = 1",
                fetch=True
            )[0][0]
            
            return jsonify({
                'total_orders': total_orders,
                'pending_orders': pending_orders,
                'completed_orders': completed_orders,
                'active_sellers': active_sellers
            })
        except Exception as e:
            logger.error(f"获取统计数据失败: {str(e)}", exc_info=True)
            return jsonify({'message': '获取统计数据失败'}), 500
            
    @app.route('/admin/api/orders')
    @admin_required
    def admin_api_orders():
        """获取订单列表"""
        try:
            limit = request.args.get('limit', default=20, type=int)
            
            orders_raw = execute_query(
                """
                SELECT id, customer_name, package, status, message, 
                       created_at, paid_at, confirmed_at, 
                       seller_id, seller_username, seller_first_name
                FROM orders
                ORDER BY id DESC
                LIMIT %s
                """,
                (limit,),
                fetch=True
            )
            
            orders = []
            for o in orders_raw:
                orders.append({
                    "id": o[0],
                    "customer_name": o[1] or "匿名用户",
                    "package": o[2],
                    "status": o[3],
                    "status_text": STATUS_TEXT_ZH.get(o[3], o[3]),
                    "message": o[4],
                    "created_at": o[5],
                    "paid_at": o[6] or "",
                    "confirmed_at": o[7] or "",
                    "seller_id": o[8],
                    "seller_username": o[9],
                    "seller_first_name": o[10]
                })
            
            return jsonify({'orders': orders})
        except Exception as e:
            logger.error(f"获取订单列表失败: {str(e)}", exc_info=True)
            return jsonify({'message': '获取订单列表失败'}), 500 