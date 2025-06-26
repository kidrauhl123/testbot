import os
import sqlite3
import logging
import hashlib
from datetime import datetime
import pytz

# 设置日志
logger = logging.getLogger(__name__)

# 中国时区
CN_TIMEZONE = pytz.timezone('Asia/Shanghai')

# 获取中国时间的函数
def get_china_time():
    """获取当前中国时间（UTC+8）"""
    utc_now = datetime.now(pytz.utc)
    china_now = utc_now.astimezone(CN_TIMEZONE)
    return china_now.strftime("%Y-%m-%d %H:%M:%S")

# 数据库路径
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db")

# 初始化数据库
def init_db():
    """初始化SQLite数据库"""
    logger.info(f"初始化数据库: {DB_PATH}")
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 创建订单表 - 简化版，只包含必要字段
    c.execute('''
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account TEXT,
        status TEXT DEFAULT 'submitted',
        created_at TEXT,
        accepted_by TEXT,
        accepted_at TEXT,
        completed_at TEXT,
        notified INTEGER DEFAULT 0
    )
    ''')
    
    # 创建卖家表 - 简化版，只包含必要字段
    c.execute('''
    CREATE TABLE IF NOT EXISTS sellers (
        telegram_id TEXT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        nickname TEXT,
        is_active INTEGER DEFAULT 1
    )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("SQLite数据库初始化完成")

# 执行查询
def execute_query(query, params=(), fetch=False, return_cursor=False):
    """执行SQLite查询"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row if fetch else None
    cursor = conn.cursor()
    
    try:
        cursor.execute(query, params)
        conn.commit()
        
        if fetch:
            result = cursor.fetchall()
            if not return_cursor:
                cursor.close()
                conn.close()
            return result
        elif return_cursor:
            return cursor
        else:
            cursor.close()
            conn.close()
            return True
    except Exception as e:
        logger.error(f"执行查询出错: {str(e)}", exc_info=True)
        conn.rollback()
        if not return_cursor:
            cursor.close()
            conn.close()
        return False if not fetch else []

# 获取未通知的订单
def get_unnotified_orders():
    """获取未通知的订单"""
    return execute_query(
        "SELECT id, account, created_at FROM orders WHERE notified = 0 AND status = 'submitted'",
        fetch=True
    )

# 获取所有卖家
def get_all_sellers():
    """获取所有卖家"""
    return execute_query("SELECT telegram_id, username, first_name, nickname, is_active FROM sellers", fetch=True)

# 获取活跃卖家IDs
def get_active_seller_ids():
    """获取活跃卖家的Telegram ID列表"""
    sellers = execute_query("SELECT telegram_id FROM sellers WHERE is_active = 1", fetch=True)
    return [str(seller[0]) for seller in sellers]

# 添加卖家
def add_seller(telegram_id, username, first_name, nickname):
    """添加卖家"""
    execute_query(
        "INSERT OR REPLACE INTO sellers (telegram_id, username, first_name, nickname, is_active) VALUES (?, ?, ?, ?, 1)",
        (str(telegram_id), username, first_name, nickname)
    )

# 切换卖家状态
def toggle_seller_status(telegram_id):
    """切换卖家的活跃状态"""
    execute_query(
        "UPDATE sellers SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE telegram_id = ?",
        (str(telegram_id),)
    )

# 获取订单详情
def get_order_details(oid):
    return execute_query("SELECT id, account, password, package, status, remark FROM orders WHERE id = ?", (oid,), fetch=True)

# 获取未通知订单
def get_unnotified_orders():
    """获取未通知的订单"""
    orders = execute_query("""
        SELECT id, account, password, package, created_at, web_user_id, remark 
        FROM orders 
        WHERE notified = 0 AND status = ?
    """, (STATUS['SUBMITTED'],), fetch=True)
    
    # 记录获取到的未通知订单
    if orders:
        logger.info(f"获取到 {len(orders)} 个未通知订单")
    
    return orders

# 获取订单详情
def get_order_details(oid):
    return execute_query("SELECT id, account, password, package, status, remark FROM orders WHERE id = ?", (oid,), fetch=True)

# 获取所有卖家
def get_all_sellers():
    """获取所有卖家信息"""
    return execute_query("SELECT telegram_id, username, first_name, nickname, is_active FROM sellers", fetch=True)

# 获取活跃卖家IDs
def get_active_seller_ids():
    """获取所有活跃的卖家Telegram ID"""
    sellers = execute_query("SELECT telegram_id FROM sellers WHERE is_active = 1", fetch=True)
    return [seller[0] for seller in sellers]

# 获取活跃的卖家的ID和昵称
def get_active_sellers():
    """获取所有活跃的卖家的ID和昵称"""
    sellers = execute_query("""
        SELECT telegram_id, nickname, username, first_name, 
               last_active_at, desired_orders
        FROM sellers 
        WHERE is_active = 1
    """, fetch=True)
    
    result = []
    for seller in sellers:
        telegram_id, nickname, username, first_name, last_active_at, desired_orders = seller
        # 如果没有设置昵称，则使用first_name或username作为默认昵称
        display_name = nickname or first_name or f"卖家 {telegram_id}"
        result.append({
            "id": telegram_id,
            "name": display_name,
            "last_active_at": last_active_at or "",
            "desired_orders": desired_orders or 0
        })
    return result

# 添加卖家
def add_seller(telegram_id, username, first_name, nickname, added_by):
    """添加新卖家"""
    timestamp = get_china_time()
    execute_query(
        "INSERT INTO sellers (telegram_id, username, first_name, nickname, added_at, added_by) VALUES (?, ?, ?, ?, ?, ?)",
        (telegram_id, username, first_name, nickname, timestamp, added_by)
    )

# 切换卖家状态
def toggle_seller_status(telegram_id):
    """切换卖家活跃状态"""
    execute_query("UPDATE sellers SET is_active = NOT is_active WHERE telegram_id = ?", (telegram_id,))

# 移除卖家
def remove_seller(telegram_id):
    """移除卖家"""
    return execute_query("DELETE FROM sellers WHERE telegram_id=?", (telegram_id,))

# 获取用户余额
def get_user_balance(user_id):
    """获取用户余额"""
    result = execute_query("SELECT balance FROM users WHERE id=?", (user_id,), fetch=True)
    
    if result:
        return result[0][0]
    return 0

# 获取用户透支额度
def get_user_credit_limit(user_id):
    """获取用户透支额度"""
    result = execute_query("SELECT credit_limit FROM users WHERE id=?", (user_id,), fetch=True)
    
    if result:
        return result[0][0]
    return 0

# 设置用户透支额度
def set_user_credit_limit(user_id, credit_limit):
    """设置用户透支额度（仅限管理员使用）"""
    # 确保透支额度不为负
    if credit_limit < 0:
        credit_limit = 0
    
    execute_query("UPDATE users SET credit_limit=? WHERE id=?", (credit_limit, user_id))
    
    return True, credit_limit

# 获取余额变动记录
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
                WHERE br.user_id = ?
                ORDER BY br.id DESC
                LIMIT ? OFFSET ?
            """, (user_id, limit, offset), fetch=True)
        else:
            # 管理员查看所有记录
            records = execute_query("""
                SELECT br.id, br.user_id, u.username, br.amount, br.type, br.reason, br.reference_id, br.balance_after, br.created_at
                FROM balance_records br
                JOIN users u ON br.user_id = u.id
                ORDER BY br.id DESC
                LIMIT ? OFFSET ?
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

# 更新用户余额
def update_user_balance(user_id, amount):
    """更新用户余额（增加或减少）"""
    # 获取当前余额
    current_balance = get_user_balance(user_id)
    new_balance = current_balance + amount
    
    # 获取透支额度
    credit_limit = get_user_credit_limit(user_id)
    
    # 确保余额+透支额度不会变成负数
    if new_balance < -credit_limit:
        return False, "余额和透支额度不足"
    
    # 使用事务处理
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        # 更新余额
        c.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user_id))
        
        # 记录余额变动
        type_name = 'recharge' if amount > 0 else 'consume'
        reason = '手动调整余额' if amount > 0 else '消费'
        now = get_china_time()
        
        c.execute("""
            INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, amount, type_name, reason, None, new_balance, now))
        
        conn.commit()
        conn.close()
        return True, new_balance
    except Exception as e:
        logger.error(f"更新用户余额失败: {str(e)}", exc_info=True)
        return False, f"更新用户余额失败: {str(e)}"

# 检查用户余额是否足够购买指定套餐
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

# 退款订单金额到用户余额
def refund_order(order_id):
    """退款订单金额到用户余额 (兼容SQLite/PostgreSQL)"""
    # 先读取订单信息（使用 execute_query，自动选择数据库）
    order = execute_query(
        "SELECT id, user_id, package, status, refunded FROM orders WHERE id = ?",
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

    try:
        # 更新余额
        execute_query("UPDATE users SET balance = balance + ? WHERE id = ?", (price, user_id))
        execute_query("UPDATE orders SET refunded = 1 WHERE id = ?", (order_id,))
        
        # 记录余额变动
        now = get_china_time()
        execute_query("""
            INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, price, 'refund', f'订单退款: #{order_id}', order_id, get_user_balance(user_id) + price, now))
        
        return True, get_user_balance(user_id) + price
    except Exception as e:
        logger.error(f"退款到用户余额失败: {str(e)}", exc_info=True)
        return False, str(e)

# 获取用户定制价格
def get_user_custom_prices(user_id):
    """
    获取用户的定制价格
    
    参数:
    - user_id: 用户ID
    
    返回:
    - 用户定制价格的字典，键为套餐（如'1'），值为价格
    """
    results = execute_query("""
        SELECT package, price FROM user_custom_prices
        WHERE user_id = ?
    """, (user_id,), fetch=True)
    
    if not results:
        return {}
        
    custom_prices = {}
    for package, price in results:
        custom_prices[package] = price
        
    return custom_prices

# 设置用户定制价格
def set_user_custom_price(user_id, package, price, admin_id):
    """
    设置用户的定制价格
    
    参数:
    - user_id: 用户ID
    - package: 套餐（如'1'，'2'等）
    - price: 价格
    - admin_id: 设置价格的管理员ID
    
    返回:
    - 成功返回True，失败返回False
    """
    now = get_china_time()
    
    # 检查是否已存在该用户的该套餐定制价格
    existing = execute_query("""
        SELECT id FROM user_custom_prices
        WHERE user_id = ? AND package = ?
    """, (user_id, package), fetch=True)
    
    if existing:
        # 更新已有价格
        execute_query("""
            UPDATE user_custom_prices
            SET price = ?, created_at = ?, created_by = ?
            WHERE user_id = ? AND package = ?
        """, (price, now, admin_id, user_id, package))
    else:
        # 添加新价格
        execute_query("""
            INSERT INTO user_custom_prices
            (user_id, package, price, created_at, created_by)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, package, price, now, admin_id))
    
    return True

# 删除用户定制价格
def delete_user_custom_price(user_id, package):
    """
    删除用户的定制价格
    
    参数:
    - user_id: 用户ID
    - package: 套餐（如'1'，'2'等）
    
    返回:
    - 成功返回True，失败返回False
    """
    execute_query("""
        DELETE FROM user_custom_prices
        WHERE user_id = ? AND package = ?
    """, (user_id, package))
    return True

# 更新卖家昵称
def update_seller_nickname(telegram_id, nickname):
    """更新卖家的显示昵称"""
    execute_query(
        "UPDATE sellers SET nickname = ? WHERE telegram_id = ?",
        (nickname, telegram_id)
    )

# 更新卖家最后活跃时间
def update_seller_last_active(telegram_id):
    """更新卖家最后活跃时间"""
    timestamp = get_china_time()
    execute_query(
        "UPDATE sellers SET last_active_at = ? WHERE telegram_id = ?",
        (timestamp, telegram_id)
    )

# 更新卖家期望接单数量
def update_seller_desired_orders(telegram_id, desired_orders):
    """更新卖家期望接单数量"""
    execute_query(
        "UPDATE sellers SET desired_orders = ? WHERE telegram_id = ?",
        (desired_orders, telegram_id)
    )

# 向卖家发送活跃度检查请求
def check_seller_activity(telegram_id):
    """向卖家发送活跃度检查请求"""
    # 记录检查请求时间
    timestamp = get_china_time()
    execute_query(
        "UPDATE sellers SET activity_check_at = ? WHERE telegram_id = ?",
        (timestamp, telegram_id)
    )
    return True 