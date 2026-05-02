import logging
from datetime import datetime, timedelta

import pytz
from flask import jsonify, request, session

from modules.constants import REASON_TEXT_ZH, STATUS, STATUS_TEXT_ZH, WEB_PRICES
from modules.web_auth_routes import login_required
from modules.database import execute_query, refund_order

logger = logging.getLogger(__name__)
CN_TIMEZONE = pytz.timezone('Asia/Shanghai')


def register_order_routes(app, notification_queue):
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
            WHERE web_user_id = %s AND status = %s
            GROUP BY package
        """, (user_id, STATUS['SUBMITTED']), fetch=True)
        
        completed_counts = execute_query("""
            SELECT package, COUNT(*) as count
            FROM orders
            WHERE web_user_id = %s AND status = %s
            GROUP BY package
        """, (user_id, STATUS['COMPLETED']), fetch=True)
        
        failed_counts = execute_query("""
            SELECT package, COUNT(*) as count
            FROM orders
            WHERE web_user_id = %s AND status = %s
            GROUP BY package
        """, (user_id, STATUS['FAILED']), fetch=True)
        
        cancelled_counts = execute_query("""
            SELECT package, COUNT(*) as count
            FROM orders
            WHERE web_user_id = %s AND status = %s
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
        limit = int(request.args.get('limit', 10))
        offset = int(request.args.get('offset', 0))
        user_filter = ""
        params = []
        
        # 非管理员只能看到自己的订单
        if not session.get('is_admin'):
            user_filter = "WHERE user_id = %s"
            params.append(session.get('user_id'))
        
        # 查询订单
        orders = execute_query(f"""
            SELECT id, account, password, package, status, created_at, accepted_at, completed_at,
                   remark, web_user_id, user_id, accepted_by, accepted_by_username, accepted_by_first_name
            FROM orders 
            {user_filter}
            ORDER BY id DESC LIMIT %s OFFSET %s
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
            WHERE id=%s
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
        execute_query("UPDATE orders SET status=%s WHERE id=%s", 
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
            WHERE id=%s
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
        execute_query("UPDATE orders SET status=%s WHERE id=%s", 
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
            WHERE id=%s
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
