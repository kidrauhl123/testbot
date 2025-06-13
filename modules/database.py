import os
import time
import sqlite3
import hashlib
import logging
import psycopg2
from functools import wraps
from datetime import datetime
from urllib.parse import urlparse
import pytz

from modules.constants import DATABASE_URL, STATUS, ADMIN_USERNAME, ADMIN_PASSWORD

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

# ===== 数据库 =====
def init_db():
    """根据环境配置初始化数据库"""
    logger.info(f"初始化数据库，使用连接: {DATABASE_URL[:10]}...")
    if DATABASE_URL.startswith('postgres'):
        init_postgres_db()
    else:
        init_sqlite_db()
    
    # 创建充值记录表
    logger.info("正在创建充值记录表...")
    create_recharge_tables()
    logger.info("充值记录表创建完成")
        
def init_sqlite_db():
    """初始化SQLite数据库"""
    logger.info("使用SQLite数据库")
    # 使用绝对路径访问数据库
    current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(current_dir, "orders.db")
    logger.info(f"初始化数据库: {db_path}")
    
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    # 订单表
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            password TEXT NOT NULL,
            package TEXT NOT NULL,
            remark TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            accepted_at TEXT,
            completed_at TEXT,
            accepted_by TEXT,
            accepted_by_username TEXT,
            accepted_by_first_name TEXT,
            notified INTEGER DEFAULT 0,
            web_user_id TEXT,
            user_id INTEGER,
            refunded INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    
    # 用户表
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login TEXT,
            balance REAL DEFAULT 0,
            credit_limit REAL DEFAULT 0
        )
    """)
    
    # 卖家表
    c.execute("""
        CREATE TABLE IF NOT EXISTS sellers (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            is_active INTEGER DEFAULT 1,
            added_at TEXT NOT NULL,
            added_by TEXT
        )
    """)
    
    # 检查orders表中是否需要添加新列
    c.execute("PRAGMA table_info(orders)")
    orders_columns = [column[1] for column in c.fetchall()]
    
    if 'user_id' not in orders_columns:
        logger.info("为orders表添加user_id列")
        c.execute("ALTER TABLE orders ADD COLUMN user_id INTEGER")
    
    # 检查是否需要添加refunded列（是否已退款）
    if 'refunded' not in orders_columns:
        logger.info("为orders表添加refunded列")
        c.execute("ALTER TABLE orders ADD COLUMN refunded INTEGER DEFAULT 0")
    
    # 检查是否需要添加accepted_by_username列（Telegram用户名）
    if 'accepted_by_username' not in orders_columns:
        logger.info("为orders表添加accepted_by_username列")
        c.execute("ALTER TABLE orders ADD COLUMN accepted_by_username TEXT")
    
    # 检查是否需要添加accepted_by_first_name列（Telegram昵称）
    if 'accepted_by_first_name' not in orders_columns:
        logger.info("为orders表添加accepted_by_first_name列")
        c.execute("ALTER TABLE orders ADD COLUMN accepted_by_first_name TEXT")
    
    # 检查users表中是否需要添加新列
    c.execute("PRAGMA table_info(users)")
    users_columns = [column[1] for column in c.fetchall()]
    
    # 检查是否需要添加balance列（用户余额）
    if 'balance' not in users_columns:
        logger.info("为users表添加balance列")
        c.execute("ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0")
    
    # 检查是否需要添加credit_limit列（透支额度）
    if 'credit_limit' not in users_columns:
        logger.info("为users表添加credit_limit列")
        c.execute("ALTER TABLE users ADD COLUMN credit_limit REAL DEFAULT 0")
    
    # 创建超级管理员账号（如果不存在）
    admin_hash = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()
    c.execute("SELECT id FROM users WHERE username = ?", (ADMIN_USERNAME,))
    if not c.fetchone():
        logger.info(f"创建默认管理员账号: {ADMIN_USERNAME}")
        c.execute("""
            INSERT INTO users (username, password_hash, is_admin, created_at) 
            VALUES (?, ?, 1, ?)
        """, (ADMIN_USERNAME, admin_hash, get_china_time()))
    
    conn.commit()
    conn.close()
    logger.info("SQLite数据库初始化完成")

def init_postgres_db():
    """初始化PostgreSQL数据库"""
    url = urlparse(DATABASE_URL)
    dbname = url.path[1:]
    user = url.username
    password = url.password
    host = url.hostname
    port = url.port
    
    conn = psycopg2.connect(
        dbname=dbname,
        user=user,
        password=password,
        host=host,
        port=port
    )
    conn.autocommit = True
    c = conn.cursor()
    
    # 订单表
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            account TEXT NOT NULL,
            password TEXT NOT NULL,
            package TEXT NOT NULL,
            remark TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            accepted_at TEXT,
            completed_at TEXT,
            accepted_by TEXT,
            accepted_by_username TEXT,
            accepted_by_first_name TEXT,
            notified INTEGER DEFAULT 0,
            web_user_id TEXT,
            user_id INTEGER,
            refunded INTEGER DEFAULT 0
        )
    """)
    
    # 用户表
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login TEXT,
            balance REAL DEFAULT 0,
            credit_limit REAL DEFAULT 0
        )
    """)
    
    # 卖家表
    c.execute("""
        CREATE TABLE IF NOT EXISTS sellers (
            telegram_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            added_at TEXT NOT NULL,
            added_by TEXT
        )
    """)
    
    # 检查是否需要添加新列
    try:
        c.execute("SELECT user_id FROM orders LIMIT 1")
    except psycopg2.errors.UndefinedColumn:
        c.execute("ALTER TABLE orders ADD COLUMN user_id INTEGER")
    
    # 检查是否需要添加refunded列（是否已退款）
    try:
        c.execute("SELECT refunded FROM orders LIMIT 1")
    except psycopg2.errors.UndefinedColumn:
        logger.info("为orders表添加refunded列")
        c.execute("ALTER TABLE orders ADD COLUMN refunded INTEGER DEFAULT 0")
    
    # 检查是否需要添加balance列（用户余额）
    try:
        c.execute("SELECT balance FROM users LIMIT 1")
    except psycopg2.errors.UndefinedColumn:
        logger.info("为users表添加balance列")
        c.execute("ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0")
    
    # 检查是否需要添加credit_limit列（透支额度）
    try:
        c.execute("SELECT credit_limit FROM users LIMIT 1")
    except psycopg2.errors.UndefinedColumn:
        logger.info("为users表添加credit_limit列")
        c.execute("ALTER TABLE users ADD COLUMN credit_limit REAL DEFAULT 0")
    
    # 检查是否需要添加accepted_by_username列（Telegram用户名）
    try:
        c.execute("SELECT accepted_by_username FROM orders LIMIT 1")
    except psycopg2.errors.UndefinedColumn:
        logger.info("为orders表添加accepted_by_username列")
        c.execute("ALTER TABLE orders ADD COLUMN accepted_by_username TEXT")
    
    # 检查是否需要添加accepted_by_first_name列（Telegram昵称）
    try:
        c.execute("SELECT accepted_by_first_name FROM orders LIMIT 1")
    except psycopg2.errors.UndefinedColumn:
        logger.info("为orders表添加accepted_by_first_name列")
        c.execute("ALTER TABLE orders ADD COLUMN accepted_by_first_name TEXT")
    
    # 创建超级管理员账号（如果不存在）
    admin_hash = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()
    c.execute("SELECT id FROM users WHERE username = %s", (ADMIN_USERNAME,))
    if not c.fetchone():
        c.execute("""
            INSERT INTO users (username, password_hash, is_admin, created_at) 
            VALUES (%s, %s, 1, %s)
        """, (ADMIN_USERNAME, admin_hash, get_china_time()))
    
    conn.close()

# 数据库执行函数
def execute_query(query, params=(), fetch=False, return_cursor=False):
    """执行数据库查询并返回结果"""
    logger.debug(f"执行查询: {query[:50]}... 参数: {params}")
    if DATABASE_URL.startswith('postgres'):
        return execute_postgres_query(query, params, fetch, return_cursor)
    else:
        return execute_sqlite_query(query, params, fetch, return_cursor)

def execute_sqlite_query(query, params=(), fetch=False, return_cursor=False):
    """执行SQLite查询并返回结果"""
    try:
        # 使用绝对路径访问数据库
        current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(current_dir, "orders.db")
        logger.debug(f"执行查询，使用数据库: {db_path}")
        
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 检查是否为INSERT语句，确保notified字段被正确设置
        if "INSERT INTO orders" in query and "notified" not in query:
            logger.warning("检测到INSERT订单但未包含notified字段，自动添加notified=0")
            # 修改查询添加notified字段
            if ")" in query and "VALUES" in query:
                parts = query.split(")")
                values_part = parts[1].strip()
                if values_part.startswith("VALUES"):
                    # 在字段列表末尾添加notified
                    parts[0] = parts[0] + ", notified"
                    # 在值列表末尾添加0
                    values_start = values_part.find("(")
                    if values_start >= 0:
                        values_part = values_part[:values_start+1] + "?, " + values_part[values_start+1:]
                        parts[1] = values_part
                        query = ")".join(parts)
                        params = params + (0,)
        
        cursor.execute(query, params)
        
        if return_cursor:
            conn.commit()
            return cursor

        result = None
        if fetch:
            result = cursor.fetchall()
            logger.debug(f"查询返回 {len(result)} 条结果")
        else:
            logger.debug(f"查询影响 {cursor.rowcount} 行")
        
        conn.commit()
        conn.close()
        return result
    except Exception as e:
        logger.error(f"SQLite查询执行失败: {str(e)}", exc_info=True)
        raise

def execute_postgres_query(query, params=(), fetch=False, return_cursor=False):
    """执行PostgreSQL查询并返回结果"""
    url = urlparse(DATABASE_URL)
    dbname = url.path[1:]
    user = url.username
    password = url.password
    host = url.hostname
    port = url.port
    
    conn = psycopg2.connect(
        dbname=dbname,
        user=user,
        password=password,
        host=host,
        port=port
    )
    cursor = conn.cursor()
    
    # PostgreSQL使用%s作为参数占位符，而不是SQLite的?
    query = query.replace('?', '%s')
    cursor.execute(query, params)
    
    if return_cursor:
        conn.commit()
        return cursor

    result = None
    if fetch:
        result = cursor.fetchall()
    
    conn.commit()
    conn.close()
    return result

# ===== 密码加密 =====
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# 获取未通知订单
def get_unnotified_orders():
    """获取未通知的订单"""
    orders = execute_query("""
        SELECT id, account, password, package, created_at, web_user_id 
        FROM orders 
        WHERE notified = 0 AND status = ?
    """, (STATUS['SUBMITTED'],), fetch=True)
    
    # 记录获取到的未通知订单
    if orders:
        logger.info(f"获取到 {len(orders)} 个未通知订单")
    
    return orders

# 接单原子操作
def accept_order_atomic(oid, user_id):
    # 使用事务确保操作的原子性
    if DATABASE_URL.startswith('postgres'):
        # PostgreSQL版本
        url = urlparse(DATABASE_URL)
        dbname = url.path[1:]
        user = url.username
        password = url.password
        host = url.hostname
        port = url.port
        
        conn = psycopg2.connect(
            dbname=dbname,
            user=user,
            password=password,
            host=host,
            port=port
        )
        cursor = conn.cursor()
        
        try:
            # 开始事务
            cursor.execute("BEGIN")
            
            # 检查订单状态
            cursor.execute("SELECT status FROM orders WHERE id = %s FOR UPDATE", (oid,))
            order = cursor.fetchone()
            if not order or order[0] != 'submitted':
                conn.rollback()
                conn.close()
                return False, "Order already taken or not found"
            
            # 检查该用户当前接单数量（状态为accepted的订单）
            cursor.execute("""
                SELECT COUNT(*) FROM orders 
                WHERE accepted_by = %s AND status = 'accepted'
            """, (str(user_id),))
            active_count = cursor.fetchone()[0]
            
            if active_count >= 2:
                conn.rollback()
                conn.close()
                return False, "You already have 2 active orders. Please complete your current orders first before accepting new ones."
            
            # 获取用户信息
            try:
                # 从缓存中获取用户名和昵称
                from modules.constants import user_info_cache
                username = None
                first_name = None
                last_name = None
                full_name = None
                
                if user_id in user_info_cache:
                    username = user_info_cache[user_id].get('username')
                    first_name = user_info_cache[user_id].get('first_name')
                    last_name = user_info_cache[user_id].get('last_name', '')
                    
                    # 组合完整昵称
                    if first_name:
                        if last_name:
                            full_name = f"{first_name} {last_name}".strip()
                        else:
                            full_name = first_name
                
                # 更新订单
                timestamp = get_china_time()
                cursor.execute("UPDATE orders SET status = 'accepted', accepted_at = %s, accepted_by = %s, accepted_by_username = %s, accepted_by_first_name = %s WHERE id = %s",
                            (timestamp, str(user_id), username, full_name, oid))
            except Exception as e:
                logger.error(f"获取用户信息失败: {str(e)}")
                # 如果获取用户信息失败，仍然更新订单，但不设置用户名和昵称
                timestamp = get_china_time()
                cursor.execute("UPDATE orders SET status = 'accepted', accepted_at = %s, accepted_by = %s WHERE id = %s",
                            (timestamp, str(user_id), oid))
            
            # 提交事务
            conn.commit()
            conn.close()
            return True, "Success"
            
        except Exception as e:
            conn.rollback()
            conn.close()
            logger.error(f"Error in accept_order_atomic: {str(e)}")
            return False, "Database error"
    else:
        # SQLite版本
        # 使用绝对路径访问数据库
        current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(current_dir, "orders.db")
        logger.debug(f"接单操作，使用数据库: {db_path}")
        
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        try:
            # 开始独占事务
            cursor.execute("BEGIN EXCLUSIVE")
            
            # 检查订单状态
            cursor.execute("SELECT status FROM orders WHERE id = ?", (oid,))
            order = cursor.fetchone()
            if not order or order[0] != 'submitted':
                conn.rollback()
                conn.close()
                return False, "Order already taken or not found"
            
            # 检查该用户当前接单数量（状态为accepted的订单）
            cursor.execute("""
                SELECT COUNT(*) FROM orders 
                WHERE accepted_by = ? AND status = 'accepted'
            """, (str(user_id),))
            active_count = cursor.fetchone()[0]
            
            if active_count >= 2:
                conn.rollback()
                conn.close()
                return False, "You already have 2 active orders. Please complete your current orders first before accepting new ones."
            
            # 获取用户信息
            try:
                # 从缓存中获取用户名和昵称
                from modules.constants import user_info_cache
                username = None
                first_name = None
                last_name = None
                full_name = None
                
                if user_id in user_info_cache:
                    username = user_info_cache[user_id].get('username')
                    first_name = user_info_cache[user_id].get('first_name')
                    last_name = user_info_cache[user_id].get('last_name', '')
                    
                    # 组合完整昵称
                    if first_name:
                        if last_name:
                            full_name = f"{first_name} {last_name}".strip()
                        else:
                            full_name = first_name
                
                # 更新订单
                timestamp = get_china_time()
                cursor.execute("UPDATE orders SET status = 'accepted', accepted_at = ?, accepted_by = ?, accepted_by_username = ?, accepted_by_first_name = ? WHERE id = ?",
                            (timestamp, str(user_id), username, full_name, oid))
            except Exception as e:
                logger.error(f"获取用户信息失败: {str(e)}")
                # 如果获取用户信息失败，仍然更新订单，但不设置用户名和昵称
                timestamp = get_china_time()
                cursor.execute("UPDATE orders SET status = 'accepted', accepted_at = ?, accepted_by = ? WHERE id = ?",
                            (timestamp, str(user_id), oid))
            
            # 提交事务
            conn.commit()
            conn.close()
            return True, "Success"
            
        except Exception as e:
            conn.rollback()
            conn.close()
            logger.error(f"Error in accept_order_atomic: {str(e)}")
            return False, "Database error"

# 获取订单详情
def get_order_details(oid):
    return execute_query("SELECT id, account, password, package, status, remark FROM orders WHERE id = ?", (oid,), fetch=True)

# ===== 卖家管理 =====
def get_all_sellers():
    """获取所有卖家信息"""
    return execute_query("SELECT telegram_id, username, first_name, is_active, added_at, added_by FROM sellers ORDER BY added_at DESC", fetch=True)

def get_active_seller_ids():
    """获取所有活跃的卖家Telegram ID"""
    if DATABASE_URL.startswith('postgres'):
        sellers = execute_query("SELECT telegram_id FROM sellers WHERE is_active = TRUE", fetch=True)
    else:
        sellers = execute_query("SELECT telegram_id FROM sellers WHERE is_active = 1", fetch=True)
    return [seller[0] for seller in sellers]

def add_seller(telegram_id, username, first_name, added_by):
    """添加新卖家"""
    timestamp = get_china_time()
    execute_query(
        "INSERT INTO sellers (telegram_id, username, first_name, added_at, added_by) VALUES (?, ?, ?, ?, ?)",
        (telegram_id, username, first_name, timestamp, added_by)
    )

def toggle_seller_status(telegram_id):
    """切换卖家活跃状态"""
    execute_query("UPDATE sellers SET is_active = NOT is_active WHERE telegram_id = ?", (telegram_id,))

def remove_seller(telegram_id):
    """移除卖家"""
    return execute_query("DELETE FROM sellers WHERE telegram_id=?", (telegram_id,))

# ===== 余额系统相关函数 =====
def get_user_balance(user_id):
    """获取用户余额"""
    result = execute_query("SELECT balance FROM users WHERE id=?", (user_id,), fetch=True)
    if result:
        return result[0][0]
    return 0

def get_user_credit_limit(user_id):
    """获取用户透支额度"""
    result = execute_query("SELECT credit_limit FROM users WHERE id=?", (user_id,), fetch=True)
    if result:
        return result[0][0]
    return 0

def set_user_credit_limit(user_id, credit_limit):
    """设置用户透支额度（仅限管理员使用）"""
    # 确保透支额度不为负
    if credit_limit < 0:
        credit_limit = 0
    
    execute_query("UPDATE users SET credit_limit=? WHERE id=?", (credit_limit, user_id))
    return True, credit_limit

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
    
    # 更新余额
    execute_query("UPDATE users SET balance=? WHERE id=?", (new_balance, user_id))
    return True, new_balance

def set_user_balance(user_id, balance):
    """设置用户余额（仅限管理员使用）"""
    # 确保余额不为负
    if balance < 0:
        balance = 0
    
    execute_query("UPDATE users SET balance=? WHERE id=?", (balance, user_id))
    return True, balance

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
    """退款订单金额到用户余额"""
    # 获取订单信息
    order = execute_query("""
        SELECT id, user_id, package, status 
        FROM orders 
        WHERE id=?
    """, (order_id,), fetch=True)
    
    if not order or not order[0]:
        logger.warning(f"退款失败: 找不到订单ID={order_id}")
        return False, "找不到订单"
    
    order_id, user_id, package, status = order[0]
    
    # 只有已撤销或充值失败的订单才能退款
    if status not in ['cancelled', 'failed']:
        logger.warning(f"退款失败: 订单状态不是已撤销或充值失败 (ID={order_id}, 状态={status})")
        return False, f"订单状态不允许退款: {status}"
    
    # 检查订单是否已退款
    refunded = execute_query("SELECT refunded FROM orders WHERE id=?", (order_id,), fetch=True)
    if refunded and refunded[0][0]:
        logger.warning(f"退款失败: 订单已退款 (ID={order_id})")
        return False, "订单已退款"
    
    # 获取套餐价格
    from modules.constants import WEB_PRICES
    price = WEB_PRICES.get(package, 0)
    
    if price <= 0:
        logger.warning(f"退款失败: 套餐价格无效 (ID={order_id}, 套餐={package}, 价格={price})")
        return False, "套餐价格无效"
    
    # 退款到用户余额
    success, new_balance = update_user_balance(user_id, price)
    if not success:
        logger.error(f"退款到用户余额失败: 订单ID={order_id}, 用户ID={user_id}, 金额={price}")
        return False, "退款到用户余额失败"
    
    # 标记订单为已退款
    execute_query("UPDATE orders SET refunded=1 WHERE id=?", (order_id,))
    
    logger.info(f"订单退款成功: ID={order_id}, 用户ID={user_id}, 金额={price}, 新余额={new_balance}")
    return True, new_balance

def create_order_with_deduction_atomic(account, password, package, remark, username, user_id):
    """
    在一个事务中创建订单并扣除用户余额。
    这是保证数据一致性的关键操作。
    返回 (bool: success, str: message, float: new_balance, float: credit_limit)
    """
    from .constants import WEB_PRICES, STATUS, DATABASE_URL

    price = WEB_PRICES.get(str(package))
    if price is None:
        return False, "无效的套餐", None, None

    if DATABASE_URL.startswith('postgres'):
        # PostgreSQL 的事务逻辑
        conn = None
        try:
            url = urlparse(DATABASE_URL)
            conn = psycopg2.connect(
                dbname=url.path[1:],
                user=url.username,
                password=url.password,
                host=url.hostname,
                port=url.port
            )
            with conn:
                c = conn.cursor()
                c.execute("SELECT balance, credit_limit FROM users WHERE id = %s FOR UPDATE", (user_id,))
                user_data = c.fetchone()
                if not user_data:
                    return False, "用户不存在", None, None
                
                balance, credit_limit = user_data
                if (balance + credit_limit) < price:
                    return False, f'余额和透支额度不足，当前余额: {balance}，透支额度: {credit_limit}，套餐价格: {price}', balance, credit_limit

                timestamp = datetime.now(CN_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
                c.execute("""
                    INSERT INTO orders (account, password, package, remark, status, created_at, web_user_id, user_id, notified, refunded)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (account, password, package, remark, STATUS['SUBMITTED'], timestamp, username, user_id, 0, 0))
                
                new_balance = balance - price
                c.execute("UPDATE users SET balance = %s WHERE id = %s", (new_balance, user_id))

            return True, "订单创建成功", new_balance, credit_limit
        except (Exception, psycopg2.Error) as e:
            logger.error(f"创建PostgreSQL订单事务失败: {e}", exc_info=True)
            return False, "数据库操作失败，订单未创建", None, None
        finally:
            if conn:
                conn.close()
    else:
        # SQLite 的事务逻辑
        conn = None
        try:
            conn = sqlite3.connect("orders.db", timeout=10)
            with conn:
                conn.isolation_level = 'EXCLUSIVE'
                c = conn.cursor()
                c.execute("SELECT balance, credit_limit FROM users WHERE id = ?", (user_id,))
                user_data = c.fetchone()
                if not user_data:
                    return False, "用户不存在", None, None
                
                balance, credit_limit = user_data
                if (balance + credit_limit) < price:
                    return False, f'余额和透支额度不足，当前余额: {balance}，透支额度: {credit_limit}，套餐价格: {price}', balance, credit_limit

                timestamp = datetime.now(CN_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
                c.execute("""
                    INSERT INTO orders (account, password, package, remark, status, created_at, web_user_id, user_id, notified, refunded)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (account, password, package, remark, STATUS['SUBMITTED'], timestamp, username, user_id, 0, 0))

                new_balance = balance - price
                c.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user_id))

            return True, "订单创建成功", new_balance, credit_limit
        except sqlite3.Error as e:
            logger.error(f"创建SQLite订单事务失败: {e}", exc_info=True)
            return False, "数据库操作失败，订单未创建", None, None
        finally:
            if conn:
                conn.close() 

# ===== 充值相关函数 =====
def create_recharge_tables():
    """创建充值记录表"""
    try:
        if DATABASE_URL.startswith('postgres'):
            # 检查表是否存在
            table_exists = execute_query("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_name = 'recharge_requests'
                )
            """, fetch=True)
            
            if not table_exists or not table_exists[0][0]:
                execute_query("""
                    CREATE TABLE recharge_requests (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        amount REAL NOT NULL,
                        status TEXT NOT NULL,
                        payment_method TEXT NOT NULL,
                        proof_image TEXT,
                        created_at TEXT NOT NULL,
                        processed_at TEXT,
                        processed_by TEXT,
                        FOREIGN KEY (user_id) REFERENCES users (id)
                    )
                """)
                logger.info("已创建充值记录表(PostgreSQL)")
        else:
            # SQLite连接
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "orders.db")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # 检查表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='recharge_requests'")
            if not cursor.fetchone():
                cursor.execute("""
                    CREATE TABLE recharge_requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        amount REAL NOT NULL,
                        status TEXT NOT NULL,
                        payment_method TEXT NOT NULL,
                        proof_image TEXT,
                        created_at TEXT NOT NULL,
                        processed_at TEXT,
                        processed_by TEXT,
                        FOREIGN KEY (user_id) REFERENCES users (id)
                    )
                """)
                conn.commit()
                logger.info("已创建充值记录表(SQLite)")
            
            conn.close()
        
        return True
    except Exception as e:
        logger.error(f"创建充值记录表失败: {str(e)}", exc_info=True)
        return False

