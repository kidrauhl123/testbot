import logging
from datetime import datetime

import pytz

from modules.constants import STATUS
from modules.db_core import execute_query, get_postgres_connection

logger = logging.getLogger(__name__)

CN_TIMEZONE = pytz.timezone('Asia/Shanghai')


# 获取中国时间的函数
def get_china_time():
    """获取当前中国时间（UTC+8）"""
    utc_now = datetime.now(pytz.utc)
    china_now = utc_now.astimezone(CN_TIMEZONE)
    return china_now.strftime("%Y-%m-%d %H:%M:%S")

def add_balance_record(user_id, amount, type_name, reason, reference_id=None, balance_after=None):
    """
    添加余额变动记录
    
    参数:
    - user_id: 用户ID
    - amount: 变动金额（正数表示收入，负数表示支出）
    - type_name: 类型（'recharge'-充值, 'consume'-消费, 'refund'-退款）
    - reason: 原因描述
    - reference_id: 关联的ID（如订单ID或充值请求ID）
    - balance_after: 变动后余额，如果不提供会自动获取当前余额
    
    返回:
    - 记录ID
    """
    try:
        # 如果未提供变动后余额，则获取当前余额
        if balance_after is None:
            balance_after = get_user_balance(user_id)
            
        now = get_china_time()
        
        # 添加记录
        result = execute_query("""
            INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (user_id, amount, type_name, reason, reference_id, balance_after, now), fetch=True)
        return result[0][0]
    except Exception as e:
        logger.error(f"添加余额变动记录失败: {str(e)}", exc_info=True)
        return None

# 获取未通知订单
def get_unnotified_orders():
    """获取未通知的订单"""
    orders = execute_query("""
        SELECT id, account, password, package, created_at, web_user_id 
        FROM orders 
        WHERE notified = 0 AND status = %s
    """, (STATUS['SUBMITTED'],), fetch=True)
    
    # 记录获取到的未通知订单
    if orders:
        logger.info(f"获取到 {len(orders)} 个未通知订单")
    
    return orders

# 接单原子操作
def accept_order_atomic(oid, user_id):
    """原子接单；Postgres-only。"""
    conn = get_postgres_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT status FROM orders WHERE id = %s FOR UPDATE", (oid,))
        order = cursor.fetchone()
        if not order:
            conn.rollback()
            return False, "Order not found"

        if order[0] == 'cancelled':
            conn.rollback()
            return False, "Order has been cancelled"

        if order[0] != 'submitted':
            conn.rollback()
            return False, "Order already taken"

        cursor.execute("""
            SELECT COUNT(*) FROM orders
            WHERE accepted_by = %s AND status = 'disputing'
        """, (str(user_id),))
        disputing_count = cursor.fetchone()[0]
        if disputing_count > 0:
            conn.rollback()
            return False, "You have a disputed order. Please resolve it before accepting new orders."

        cursor.execute("""
            SELECT COUNT(*) FROM orders
            WHERE accepted_by = %s AND status = 'accepted'
        """, (str(user_id),))
        active_count = cursor.fetchone()[0]
        if active_count >= 3:
            conn.rollback()
            return False, "You already have 3 active orders. Please complete your current orders first before accepting new ones."

        from modules.constants import user_info_cache
        cached_user = user_info_cache.get(user_id, {})
        username = cached_user.get('username')
        first_name = cached_user.get('first_name')
        last_name = cached_user.get('last_name', '')
        full_name = None
        if first_name:
            full_name = f"{first_name} {last_name}".strip() if last_name else first_name

        timestamp = get_china_time()
        cursor.execute(
            """
            UPDATE orders
            SET status = 'accepted',
                accepted_at = %s,
                accepted_by = %s,
                accepted_by_username = %s,
                accepted_by_first_name = %s
            WHERE id = %s
            """,
            (timestamp, str(user_id), username, full_name, oid),
        )

        conn.commit()
        return True, "Success"

    except Exception as e:
        conn.rollback()
        logger.error(f"Error in accept_order_atomic: {str(e)}")
        return False, "Database error"
    finally:
        conn.close()

# 获取订单详情
def get_order_details(oid):
    return execute_query(
        "SELECT id, account, password, package, status, remark FROM orders WHERE id = %s",
        (oid,),
        fetch=True,
    )

# ===== 余额系统相关函数 =====
def get_user_balance(user_id):
    """获取用户余额"""
    result = execute_query("SELECT balance FROM users WHERE id=%s", (user_id,), fetch=True)
    
    if result:
        return result[0][0]
    return 0

def get_user_credit_limit(user_id):
    """获取用户透支额度"""
    result = execute_query("SELECT credit_limit FROM users WHERE id=%s", (user_id,), fetch=True)
    
    if result:
        return result[0][0]
    return 0

def set_user_credit_limit(user_id, credit_limit):
    """设置用户透支额度（仅限管理员使用）"""
    # 确保透支额度不为负
    if credit_limit < 0:
        credit_limit = 0
    
    execute_query("UPDATE users SET credit_limit=%s WHERE id=%s", (credit_limit, user_id))
    
    return True, credit_limit

def get_balance_records(user_id=None, limit=50, offset=0):
    """
    获取余额变动记录
    
    参数:
    - user_id: 用户ID，如果不提供则获取所有用户的记录（仅限管理员）
    - limit: 最大记录数
    - offset: 偏移量
    
    返回:
    - 记录列表
    """
    try:
        if user_id:
            records = execute_query("""
                SELECT br.id, br.user_id, u.username, br.amount, br.type, br.reason, br.reference_id, br.balance_after, br.created_at
                FROM balance_records br
                JOIN users u ON br.user_id = u.id
                WHERE br.user_id = %s
                ORDER BY br.id DESC
                LIMIT %s OFFSET %s
            """, (user_id, limit, offset), fetch=True)
        else:
            # 管理员查看所有记录
            records = execute_query("""
                SELECT br.id, br.user_id, u.username, br.amount, br.type, br.reason, br.reference_id, br.balance_after, br.created_at
                FROM balance_records br
                JOIN users u ON br.user_id = u.id
                ORDER BY br.id DESC
                LIMIT %s OFFSET %s
            """, (limit, offset), fetch=True)
        
        # 格式化记录
        formatted_records = []
        for record in records:
            formatted_records.append({
                'id': record[0],
                'user_id': record[1],
                'username': record[2],
                'amount': record[3],
                'type': record[4],
                'reason': record[5],
                'reference_id': record[6],
                'balance_after': record[7],
                'created_at': record[8]
            })
        
        return formatted_records
    except Exception as e:
        logger.error(f"获取余额变动记录失败: {str(e)}", exc_info=True)
        return []

def update_user_balance(user_id, amount):
    """更新用户余额（增加或减少）"""
    current_balance = get_user_balance(user_id)
    new_balance = current_balance + amount
    credit_limit = get_user_credit_limit(user_id)
    if new_balance < -credit_limit:
        return False, "余额和透支额度不足"
    
    conn = None
    try:
        conn = get_postgres_connection()
        with conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE users 
                SET balance = %s 
                WHERE id = %s
                RETURNING balance
            """, (new_balance, user_id))
            result = cursor.fetchone()
            if not result:
                logger.error(f"更新用户余额失败: 用户ID={user_id}不存在")
                return False, "用户不存在"
            updated_balance = result[0]
            type_name = 'recharge' if amount > 0 else 'consume'
            reason = '手动调整余额' if amount > 0 else '消费'
            now = get_china_time()
            cursor.execute("""
                INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (user_id, amount, type_name, reason, None, updated_balance, now))
        return True, updated_balance
    except Exception as e:
        logger.error(f"更新用户余额失败: {str(e)}", exc_info=True)
        return False, f"更新用户余额失败: {str(e)}"
    finally:
        if conn:
            conn.close()

def set_user_balance(user_id, balance):
    """设置用户余额（仅限管理员使用）"""
    current_balance = get_user_balance(user_id)
    change_amount = balance - current_balance
    if balance < 0:
        balance = 0
        change_amount = -current_balance
    if change_amount == 0:
        return True, balance
    
    conn = None
    try:
        conn = get_postgres_connection()
        with conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE users 
                SET balance = %s 
                WHERE id = %s
                RETURNING balance
            """, (balance, user_id))
            result = cursor.fetchone()
            if not result:
                logger.error(f"设置用户余额失败: 用户ID={user_id}不存在")
                return False, "用户不存在"
            updated_balance = result[0]
            if change_amount != 0:
                type_name = 'recharge' if change_amount > 0 else 'consume'
                now = get_china_time()
                cursor.execute("""
                    INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (user_id, change_amount, type_name, '管理员调整余额', None, updated_balance, now))
        return True, updated_balance
    except Exception as e:
        logger.error(f"设置用户余额失败: {str(e)}", exc_info=True)
        return False, f"设置用户余额失败: {str(e)}"
    finally:
        if conn:
            conn.close()

def check_balance_for_package(user_id, package):
    """检查用户余额是否足够购买指定套餐"""
    from modules.constants import WEB_PRICES
    
    # 获取套餐价格
    price = WEB_PRICES.get(package, 0)
    
    # 获取用户余额
    balance = get_user_balance(user_id)
    
    # 获取用户透支额度
    credit_limit = get_user_credit_limit(user_id)
    
    # 判断余额+透支额度是否足够
    if balance + credit_limit >= price:
        return True, balance, price, credit_limit
    else:
        return False, balance, price, credit_limit

def refund_order(order_id):
    """退款订单金额到用户余额。"""
    order = execute_query(
        "SELECT id, user_id, package, status, refunded FROM orders WHERE id = %s",
        (order_id,), fetch=True)

    if not order:
        logger.warning(f"退款失败: 找不到订单ID={order_id}")
        return False, "找不到订单"

    order_id, user_id, package, status, refunded_flag = order[0]

    # 只有已撤销或充值失败的订单才能退款
    if status not in ['cancelled', 'failed']:
        logger.warning(f"退款失败: 订单状态不是已撤销或充值失败 (ID={order_id}, 状态={status})")
        return False, f"订单状态不允许退款: {status}"

    if refunded_flag:
        logger.warning(f"退款失败: 订单已退款 (ID={order_id})")
        return False, "订单已退款"

    from modules.constants import WEB_PRICES
    price = WEB_PRICES.get(package, 0)
    if price <= 0:
        logger.warning(f"退款失败: 套餐价格无效 (ID={order_id}, 套餐={package}, 价格={price})")
        return False, "套餐价格无效"

    conn = None
    try:
        conn = get_postgres_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN")
            # 获取当前余额（FOR UPDATE 锁行）
            cursor.execute("SELECT balance FROM users WHERE id = %s FOR UPDATE", (user_id,))
            current_balance = cursor.fetchone()[0]
            new_balance = current_balance + price
            # 更新余额
            cursor.execute("UPDATE users SET balance = %s WHERE id = %s", (new_balance, user_id))
            # 标记订单已退款
            cursor.execute("UPDATE orders SET refunded = 1 WHERE id = %s", (order_id,))
            # 插入余额记录
            now = get_china_time()
            cursor.execute(
                """
                INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, price, 'refund', f'订单退款: #{order_id}', order_id, new_balance, now)
            )
            conn.commit()
            logger.info(f"订单退款成功: ID={order_id}, 用户ID={user_id}, 金额={price}, 新余额={new_balance}")
            return True, new_balance
        except Exception as e:
            conn.rollback()
            logger.error(f"退款到用户余额失败: {str(e)}", exc_info=True)
            return False, str(e)
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"退款到用户余额失败: {str(e)}", exc_info=True)
        return False, str(e)

