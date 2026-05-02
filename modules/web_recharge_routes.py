import logging
import os
import time

from flask import request, render_template, jsonify, session

from modules.web_auth_routes import login_required
from modules.database import (
    get_user_balance,
    get_user_recharge_requests,
    create_recharge_request,
    get_pending_recharge_requests,
    approve_recharge_request,
    reject_recharge_request,
)

logger = logging.getLogger(__name__)


def register_recharge_routes(app, notification_queue, admin_required):
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
