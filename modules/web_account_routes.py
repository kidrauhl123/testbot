import logging

from flask import jsonify, redirect, render_template, request, session, url_for

from modules.web_auth_routes import login_required
from modules.database import get_balance_records, get_user_balance, get_user_credit_limit

logger = logging.getLogger(__name__)


def register_account_routes(app):
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
