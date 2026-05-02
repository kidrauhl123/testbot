import logging

from flask import jsonify, request, session

from modules.constants import STATUS, STATUS_TEXT_ZH
from modules.web_auth_routes import login_required
from modules.database import execute_query, get_postgres_connection, refund_order

logger = logging.getLogger(__name__)


def register_order_admin_routes(app, admin_required):
    @app.route('/admin/api/orders')
    @login_required
    @admin_required
    def admin_api_orders():
        """获取所有订单"""
        # 获取查询参数
        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))
        status = request.args.get('status')
        search = request.args.get('search', '')
        
        # 构建查询条件
        conditions = []
        params = []
        
        if status:
            conditions.append("status = %s")
            params.append(status)
        
        if search:
            conditions.append("(account LIKE %s OR web_user_id LIKE %s OR id LIKE %s)")
            search_param = f"%{search}%"
            params.extend([search_param, search_param, search_param])
        
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        
        # 查询订单
        orders = execute_query(f"""
            SELECT id, account, password, package, status, remark, created_at, accepted_at, completed_at, 
                   web_user_id as creator, accepted_by, accepted_by_username, accepted_by_first_name, refunded
            FROM orders
            {where_clause}
            ORDER BY id DESC
            LIMIT %s OFFSET %s
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
                "creator": creator,
                "seller": seller_info,
                "refunded": bool(refunded)
            })
        
        return jsonify({
            "orders": formatted_orders,
            "total": count
        })

    # 获取单个订单详情的API
    @app.route('/admin/api/orders/<int:order_id>')
    @login_required
    @admin_required
    def admin_api_order_detail(order_id):
        """获取单个订单的详细信息"""
        order = execute_query("""
            SELECT id, account, password, package, status, remark, created_at, 
                   accepted_at, completed_at, accepted_by, web_user_id, user_id,
                   accepted_by_username, accepted_by_first_name
            FROM orders 
            WHERE id = %s
        """, (order_id,), fetch=True)
        
        if not order:
            return jsonify({"error": "订单不存在"}), 404
            
        o = order[0]
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
        order = execute_query("SELECT status, user_id, package, refunded FROM orders WHERE id=%s", (order_id,), fetch=True)
        if not order:
            return jsonify({"error": "订单不存在"}), 404
        
        current_status, user_id, current_package, refunded = order[0]
        
        # 获取新状态
        new_status = data.get('status')
        
        # 更新订单信息
        execute_query("""
            UPDATE orders 
            SET account=%s, password=%s, package=%s, status=%s, remark=%s 
            WHERE id=%s
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
                # 全部删除，直接 truncate 并重置自增 ID
                conn = get_postgres_connection()
                cur = conn.cursor()
                try:
                    cur.execute("TRUNCATE TABLE orders RESTART IDENTITY;")
                    conn.commit()
                finally:
                    cur.close()
                    conn.close()
                deleted_count = total_count
            else:
                # 普通批量删除
                order_ids_int = [int(oid) for oid in order_ids]
                placeholders = ','.join(['%s'] * len(order_ids_int))
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
