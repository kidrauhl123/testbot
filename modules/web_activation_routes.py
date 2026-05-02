import logging
from datetime import datetime

from flask import request, render_template, jsonify, session

from modules.web_auth_routes import login_required
from modules.database import (
    execute_query,
    create_activation_code,
    get_admin_activation_codes,
)

logger = logging.getLogger(__name__)


def register_activation_routes(app, admin_required):
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
            conditions.append("is_used = %s")
            params.append(is_used)

        if package:
            conditions.append("package = %s")
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
            "codes": codes,
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
            code_ids_int = [int(code_id) for code_id in code_ids]
            placeholders = ','.join(['%s'] * len(code_ids_int))
            query = f"DELETE FROM activation_codes WHERE id IN ({placeholders}) AND is_used = 0"

            # 执行删除
            result = execute_query(query, code_ids_int, return_cursor=True)
            deleted_count = result.rowcount if result else 0

            logger.info(f"管理员删除了 {deleted_count} 个激活码")
            return jsonify({
                "success": True,
                "deleted_count": deleted_count,
                "message": f"成功删除 {deleted_count} 个未使用的激活码",
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
                conditions.append("is_used = %s")
                params.append(is_used)

            if package:
                conditions.append("package = %s")
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
                mimetype='text/plain',
            )
            response.headers["Content-Disposition"] = f"attachment; filename={filename}"

            logger.info(f"管理员导出了 {len(codes)} 个激活码")
            return response

        except Exception as e:
            logger.error(f"导出激活码失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "message": f"导出失败: {str(e)}"}), 500