def create_order_with_deduction_atomic(account, password, package, remark, username, user_id):
    """
    使用事务原子性地创建订单并扣除用户余额。
    
    返回:
    - (success, message, new_balance, credit_limit)
    """
    from modules.constants import get_user_package_price

    conn = None
    try:
        conn = get_postgres_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("BEGIN")

            # 查询余额和额度
            cursor.execute("SELECT balance, credit_limit FROM users WHERE id = %s FOR UPDATE", (user_id,))
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return False, "用户不存在", None, None

            current_balance, credit_limit = row
            available_funds = current_balance + credit_limit

            price = get_user_package_price(user_id, package)
            if price > available_funds:
                conn.rollback()
                return False, f"余额不足，需要 {price} 元，可用 {available_funds} 元", current_balance, credit_limit

            # 扣款并更新余额
            new_balance = current_balance - price
            cursor.execute("UPDATE users SET balance = %s WHERE id = %s", (new_balance, user_id))

            # 记录余额变动
            now = get_china_time()
            cursor.execute(
                """
                INSERT INTO balance_records (user_id, amount, type, reason, balance_after, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (user_id, -price, 'consume', f'购买{package}个月套餐', new_balance, now)
            )

            # 创建订单记录
            cursor.execute(
                """
                INSERT INTO orders (account, password, package, status, created_at, remark, user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (account, password, package, 'submitted', now, remark, user_id)
            )

            conn.commit()
            return True, "订单创建成功", new_balance, credit_limit
        except Exception as e:
            conn.rollback()
            logger.error(f"创建订单失败: {str(e)}", exc_info=True)
            return False, f"创建订单失败: {str(e)}", None, None
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"创建订单时数据库连接失败: {str(e)}", exc_info=True)
        return False, f"数据库连接失败: {str(e)}", None, None
