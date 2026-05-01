import logging

from modules.db_core import execute_query
from modules.order_balance import get_china_time

logger = logging.getLogger(__name__)


def get_user_custom_prices(user_id):
    """获取用户的定制价格，返回 {package: price}。"""
    try:
        results = execute_query("""
            SELECT package, price FROM user_custom_prices
            WHERE user_id = %s
        """, (user_id,), fetch=True)

        if not results:
            return {}

        return {package: price for package, price in results}
    except Exception as e:
        logger.error(f"获取用户定制价格失败: {str(e)}", exc_info=True)
        return {}


def set_user_custom_price(user_id, package, price, admin_id):
    """设置或更新用户的定制价格。"""
    try:
        now = get_china_time()
        existing = execute_query("""
            SELECT id FROM user_custom_prices
            WHERE user_id = %s AND package = %s
        """, (user_id, package), fetch=True)

        if existing:
            execute_query("""
                UPDATE user_custom_prices
                SET price = %s, created_at = %s, created_by = %s
                WHERE user_id = %s AND package = %s
            """, (price, now, admin_id, user_id, package))
        else:
            execute_query("""
                INSERT INTO user_custom_prices
                (user_id, package, price, created_at, created_by)
                VALUES (%s, %s, %s, %s, %s)
            """, (user_id, package, price, now, admin_id))

        return True
    except Exception as e:
        logger.error(f"设置用户定制价格失败: {str(e)}", exc_info=True)
        return False


def delete_user_custom_price(user_id, package):
    """删除用户的定制价格。"""
    try:
        execute_query("""
            DELETE FROM user_custom_prices
            WHERE user_id = %s AND package = %s
        """, (user_id, package))
        return True
    except Exception as e:
        logger.error(f"删除用户定制价格失败: {str(e)}", exc_info=True)
        return False
