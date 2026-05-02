from flask import jsonify, request, session

from modules.web_auth_routes import login_required
from modules.database import (
    get_all_sellers,
    add_seller,
    remove_seller,
    toggle_seller_status,
    toggle_seller_admin,
)


def register_seller_routes(app, admin_required):
    @app.route('/admin/api/sellers', methods=['GET'])
    @login_required
    @admin_required
    def admin_api_get_sellers():
        sellers = get_all_sellers()
        return jsonify([{
            "telegram_id": s[0], "username": s[1], "first_name": s[2],
            "is_active": s[3], "added_at": s[4], "added_by": s[5]
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
            session['username']
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