def create_recharge_request(user_id, amount, payment_method, proof_image):
    """创建充值请求"""
    try:
        # 获取当前时间
        now = get_china_time()
        
        # 插入充值请求记录
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL需要使用RETURNING子句获取新ID
            result = execute_query("""
                INSERT INTO recharge_requests (user_id, amount, status, payment_method, proof_image, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, amount, 'pending', payment_method, proof_image, now), fetch=True)
            request_id = result[0][0]
        else:
            # SQLite可以直接获取lastrowid
            conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db"))
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO recharge_requests (user_id, amount, status, payment_method, proof_image, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, amount, 'pending', payment_method, proof_image, now))
            request_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
        return request_id, True, "充值请求已提交"
    except Exception as e:
        logger.error(f"创建充值请求失败: {str(e)}", exc_info=True)
        return None, False, f"创建充值请求失败: {str(e)}"

def get_user_recharge_requests(user_id):
    """获取用户的充值请求记录"""
    try:
        if DATABASE_URL.startswith('postgres'):
            requests = execute_query("""
                SELECT id, amount, status, payment_method, proof_image, created_at, processed_at
                FROM recharge_requests
                WHERE user_id = %s
                ORDER BY created_at DESC
            """, (user_id,), fetch=True)
        else:
            requests = execute_query("""
                SELECT id, amount, status, payment_method, proof_image, created_at, processed_at
                FROM recharge_requests
                WHERE user_id = ?
                ORDER BY created_at DESC
            """, (user_id,), fetch=True)
        
        return requests
    except Exception as e:
        logger.error(f"获取用户充值请求失败: {str(e)}", exc_info=True)
        return []

def get_pending_recharge_requests():
    """获取所有待处理的充值请求"""
    try:
        if DATABASE_URL.startswith('postgres'):
            requests = execute_query("""
                SELECT r.id, r.user_id, r.amount, r.payment_method, r.proof_image, r.created_at, u.username
                FROM recharge_requests r
                JOIN users u ON r.user_id = u.id
                WHERE r.status = %s
                ORDER BY r.created_at ASC
            """, ('pending',), fetch=True)
        else:
            requests = execute_query("""
                SELECT r.id, r.user_id, r.amount, r.payment_method, r.proof_image, r.created_at, u.username
                FROM recharge_requests r
                JOIN users u ON r.user_id = u.id
                WHERE r.status = ?
                ORDER BY r.created_at ASC
            """, ('pending',), fetch=True)
        
        return requests
    except Exception as e:
        logger.error(f"获取待处理充值请求失败: {str(e)}", exc_info=True)
        return []

def approve_recharge_request(request_id, admin_id):
    """批准充值请求并增加用户余额"""
    try:
        # 获取充值请求详情
        if DATABASE_URL.startswith('postgres'):
            request = execute_query("""
                SELECT user_id, amount
                FROM recharge_requests
                WHERE id = %s AND status = %s
            """, (request_id, 'pending'), fetch=True)
        else:
            request = execute_query("""
                SELECT user_id, amount
                FROM recharge_requests
                WHERE id = ? AND status = ?
            """, (request_id, 'pending'), fetch=True)
        
        if not request:
            return False, "充值请求不存在或已处理"
            
        user_id, amount = request[0]
        
        # 开始事务
        conn = None
        if DATABASE_URL.startswith('postgres'):
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False
        else:
            # 使用绝对路径访问数据库
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "orders.db")
            conn = sqlite3.connect(db_path)
        
        try:
            cursor = conn.cursor()
            now = get_china_time()
            
            # 更新充值请求状态
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("""
                    UPDATE recharge_requests
                    SET status = %s, processed_at = %s, processed_by = %s
                    WHERE id = %s
                """, ('approved', now, admin_id, request_id))
                
                # 增加用户余额
                cursor.execute("""
                    UPDATE users
                    SET balance = balance + %s
                    WHERE id = %s
                """, (amount, user_id))
            else:
                cursor.execute("""
                    UPDATE recharge_requests
                    SET status = ?, processed_at = ?, processed_by = ?
                    WHERE id = ?
                """, ('approved', now, admin_id, request_id))
                
                # 增加用户余额
                cursor.execute("""
                    UPDATE users
                    SET balance = balance + ?
                    WHERE id = ?
                """, (amount, user_id))
            
            # 提交事务
            conn.commit()
            
            return True, f"已成功批准充值 {amount} 元"
        except Exception as e:
            # 回滚事务
            if conn:
                conn.rollback()
            logger.error(f"批准充值请求失败: {str(e)}", exc_info=True)
            return False, f"批准充值请求失败: {str(e)}"
        finally:
            if conn:
                conn.close()
    except Exception as e:
        logger.error(f"批准充值请求失败: {str(e)}", exc_info=True)
        return False, f"批准充值请求失败: {str(e)}"

def reject_recharge_request(request_id, admin_id):
    """拒绝充值请求"""
    try:
        # 获取当前时间
        now = get_china_time()
        
        # 更新充值请求状态
        if DATABASE_URL.startswith('postgres'):
            execute_query("""
                UPDATE recharge_requests
                SET status = %s, processed_at = %s, processed_by = %s
                WHERE id = %s AND status = %s
            """, ('rejected', now, admin_id, request_id, 'pending'))
        else:
            execute_query("""
                UPDATE recharge_requests
                SET status = ?, processed_at = ?, processed_by = ?
                WHERE id = ? AND status = ?
            """, ('rejected', now, admin_id, request_id, 'pending'))
        
        return True, "已拒绝充值请求"
    except Exception as e:
        logger.error(f"拒绝充值请求失败: {str(e)}", exc_info=True)
        return False, f"拒绝充值请求失败: {str(e)}" 