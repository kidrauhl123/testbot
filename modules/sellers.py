import hashlib
import logging

from modules.db_core import execute_query
from modules.order_balance import get_china_time

logger = logging.getLogger(__name__)


# ===== 密码加密 =====
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()


# ===== 卖家管理 =====
def get_all_sellers():
    """获取所有卖家信息"""
    return execute_query("""
        SELECT telegram_id, username, first_name, is_active,
               added_at, added_by,
               COALESCE(is_admin, FALSE) as is_admin
        FROM sellers
        ORDER BY added_at DESC
    """, fetch=True)


def get_active_seller_ids():
    """获取所有活跃的卖家Telegram ID"""
    sellers = execute_query("SELECT telegram_id FROM sellers WHERE is_active = TRUE", fetch=True)
    return [seller[0] for seller in sellers]


def add_seller(telegram_id, username, first_name, added_by):
    """添加新卖家"""
    timestamp = get_china_time()
    execute_query(
        "INSERT INTO sellers (telegram_id, username, first_name, added_at, added_by) VALUES (%s, %s, %s, %s, %s)",
        (telegram_id, username, first_name, timestamp, added_by)
    )


def toggle_seller_status(telegram_id):
    """切换卖家活跃状态"""
    execute_query("UPDATE sellers SET is_active = NOT is_active WHERE telegram_id = %s", (telegram_id,))


def remove_seller(telegram_id):
    """移除卖家"""
    return execute_query("DELETE FROM sellers WHERE telegram_id=%s", (telegram_id,))


def toggle_seller_admin(telegram_id):
    """切换卖家的管理员状态"""
    try:
        # 先获取当前状态
        current = execute_query(
            "SELECT COALESCE(is_admin, FALSE) FROM sellers WHERE telegram_id = %s",
            (telegram_id,),
            fetch=True
        )

        if not current:
            return False

        new_status = not bool(current[0][0])

        execute_query(
            "UPDATE sellers SET is_admin = %s WHERE telegram_id = %s",
            (new_status, telegram_id)
        )
        return True
    except Exception as e:
        logger.error(f"切换卖家管理员状态失败: {e}")
        return False


def is_admin_seller(telegram_id):
    """检查卖家是否是管理员"""
    result = execute_query(
        "SELECT COALESCE(is_admin, FALSE) FROM sellers WHERE telegram_id = %s AND is_active = TRUE",
        (telegram_id,),
        fetch=True
    )
    return bool(result and result[0][0])
