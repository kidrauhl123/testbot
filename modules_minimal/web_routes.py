import os
import time
import logging
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash, send_file
from functools import wraps
from datetime import datetime
import mimetypes

from modules_minimal.database import execute_query, get_china_time
from modules_minimal.constants import STATUS

# 设置日志
logger = logging.getLogger(__name__)

def register_routes(app, notification_queue):
    """注册Web路由"""
    
    @app.route('/', methods=['GET'])
    def index():
        """显示二维码上传页面"""
        return render_template('minimal/index.html')
    
    @app.route('/', methods=['POST'])
    def create_order():
        """创建新订单 - 上传二维码"""
        try:
            # 处理上传的图片
            if 'qrcode' not in request.files:
                return jsonify({"success": False, "error": "未上传图片"}), 400
                
            file = request.files['qrcode']
            if not file or not file.filename:
                return jsonify({"success": False, "error": "文件无效"}), 400
                
            # 检查文件类型
            if not allowed_file(file.filename):
                return jsonify({"success": False, "error": "不支持的文件类型，请上传图片"}), 400
                
            # 保存文件
            try:
                # 确保上传目录存在
                current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                upload_dir = os.path.join(current_dir, 'static', 'uploads')
                
                if not os.path.exists(upload_dir):
                    os.makedirs(upload_dir)
                    
                # 生成唯一文件名
                filename = f"{int(time.time())}_{file.filename}"
                file_path = os.path.join(upload_dir, filename)
                
                # 保存文件
                file.save(file_path)
                
                # 确保URL路径正确
                file_url = f"static/uploads/{filename}"
                
            except Exception as e:
                logger.error(f"保存文件失败: {str(e)}", exc_info=True)
                return jsonify({"success": False, "error": f"保存文件失败: {str(e)}"}), 500
            
            # 创建订单记录
            timestamp = get_china_time()
            try:
                result = execute_query(
                    "INSERT INTO orders (account, status, created_at, notified) VALUES (?, ?, ?, ?)",
                    (file_url, STATUS['SUBMITTED'], timestamp, 0),
                    return_cursor=True
                )
                order_id = result.lastrowid
                result.close()
                
                logger.info(f"创建订单成功: ID={order_id}, 图片路径={file_url}")
                
                # 将订单添加到通知队列
                notification_queue.put({
                    'type': 'new_order',
                    'order_id': order_id,
                    'account': file_url,
                    'preferred_seller': None
                })
                logger.info(f"订单 #{order_id} 已添加到通知队列")
                
                return jsonify({
                    "success": True, 
                    "message": "二维码已上传，等待处理",
                    "order_id": order_id,
                    "image_url": file_url
                })
                
            except Exception as e:
                logger.error(f"创建订单失败: {str(e)}", exc_info=True)
                return jsonify({"success": False, "error": f"创建订单失败: {str(e)}"}), 500
                
        except Exception as e:
            logger.error(f"处理二维码上传请求时出错: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": f"服务器错误: {str(e)}"}), 500
    
    @app.route('/order/<int:order_id>')
    def order_status(order_id):
        """查询订单状态"""
        try:
            order = execute_query(
                "SELECT id, status, created_at, accepted_at, completed_at FROM orders WHERE id = ?",
                (order_id,),
                fetch=True
            )
            
            if not order:
                return jsonify({"success": False, "error": "订单不存在"}), 404
                
            order_data = {
                "id": order[0][0],
                "status": order[0][1],
                "created_at": order[0][2],
                "accepted_at": order[0][3],
                "completed_at": order[0][4]
            }
            
            return jsonify({"success": True, "order": order_data})
            
        except Exception as e:
            logger.error(f"查询订单状态失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": f"查询订单状态失败: {str(e)}"}), 500

def allowed_file(filename):
    """检查文件类型是否允许上传"""
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS 