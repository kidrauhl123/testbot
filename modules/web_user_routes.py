import logging
from datetime import datetime

from flask import jsonify, request, session

from modules.constants import WEB_PRICES
from modules.web_auth_routes import login_required
from modules.database import (
    delete_user_custom_price,
    execute_query,
    get_user_custom_prices,
    set_user_balance,
    set_user_credit_limit,
    set_user_custom_price,
)

logger = logging.getLogger(__name__)


def register_user_routes(app, admin_required):
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
                WHERE web_user_id = %s AND created_at LIKE %s AND status = 'completed'
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
            user = execute_query("SELECT username FROM users WHERE id=%s", (user_id,), fetch=True)
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
        user = execute_query("SELECT username FROM users WHERE id=%s", (user_id,), fetch=True)
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
