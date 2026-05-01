import logging

from modules.db_core import execute_query, get_postgres_connection
from modules.order_balance import get_china_time

logger = logging.getLogger(__name__)


# ===== 充值相关函数 =====
def create_recharge_tables():
    """创建充值记录表和余额明细表"""
    try:
        execute_query("""
            CREATE TABLE IF NOT EXISTS recharge_requests (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL,
                payment_method TEXT NOT NULL,
                proof_image TEXT,
                details TEXT,
                created_at TEXT NOT NULL,
                processed_at TEXT,
                processed_by TEXT,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        logger.info("已确认充值记录表")

        execute_query("""
            CREATE TABLE IF NOT EXISTS balance_records (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                type TEXT NOT NULL,
                reason TEXT NOT NULL,
                reference_id INTEGER,
                balance_after REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        """)
        logger.info("已确认余额明细表")
        return True
    except Exception as e:
        logger.error(f"创建充值记录表或余额明细表失败: {str(e)}", exc_info=True)
        return False

def create_recharge_request(user_id, amount, payment_method, proof_image, details=None):
    """创建充值请求"""
    try:
        now = get_china_time()
        result = execute_query("""
            INSERT INTO recharge_requests (user_id, amount, status, payment_method, proof_image, details, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (user_id, amount, 'pending', payment_method, proof_image, details, now), fetch=True)
        request_id = result[0][0]
        return request_id, True, "充值请求已提交"
    except Exception as e:
        logger.error(f"创建充值请求失败: {str(e)}", exc_info=True)
        return None, False, f"创建充值请求失败: {str(e)}"

def get_user_recharge_requests(user_id):
    """获取用户的充值请求记录"""
    try:
        requests = execute_query("""
            SELECT id, amount, status, payment_method, proof_image, created_at, processed_at, details
            FROM recharge_requests
            WHERE user_id = %s
            ORDER BY created_at DESC
        """, (user_id,), fetch=True)
        return requests
    except Exception as e:
        logger.error(f"获取用户充值请求失败: {str(e)}", exc_info=True)
        return []

def get_pending_recharge_requests():
    """获取所有待处理的充值请求"""
    try:
        requests = execute_query("""
            SELECT r.id, r.user_id, r.amount, r.payment_method, r.proof_image, r.created_at, u.username, r.details
            FROM recharge_requests r
            JOIN users u ON r.user_id = u.id
            WHERE r.status = %s
            ORDER BY r.created_at ASC
        """, ('pending',), fetch=True)
        return requests
    except Exception as e:
        logger.error(f"获取待处理充值请求失败: {str(e)}", exc_info=True)
        return []

def approve_recharge_request(request_id, admin_id):
    """批准充值请求并增加用户余额"""
    try:
        conn = get_postgres_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            cursor.execute("""
                SELECT user_id, amount
                FROM recharge_requests
                WHERE id = %s AND status = %s
                FOR UPDATE
            """, (request_id, 'pending'))
            request = cursor.fetchone()
            if not request:
                conn.rollback()
                return False, "充值请求不存在或已处理"

            user_id, amount = request
            now = get_china_time()
            cursor.execute("""
                UPDATE recharge_requests
                SET status = %s, processed_at = %s, processed_by = %s
                WHERE id = %s AND status = %s
            """, ('approved', now, admin_id, request_id, 'pending'))

            cursor.execute("""
                UPDATE users
                SET balance = balance + %s
                WHERE id = %s
                RETURNING balance
            """, (amount, user_id))
            new_balance = cursor.fetchone()[0]

            cursor.execute("""
                INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (user_id, amount, 'recharge', f'充值: 请求#{request_id}', request_id, new_balance, now))
            conn.commit()
            return True, f"已成功批准充值 {amount} 元"
        except Exception as e:
            conn.rollback()
            logger.error(f"批准充值请求失败: {str(e)}", exc_info=True)
            return False, f"批准充值请求失败: {str(e)}"
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"批准充值请求失败: {str(e)}", exc_info=True)
        return False, f"批准充值请求失败: {str(e)}"

def reject_recharge_request(request_id, admin_id):
    """拒绝充值请求"""
    try:
        now = get_china_time()
        execute_query("""
            UPDATE recharge_requests
            SET status = %s, processed_at = %s, processed_by = %s
            WHERE id = %s AND status = %s
        """, ('rejected', now, admin_id, request_id, 'pending'))
        return True, "已拒绝充值请求"
    except Exception as e:
        logger.error(f"拒绝充值请求失败: {str(e)}", exc_info=True)
        return False, f"拒绝充值请求失败: {str(e)}"
