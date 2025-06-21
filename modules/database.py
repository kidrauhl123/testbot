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
        if DATABASE_URL.startswith('postgres'):
            result = execute_query("""
                INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, amount, type_name, reason, reference_id, balance_after, now), fetch=True)
            return result[0][0]
        else:
            conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db"))
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, amount, type_name, reason, reference_id, balance_after, now))
            record_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return record_id
    except Exception as e:
        logger.error(f"添加余额变动记录失败: {str(e)}", exc_info=True)
        return None

# ===== 数据库 =====
def init_db():
    """根据环境配置初始化数据库"""
    logger.info(f"初始化数据库，使用连接: {DATABASE_URL[:10]}...")
    if DATABASE_URL.startswith('postgres'):
        init_postgres_db()
    else:
        init_sqlite_db()
    
    # 创建充值记录表和余额记录表
    logger.info("正在创建充值记录表和余额记录表...")
    create_recharge_tables()
    logger.info("充值记录表和余额记录表创建完成")
    
    # 创建激活码表
    logger.info("正在创建激活码表...")
    create_activation_code_table()
    logger.info("激活码表创建完成")

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
    
    # 油管会员充值表
    c.execute("""
        CREATE TABLE IF NOT EXISTS youtube_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qrcode_path TEXT NOT NULL,
            package TEXT NOT NULL DEFAULT '12',
            remark TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            accepted_at TEXT,
            completed_at TEXT,
            accepted_by TEXT,
            accepted_by_username TEXT,
            accepted_by_first_name TEXT,
            notified INTEGER DEFAULT 0,
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
            added_by TEXT,
            is_admin BOOLEAN DEFAULT FALSE
        )
    """)
    
    # 用户定制价格表
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_custom_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            package TEXT NOT NULL,
            price REAL NOT NULL,
            created_at TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (created_by) REFERENCES users (id),
            UNIQUE(user_id, package)
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
    
    # 检查recharge_requests表中是否需要添加新列
    try:
        c.execute("PRAGMA table_info(recharge_requests)")
        recharge_columns = [column[1] for column in c.fetchall()]
        if 'details' not in recharge_columns:
            logger.info("为recharge_requests表添加details列")
            c.execute("ALTER TABLE recharge_requests ADD COLUMN details TEXT")
    except sqlite3.OperationalError:
        # Table might not exist yet, will be created by create_recharge_tables()
        pass
    
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
            added_by TEXT,
            is_admin BOOLEAN DEFAULT FALSE
        )
    """)
    
    # 用户定制价格表
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_custom_prices (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            package TEXT NOT NULL,
            price REAL NOT NULL,
            created_at TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            UNIQUE(user_id, package)
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
    
    # 检查是否需要添加details列（充值详情，如口令）
    try:
        c.execute("SELECT details FROM recharge_requests LIMIT 1")
    except psycopg2.errors.UndefinedColumn:
        logger.info("为recharge_requests表添加details列")
        c.execute("ALTER TABLE recharge_requests ADD COLUMN details TEXT")
    except psycopg2.errors.UndefinedTable:
        # Table might not exist yet
        pass
    
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
            if not order:
                conn.rollback()
                conn.close()
                return False, "Order not found"
                
            if order[0] == 'cancelled':
                conn.rollback()
                conn.close()
                return False, "Order has been cancelled"
                
            if order[0] != 'submitted':
                conn.rollback()
                conn.close()
                return False, "Order already taken"
            
            # 检查该用户是否有正在质疑的订单
            cursor.execute("""
                SELECT COUNT(*) FROM orders 
                WHERE accepted_by = %s AND status = 'disputing'
            """, (str(user_id),))
            disputing_count = cursor.fetchone()[0]
            
            if disputing_count > 0:
                conn.rollback()
                conn.close()
                return False, "You have a disputed order. Please resolve it before accepting new orders."
            
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
            if not order:
                conn.rollback()
                conn.close()
                return False, "Order not found"
                
            if order[0] == 'cancelled':
                conn.rollback()
                conn.close()
                return False, "Order has been cancelled"
                
            if order[0] != 'submitted':
                conn.rollback()
                conn.close()
                return False, "Order already taken"
            
            # 检查该用户是否有正在质疑的订单
            cursor.execute("""
                SELECT COUNT(*) FROM orders 
                WHERE accepted_by = ? AND status = 'disputing'
            """, (str(user_id),))
            disputing_count = cursor.fetchone()[0]
            
            if disputing_count > 0:
                conn.rollback()
                conn.close()
                return False, "You have a disputed order. Please resolve it before accepting new orders."
            
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
    if DATABASE_URL.startswith('postgres'):
        # PostgreSQL需要显式处理BOOLEAN类型
        return execute_query("""
            SELECT telegram_id, username, first_name, is_active, 
                   added_at, added_by, 
                   COALESCE(is_admin, FALSE) as is_admin 
            FROM sellers 
            ORDER BY added_at DESC
        """, fetch=True)
    else:
        # SQLite版本
        return execute_query("""
            SELECT telegram_id, username, first_name, is_active, 
                   added_at, added_by, 
                   COALESCE(is_admin, 0) as is_admin 
            FROM sellers 
            ORDER BY added_at DESC
        """, fetch=True)

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

def toggle_seller_admin(telegram_id):
    """切换卖家的管理员状态"""
    try:
        # 先获取当前状态
        if DATABASE_URL.startswith('postgres'):
            current = execute_query(
                "SELECT COALESCE(is_admin, FALSE) FROM sellers WHERE telegram_id = ?", 
                (telegram_id,), 
                fetch=True
            )
        else:
            current = execute_query(
                "SELECT COALESCE(is_admin, 0) FROM sellers WHERE telegram_id = ?", 
                (telegram_id,), 
                fetch=True
            )
            
        if not current:
            return False
            
        new_status = not bool(current[0][0])
        
        if DATABASE_URL.startswith('postgres'):
            execute_query(
                "UPDATE sellers SET is_admin = ? WHERE telegram_id = ?",
                (new_status, telegram_id)
            )
        else:
            execute_query(
                "UPDATE sellers SET is_admin = ? WHERE telegram_id = ?",
                (1 if new_status else 0, telegram_id)
            )
        return True
    except Exception as e:
        logger.error(f"切换卖家管理员状态失败: {e}")
        return False

def is_admin_seller(telegram_id):
    """检查卖家是否是管理员"""
    if DATABASE_URL.startswith('postgres'):
        result = execute_query(
            "SELECT COALESCE(is_admin, FALSE) FROM sellers WHERE telegram_id = ? AND is_active = TRUE",
            (telegram_id,),
            fetch=True
        )
    else:
        result = execute_query(
            "SELECT COALESCE(is_admin, 0) FROM sellers WHERE telegram_id = ? AND is_active = 1",
            (telegram_id,),
            fetch=True
        )
    return bool(result and result[0][0])

# ===== 余额系统相关函数 =====
def get_user_balance(user_id):
    """获取用户余额"""
    if DATABASE_URL.startswith('postgres'):
        result = execute_query("SELECT balance FROM users WHERE id=%s", (user_id,), fetch=True)
    else:
        result = execute_query("SELECT balance FROM users WHERE id=?", (user_id,), fetch=True)
    
    if result:
        return result[0][0]
    return 0

def get_user_credit_limit(user_id):
    """获取用户透支额度"""
    if DATABASE_URL.startswith('postgres'):
        result = execute_query("SELECT credit_limit FROM users WHERE id=%s", (user_id,), fetch=True)
    else:
        result = execute_query("SELECT credit_limit FROM users WHERE id=?", (user_id,), fetch=True)
    
    if result:
        return result[0][0]
    return 0

def set_user_credit_limit(user_id, credit_limit):
    """设置用户透支额度（仅限管理员使用）"""
    # 确保透支额度不为负
    if credit_limit < 0:
        credit_limit = 0
    
    if DATABASE_URL.startswith('postgres'):
        execute_query("UPDATE users SET credit_limit=%s WHERE id=%s", (credit_limit, user_id))
    else:
        execute_query("UPDATE users SET credit_limit=? WHERE id=?", (credit_limit, user_id))
    
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
            if DATABASE_URL.startswith('postgres'):
                records = execute_query("""
                    SELECT br.id, br.user_id, u.username, br.amount, br.type, br.reason, br.reference_id, br.balance_after, br.created_at
                    FROM balance_records br
                    JOIN users u ON br.user_id = u.id
                    WHERE br.user_id = %s
                    ORDER BY br.id DESC
                    LIMIT %s OFFSET %s
                """, (user_id, limit, offset), fetch=True)
            else:
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
            if DATABASE_URL.startswith('postgres'):
                records = execute_query("""
                    SELECT br.id, br.user_id, u.username, br.amount, br.type, br.reason, br.reference_id, br.balance_after, br.created_at
                    FROM balance_records br
                    JOIN users u ON br.user_id = u.id
                    ORDER BY br.id DESC
                    LIMIT %s OFFSET %s
                """, (limit, offset), fetch=True)
            else:
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
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL连接
            url = urlparse(DATABASE_URL)
            conn = psycopg2.connect(
                dbname=url.path[1:],
                user=url.username,
                password=url.password,
                host=url.hostname,
                port=url.port
            )
            
            with conn:
                cursor = conn.cursor()
                
                # 更新余额
                cursor.execute("""
                    UPDATE users 
                    SET balance = %s 
                    WHERE id = %s
                    RETURNING balance
                """, (new_balance, user_id))
                
                # 确认更新成功
                result = cursor.fetchone()
                if not result:
                    logger.error(f"更新用户余额失败: 用户ID={user_id}不存在")
                    return False, "用户不存在"
                
                updated_balance = result[0]
                
                # 记录余额变动
                type_name = 'recharge' if amount > 0 else 'consume'
                reason = '手动调整余额' if amount > 0 else '消费'
                now = get_china_time()
                
                cursor.execute("""
                    INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (user_id, amount, type_name, reason, None, updated_balance, now))
            
            return True, updated_balance
        else:
            # SQLite连接
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "orders.db")
            conn = sqlite3.connect(db_path, timeout=10)
            
            with conn:
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
            
            return True, new_balance
    
    except Exception as e:
        logger.error(f"更新用户余额失败: {str(e)}", exc_info=True)
        return False, f"更新用户余额失败: {str(e)}"
    
    finally:
        if conn:
            conn.close()

def set_user_balance(user_id, balance):
    """设置用户余额（仅限管理员使用）"""
    # 获取当前余额
    current_balance = get_user_balance(user_id)
    
    # 计算变动金额
    change_amount = balance - current_balance
    
    # 确保余额不为负
    if balance < 0:
        balance = 0
        change_amount = -current_balance
    
    # 如果没有变化，直接返回
    if change_amount == 0:
        return True, balance
    
    # 使用事务处理
    conn = None
    try:
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL连接
            url = urlparse(DATABASE_URL)
            conn = psycopg2.connect(
                dbname=url.path[1:],
                user=url.username,
                password=url.password,
                host=url.hostname,
                port=url.port
            )
            
            with conn:
                cursor = conn.cursor()
                
                # 更新余额
                cursor.execute("""
                    UPDATE users 
                    SET balance = %s 
                    WHERE id = %s
                    RETURNING balance
                """, (balance, user_id))
                
                # 确认更新成功
                result = cursor.fetchone()
                if not result:
                    logger.error(f"设置用户余额失败: 用户ID={user_id}不存在")
                    return False, "用户不存在"
                
                updated_balance = result[0]
                
                # 记录余额变动
                if change_amount != 0:
                    type_name = 'recharge' if change_amount > 0 else 'consume'
                    now = get_china_time()
                    
                    cursor.execute("""
                        INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (user_id, change_amount, type_name, '管理员调整余额', None, updated_balance, now))
            
            return True, updated_balance
        else:
            # SQLite连接
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "orders.db")
            conn = sqlite3.connect(db_path, timeout=10)
            
            with conn:
                c = conn.cursor()
                
                # 更新余额
                c.execute("UPDATE users SET balance = ? WHERE id = ?", (balance, user_id))
                
                # 记录余额变动
                if change_amount != 0:
                    type_name = 'recharge' if change_amount > 0 else 'consume'
                    now = get_china_time()
                    
                    c.execute("""
                        INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (user_id, change_amount, type_name, '管理员调整余额', None, balance, now))
            
            return True, balance
    
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
    """退款订单金额到用户余额 (兼容SQLite/PostgreSQL)"""
    # 先读取订单信息（使用 execute_query，自动选择数据库）
    order = execute_query(
        "SELECT id, user_id, package, status, refunded FROM orders WHERE id = ?" if not DATABASE_URL.startswith('postgres') else
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

    try:
        if DATABASE_URL.startswith('postgres'):
            # ---------- PostgreSQL 版本 ----------
            url = urlparse(DATABASE_URL)
            conn = psycopg2.connect(
                dbname=url.path[1:],
                user=url.username,
                password=url.password,
                host=url.hostname,
                port=url.port
            )
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
                logger.info(f"订单退款成功(PostgreSQL): ID={order_id}, 用户ID={user_id}, 金额={price}, 新余额={new_balance}")
                return True, new_balance
            except Exception as e:
                conn.rollback()
                logger.error(f"退款到用户余额失败(PostgreSQL): {str(e)}", exc_info=True)
                return False, str(e)
            finally:
                conn.close()
        else:
            # ---------- SQLite 版本 ---------- (原逻辑保持)
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "orders.db")
            conn = sqlite3.connect(db_path, timeout=10)
            try:
                with conn:
                    c = conn.cursor()
                    c.execute("SELECT balance FROM users WHERE id = ?", (user_id,))
                    current_balance = c.fetchone()[0]
                    new_balance = current_balance + price
                    c.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user_id))
                    c.execute("UPDATE orders SET refunded = 1 WHERE id = ?", (order_id,))
                    now = get_china_time()
                    c.execute(
                        """
                        INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (user_id, price, 'refund', f'订单退款: #{order_id}', order_id, new_balance, now)
                    )
                logger.info(f"订单退款成功(SQLite): ID={order_id}, 用户ID={user_id}, 金额={price}, 新余额={new_balance}")
                return True, new_balance
            except Exception as e:
                logger.error(f"退款到用户余额失败(SQLite): {str(e)}", exc_info=True)
                return False, str(e)
            finally:
                conn.close()
    except Exception as e:
        logger.error(f"退款到用户余额失败: {str(e)}", exc_info=True)
        return False, str(e)

def create_order_with_deduction_atomic(account, password, package, remark, username, user_id):
    """
    使用事务原子性地创建订单并扣除用户余额，兼容 SQLite 与 PostgreSQL
    
    返回:
    - (success, message, new_balance, credit_limit)
    """
    from modules.constants import WEB_PRICES, get_user_package_price

    try:
        if DATABASE_URL.startswith('postgres'):
            # ---------- PostgreSQL 版本 ----------
            url = urlparse(DATABASE_URL)
            conn = psycopg2.connect(
                dbname=url.path[1:],
                user=url.username,
                password=url.password,
                host=url.hostname,
                port=url.port
            )
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
                    INSERT INTO orders (account, password, package, status, created_at, remark, user_id, web_user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (account, password, package, 'submitted', now, remark, user_id, username)
                )

                conn.commit()
                return True, "订单创建成功", new_balance, credit_limit
            except Exception as e:
                conn.rollback()
                logger.error(f"创建订单失败(PostgreSQL): {str(e)}", exc_info=True)
                return False, f"创建订单失败: {str(e)}", None, None
            finally:
                conn.close()
        else:
            # ---------- SQLite 版本 ----------
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "orders.db")
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            try:
                cursor.execute("BEGIN TRANSACTION")
                cursor.execute("SELECT balance, credit_limit FROM users WHERE id = ?", (user_id,))
                user_data = cursor.fetchone()
                if not user_data:
                    conn.rollback()
                    return False, "用户不存在", None, None

                current_balance = user_data['balance']
                credit_limit = user_data['credit_limit']
                available_funds = current_balance + credit_limit

                price = get_user_package_price(user_id, package)
                if price > available_funds:
                    conn.rollback()
                    return False, f"余额不足，需要 {price} 元，可用 {available_funds} 元", current_balance, credit_limit

                new_balance = current_balance - price
                cursor.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user_id))
                now = get_china_time()
                cursor.execute(
                    """
                    INSERT INTO balance_records (user_id, amount, type, reason, balance_after, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, -price, 'consume', f'购买{package}个月套餐', new_balance, now)
                )

                cursor.execute(
                    """
                    INSERT INTO orders (account, password, package, status, created_at, remark, user_id, web_user_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (account, password, package, 'submitted', now, remark, user_id, username)
                )

                conn.commit()
                return True, "订单创建成功", new_balance, credit_limit
            except Exception as e:
                conn.rollback()
                logger.error(f"创建订单失败(SQLite): {str(e)}", exc_info=True)
                return False, f"创建订单失败: {str(e)}", None, None
            finally:
                conn.close()
    except Exception as e:
        logger.error(f"创建订单时数据库连接失败: {str(e)}", exc_info=True)
        return False, f"数据库连接失败: {str(e)}", None, None

# ===== 充值相关函数 =====
def create_recharge_tables():
    """创建充值记录表和余额明细表"""
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
                        details TEXT,
                        created_at TEXT NOT NULL,
                        processed_at TEXT,
                        processed_by TEXT,
                        FOREIGN KEY (user_id) REFERENCES users (id)
                    )
                """)
                logger.info("已创建充值记录表(PostgreSQL)")
                
            # 检查余额明细表是否存在
            balance_table_exists = execute_query("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_name = 'balance_records'
                )
            """, fetch=True)
            
            if not balance_table_exists or not balance_table_exists[0][0]:
                execute_query("""
                    CREATE TABLE balance_records (
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
                logger.info("已创建余额明细表(PostgreSQL)")
        else:
            # SQLite连接
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "orders.db")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # 检查充值表是否存在
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
                        details TEXT,
                        created_at TEXT NOT NULL,
                        processed_at TEXT,
                        processed_by TEXT,
                        FOREIGN KEY (user_id) REFERENCES users (id)
                    )
                """)
                conn.commit()
                logger.info("已创建充值记录表(SQLite)")
                
            # 检查余额明细表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='balance_records'")
            if not cursor.fetchone():
                cursor.execute("""
                    CREATE TABLE balance_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                conn.commit()
                logger.info("已创建余额明细表(SQLite)")
            
            conn.close()
        
        return True
    except Exception as e:
        logger.error(f"创建充值记录表或余额明细表失败: {str(e)}", exc_info=True)
        return False

def create_recharge_request(user_id, amount, payment_method, proof_image, details=None):
    """创建充值请求"""
    try:
        # 获取当前时间
        now = get_china_time()
        
        # 插入充值请求记录
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL需要使用RETURNING子句获取新ID
            result = execute_query("""
                INSERT INTO recharge_requests (user_id, amount, status, payment_method, proof_image, details, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, amount, 'pending', payment_method, proof_image, details, now), fetch=True)
            request_id = result[0][0]
        else:
            # SQLite可以直接获取lastrowid
            conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db"))
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO recharge_requests (user_id, amount, status, payment_method, proof_image, details, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, amount, 'pending', payment_method, proof_image, details, now))
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
                SELECT id, amount, status, payment_method, proof_image, created_at, processed_at, details
                FROM recharge_requests
                WHERE user_id = %s
                ORDER BY created_at DESC
            """, (user_id,), fetch=True)
        else:
            requests = execute_query("""
                SELECT id, amount, status, payment_method, proof_image, created_at, processed_at, details
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
                SELECT r.id, r.user_id, r.amount, r.payment_method, r.proof_image, r.created_at, u.username, r.details
                FROM recharge_requests r
                JOIN users u ON r.user_id = u.id
                WHERE r.status = %s
                ORDER BY r.created_at ASC
            """, ('pending',), fetch=True)
        else:
            requests = execute_query("""
                SELECT r.id, r.user_id, r.amount, r.payment_method, r.proof_image, r.created_at, u.username, r.details
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
                    RETURNING balance
                """, (amount, user_id))
                new_balance = cursor.fetchone()[0]
                
                # 记录余额变动
                cursor.execute("""
                    INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (user_id, amount, 'recharge', f'充值: 请求#{request_id}', request_id, new_balance, now))
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
                
                # 获取新余额
                cursor.execute("SELECT balance FROM users WHERE id = ?", (user_id,))
                new_balance = cursor.fetchone()[0]
                
                # 记录余额变动
                cursor.execute("""
                    INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (user_id, amount, 'recharge', f'充值: 请求#{request_id}', request_id, new_balance, now))
            
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

# ===== 激活码系统 =====
def create_activation_code_table():
    """创建激活码表"""
    try:
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL
            table_exists = execute_query("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_name = 'activation_codes'
                )
            """, fetch=True)
            
            if not table_exists or not table_exists[0][0]:
                execute_query("""
                    CREATE TABLE activation_codes (
                        id SERIAL PRIMARY KEY,
                        code TEXT UNIQUE NOT NULL,
                        package TEXT NOT NULL,
                        is_used INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL,
                        used_at TEXT,
                        used_by INTEGER,
                        created_by INTEGER,
                        FOREIGN KEY (used_by) REFERENCES users (id),
                        FOREIGN KEY (created_by) REFERENCES users (id)
                    )
                """)
                logger.info("已创建激活码表(PostgreSQL)")
        else:
            # SQLite
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "orders.db")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # 检查激活码表是否存在
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='activation_codes'")
            if not cursor.fetchone():
                cursor.execute("""
                    CREATE TABLE activation_codes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT UNIQUE NOT NULL,
                        package TEXT NOT NULL,
                        is_used INTEGER DEFAULT 0,
                        created_at TEXT NOT NULL,
                        used_at TEXT,
                        used_by INTEGER,
                        created_by INTEGER,
                        FOREIGN KEY (used_by) REFERENCES users (id),
                        FOREIGN KEY (created_by) REFERENCES users (id)
                    )
                """)
                conn.commit()
                logger.info("已创建激活码表(SQLite)")
            
            conn.close()
        
        return True
    except Exception as e:
        logger.error(f"创建激活码表失败: {str(e)}", exc_info=True)
        return False

def generate_activation_code(length=16):
    """生成唯一的激活码"""
    import random
    import string
    
    while True:
        # 生成随机激活码
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
        
        # 检查是否已存在
        existing = execute_query(
            "SELECT id FROM activation_codes WHERE code = %s" if DATABASE_URL.startswith('postgres') else "SELECT id FROM activation_codes WHERE code = ?", 
            (code,), fetch=True)
        if not existing:
            return code

def create_activation_code(package, created_by=None, count=1):
    """创建激活码"""
    codes = []
    now = get_china_time()
    
    for _ in range(count):
        code = generate_activation_code()
        
        if DATABASE_URL.startswith('postgres'):
            result = execute_query("""
                INSERT INTO activation_codes (code, package, created_at, created_by, is_used)
                VALUES (%s, %s, %s, %s, 0)
                RETURNING id
            """, (code, package, now, created_by), fetch=True)
            code_id = result[0][0]
        else:
            conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db"))
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO activation_codes (code, package, created_at, created_by, is_used)
                VALUES (?, ?, ?, ?, 0)
            """, (code, package, now, created_by))
            code_id = cursor.lastrowid
            conn.commit()
            conn.close()
        
        codes.append({"id": code_id, "code": code})
    
    return codes

def get_activation_code(code):
    """获取激活码信息"""
    try:
        if DATABASE_URL.startswith('postgres'):
            result = execute_query("""
                SELECT id, code, package, is_used, created_at, used_at, used_by
                FROM activation_codes
                WHERE code = %s
            """, (code,), fetch=True)
        else:
            result = execute_query("""
                SELECT id, code, package, is_used, created_at, used_at, used_by
                FROM activation_codes
                WHERE code = ?
            """, (code,), fetch=True)
        
        if result and len(result) > 0:
            return {
                "id": result[0][0],
                "code": result[0][1],
                "package": result[0][2],
                "is_used": result[0][3],
                "created_at": result[0][4],
                "used_at": result[0][5],
                "used_by": result[0][6]
            }
        return None
    except Exception as e:
        logger.error(f"获取激活码信息失败: {str(e)}", exc_info=True)
        return None

def mark_activation_code_used(code_id, user_id):
    """标记激活码为已使用"""
    now = get_china_time()
    try:
        # 如果user_id为0或无效值，设置为NULL以避免外键约束错误
        if user_id <= 0:
            user_id = None
            
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL使用事务
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE activation_codes
                SET is_used = 1, used_at = %s, used_by = %s
                WHERE id = %s AND is_used = 0
            """, (now, user_id, code_id))
            
            # 检查是否真的更新了记录
            cursor.execute("""
                SELECT count(*) FROM activation_codes 
                WHERE id = %s AND is_used = 1
            """, (code_id,))
            result = cursor.fetchone()
            rows_updated = cursor.rowcount
            
            conn.commit()
            conn.close()
            
            return rows_updated > 0
        else:
            # SQLite使用事务
            conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db"))
            cursor = conn.cursor()
            
            # 开始事务
            conn.execute("BEGIN TRANSACTION")
            
            # 只更新未使用的激活码
            cursor.execute("""
                UPDATE activation_codes
                SET is_used = 1, used_at = ?, used_by = ?
                WHERE id = ? AND is_used = 0
            """, (now, user_id, code_id))
            
            # 检查是否真的更新了记录
            rows_updated = cursor.rowcount
            
            if rows_updated > 0:
                # 提交事务
                conn.commit()
                conn.close()
                return True
            else:
                # 回滚事务
                conn.rollback()
                conn.close()
                return False
    except Exception as e:
        logger.error(f"标记激活码已使用失败: {str(e)}", exc_info=True)
        return False

def get_admin_activation_codes(limit=100, offset=0, conditions=None, params=None):
    """获取所有激活码（管理员用）"""
    try:
        # 构建WHERE子句
        where_clause = ""
        query_params = []
        
        if conditions and params:
            where_clause = " WHERE " + " AND ".join(conditions)
            query_params.extend(params)
        
        # 添加分页参数
        query_params.extend([limit, offset])
        
        if DATABASE_URL.startswith('postgres'):
            placeholders = ["%s"] * len(query_params)
            result = execute_query(f"""
                SELECT a.id, a.code, a.package, a.is_used, a.created_at, a.used_at, 
                       c.username as creator, u.username as user
                FROM activation_codes a
                LEFT JOIN users c ON a.created_by = c.id
                LEFT JOIN users u ON a.used_by = u.id
                {where_clause}
                ORDER BY a.created_at DESC
                LIMIT %s OFFSET %s
            """, query_params, fetch=True)
        else:
            result = execute_query(f"""
                SELECT a.id, a.code, a.package, a.is_used, a.created_at, a.used_at, 
                       c.username as creator, u.username as user
                FROM activation_codes a
                LEFT JOIN users c ON a.created_by = c.id
                LEFT JOIN users u ON a.used_by = u.id
                {where_clause}
                ORDER BY a.created_at DESC
                LIMIT ? OFFSET ?
            """, query_params, fetch=True)
        
        codes = []
        for r in result:
            codes.append({
                "id": r[0],
                "code": r[1],
                "package": r[2],
                "is_used": r[3],
                "created_at": r[4],
                "used_at": r[5],
                "creator": r[6],
                "user": r[7]
            })
        return codes
    except Exception as e:
        logger.error(f"获取激活码列表失败: {str(e)}", exc_info=True)
        return []

# 用户定制价格函数
def get_user_custom_prices(user_id):
    """
    获取用户的定制价格
    
    参数:
    - user_id: 用户ID
    
    返回:
    - 用户定制价格的字典，键为套餐（如'1'），值为价格
    """
    try:
        if DATABASE_URL.startswith('postgres'):
            results = execute_query("""
                SELECT package, price FROM user_custom_prices
                WHERE user_id = %s
            """, (user_id,), fetch=True)
        else:
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
    except Exception as e:
        logger.error(f"获取用户定制价格失败: {str(e)}", exc_info=True)
        return {}

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
    try:
        now = get_china_time()
        
        # 检查是否已存在该用户的该套餐定制价格
        if DATABASE_URL.startswith('postgres'):
            existing = execute_query("""
                SELECT id FROM user_custom_prices
                WHERE user_id = %s AND package = %s
            """, (user_id, package), fetch=True)
            
            if existing:
                # 更新已有价格
                execute_query("""
                    UPDATE user_custom_prices
                    SET price = %s, created_at = %s, created_by = %s
                    WHERE user_id = %s AND package = %s
                """, (price, now, admin_id, user_id, package))
            else:
                # 添加新价格
                execute_query("""
                    INSERT INTO user_custom_prices
                    (user_id, package, price, created_at, created_by)
                    VALUES (%s, %s, %s, %s, %s)
                """, (user_id, package, price, now, admin_id))
        else:
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
    except Exception as e:
        logger.error(f"设置用户定制价格失败: {str(e)}", exc_info=True)
        return False

def delete_user_custom_price(user_id, package):
    """
    删除用户的定制价格
    
    参数:
    - user_id: 用户ID
    - package: 套餐（如'1'，'2'等）
    
    返回:
    - 成功返回True，失败返回False
    """
    try:
        if DATABASE_URL.startswith('postgres'):
            execute_query("""
                DELETE FROM user_custom_prices
                WHERE user_id = %s AND package = %s
            """, (user_id, package))
        else:
            execute_query("""
                DELETE FROM user_custom_prices
                WHERE user_id = ? AND package = ?
            """, (user_id, package))
        return True
    except Exception as e:
        logger.error(f"删除用户定制价格失败: {str(e)}", exc_info=True)
        return False 

def create_youtube_order_with_deduction_atomic(qrcode_path, package, remark, username, user_id):
    """
    原子性创建油管会员充值订单并扣款
    
    参数:
    - qrcode_path: 上传的二维码图片路径
    - package: 套餐类型
    - remark: 备注
    - username: 用户名
    - user_id: 用户ID
    
    返回:
    - (success, message, new_balance, credit_limit) 元组
      success: 布尔值，表示操作是否成功
      message: 字符串，结果消息
      new_balance: 新的余额
      credit_limit: 透支额度
    """
    try:
        from modules.constants import YOUTUBE_PRICES, get_user_package_price
        
        # 获取套餐价格
        package_price = get_user_package_price(user_id, package)
        if package not in YOUTUBE_PRICES and not package_price:
            logger.warning(f"创建油管会员充值订单失败: 无效的套餐类型 {package}")
            return False, "无效的套餐类型", None, None
        
        # 如果没有定制价格，使用默认价格
        price = package_price or YOUTUBE_PRICES.get(package, 0)
        
        # 检查用户余额
        balance = get_user_balance(user_id)
        credit_limit = get_user_credit_limit(user_id)
        available_funds = balance + credit_limit
        
        if available_funds < price:
            logger.warning(f"创建油管会员充值订单失败: 用户 {username} 余额不足，需要 {price}，现有 {available_funds}")
            return False, f"余额不足，此套餐需要 {price} 元，您的账户余额 {balance} 元，透支额度 {credit_limit} 元", balance, credit_limit
        
        # 获取当前时间
        now = get_china_time()
        
        # 在一个事务中完成所有数据库操作
        if DATABASE_URL.startswith('postgres'):
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False  # 开启事务
        else:
            conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db"))
        
        cursor = conn.cursor()
        
        try:
            # 1. 插入订单记录
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("""
                    INSERT INTO youtube_orders (qrcode_path, package, remark, status, created_at, user_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (qrcode_path, package, remark, STATUS['SUBMITTED'], now, user_id))
                order_id = cursor.fetchone()[0]
            else:
                cursor.execute("""
                    INSERT INTO youtube_orders (qrcode_path, package, remark, status, created_at, user_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (qrcode_path, package, remark, STATUS['SUBMITTED'], now, user_id))
                order_id = cursor.lastrowid
            
            # 2. 扣除用户余额
            new_balance = balance - price
            
            # 更新用户余额
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("UPDATE users SET balance = %s WHERE id = %s", (new_balance, user_id))
            else:
                cursor.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user_id))
            
            # 3. 添加余额变动记录
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("""
                    INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (user_id, -price, 'consume', f"油管会员充值：{package}个月", order_id, new_balance, now))
            else:
                cursor.execute("""
                    INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (user_id, -price, 'consume', f"油管会员充值：{package}个月", order_id, new_balance, now))
            
            # 提交事务
            conn.commit()
            
            logger.info(f"油管会员充值订单创建成功: ID={order_id}, 用户={username}, 套餐={package}, 价格={price}")
            return True, "订单创建成功", new_balance, credit_limit
            
        except Exception as e:
            # 发生错误，回滚事务
            conn.rollback()
            logger.error(f"创建油管会员充值订单事务失败: {str(e)}", exc_info=True)
            return False, f"订单创建失败: {str(e)}", balance, credit_limit
        finally:
            conn.close()
            
    except Exception as e:
        logger.error(f"创建油管会员充值订单失败: {str(e)}", exc_info=True)
        return False, f"订单创建失败: {str(e)}", None, None

def get_youtube_orders(limit=10, offset=0, user_id=None):
    """
    获取油管会员充值订单记录
    
    参数:
    - limit: 限制返回记录数量
    - offset: 起始偏移量
    - user_id: 用户ID，如果提供则只返回该用户的订单
    
    返回:
    - 订单列表
    """
    try:
        if user_id:
            if DATABASE_URL.startswith('postgres'):
                query = """
                    SELECT yo.*, u.username
                    FROM youtube_orders yo
                    LEFT JOIN users u ON yo.user_id = u.id
                    WHERE yo.user_id = %s
                    ORDER BY yo.id DESC
                    LIMIT %s OFFSET %s
                """
                result = execute_query(query, (user_id, limit, offset), fetch=True)
            else:
                query = """
                    SELECT yo.*, u.username
                    FROM youtube_orders yo
                    LEFT JOIN users u ON yo.user_id = u.id
                    WHERE yo.user_id = ?
                    ORDER BY yo.id DESC
                    LIMIT ? OFFSET ?
                """
                result = execute_query(query, (user_id, limit, offset), fetch=True)
        else:
            if DATABASE_URL.startswith('postgres'):
                query = """
                    SELECT yo.*, u.username
                    FROM youtube_orders yo
                    LEFT JOIN users u ON yo.user_id = u.id
                    ORDER BY yo.id DESC
                    LIMIT %s OFFSET %s
                """
                result = execute_query(query, (limit, offset), fetch=True)
            else:
                query = """
                    SELECT yo.*, u.username
                    FROM youtube_orders yo
                    LEFT JOIN users u ON yo.user_id = u.id
                    ORDER BY yo.id DESC
                    LIMIT ? OFFSET ?
                """
                result = execute_query(query, (limit, offset), fetch=True)
        
        return result
    except Exception as e:
        logger.error(f"获取油管会员充值订单失败: {str(e)}", exc_info=True)
        return []

def get_youtube_order_details(oid):
    """
    获取油管会员充值订单详情
    
    参数:
    - oid: 订单ID
    
    返回:
    - 订单详情
    """
    try:
        if DATABASE_URL.startswith('postgres'):
            query = """
                SELECT yo.*, u.username
                FROM youtube_orders yo
                LEFT JOIN users u ON yo.user_id = u.id
                WHERE yo.id = %s
            """
            result = execute_query(query, (oid,), fetch=True)
        else:
            query = """
                SELECT yo.*, u.username
                FROM youtube_orders yo
                LEFT JOIN users u ON yo.user_id = u.id
                WHERE yo.id = ?
            """
            result = execute_query(query, (oid,), fetch=True)
        
        if result and len(result) > 0:
            return result[0]
        return None
    except Exception as e:
        logger.error(f"获取油管会员充值订单详情失败: {str(e)}", exc_info=True)
        return None

def accept_youtube_order_atomic(oid, telegram_id):
    """
    卖家接单（原子操作）
    
    参数:
    - oid: 订单ID
    - telegram_id: 卖家Telegram ID
    
    返回:
    - (success, message, order) 元组
      success: 布尔值，表示操作是否成功
      message: 字符串，结果消息
      order: 订单数据（如果成功）
    """
    try:
        # 获取当前时间
        now = get_china_time()
        
        # 在一个事务中完成所有数据库操作
        if DATABASE_URL.startswith('postgres'):
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False  # 开启事务
        else:
            conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db"))
        
        cursor = conn.cursor()
        
        try:
            # 1. 检查订单是否存在且状态为已提交
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("SELECT status FROM youtube_orders WHERE id = %s", (oid,))
            else:
                cursor.execute("SELECT status FROM youtube_orders WHERE id = ?", (oid,))
            
            result = cursor.fetchone()
            if not result:
                conn.close()
                return False, f"订单 #{oid} 不存在", None
            
            status = result[0]
            if status != STATUS['SUBMITTED']:
                conn.close()
                return False, f"订单 #{oid} 状态为 {status}，无法接单", None
            
            # 2. 获取接单者的信息
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("SELECT username, first_name FROM sellers WHERE telegram_id = %s", (telegram_id,))
            else:
                cursor.execute("SELECT username, first_name FROM sellers WHERE telegram_id = ?", (telegram_id,))
            
            seller_result = cursor.fetchone()
            if not seller_result:
                username = None
                first_name = None
            else:
                username, first_name = seller_result
            
            # 3. 更新订单状态
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("""
                    UPDATE youtube_orders 
                    SET status = %s, accepted_at = %s, accepted_by = %s, accepted_by_username = %s, accepted_by_first_name = %s
                    WHERE id = %s
                """, (STATUS['ACCEPTED'], now, telegram_id, username, first_name, oid))
            else:
                cursor.execute("""
                    UPDATE youtube_orders 
                    SET status = ?, accepted_at = ?, accepted_by = ?, accepted_by_username = ?, accepted_by_first_name = ?
                    WHERE id = ?
                """, (STATUS['ACCEPTED'], now, telegram_id, username, first_name, oid))
            
            # 4. 获取更新后的订单信息
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("""
                    SELECT yo.*, u.username as web_username
                    FROM youtube_orders yo
                    LEFT JOIN users u ON yo.user_id = u.id
                    WHERE yo.id = %s
                """, (oid,))
            else:
                cursor.execute("""
                    SELECT yo.*, u.username as web_username
                    FROM youtube_orders yo
                    LEFT JOIN users u ON yo.user_id = u.id
                    WHERE yo.id = ?
                """, (oid,))
            
            updated_order = cursor.fetchone()
            
            # 提交事务
            conn.commit()
            
            logger.info(f"油管会员充值订单接单成功: ID={oid}, 卖家={telegram_id}")
            return True, "接单成功", updated_order
            
        except Exception as e:
            # 发生错误，回滚事务
            conn.rollback()
            logger.error(f"油管会员充值订单接单事务失败: {str(e)}", exc_info=True)
            return False, f"接单失败: {str(e)}", None
        finally:
            conn.close()
            
    except Exception as e:
        logger.error(f"油管会员充值订单接单失败: {str(e)}", exc_info=True)
        return False, f"接单失败: {str(e)}", None

def complete_youtube_order(oid):
    """
    完成油管会员充值订单
    
    参数:
    - oid: 订单ID
    
    返回:
    - 是否成功
    """
    try:
        now = get_china_time()
        
        if DATABASE_URL.startswith('postgres'):
            execute_query("""
                UPDATE youtube_orders 
                SET status = %s, completed_at = %s
                WHERE id = %s AND status = %s
            """, (STATUS['COMPLETED'], now, oid, STATUS['ACCEPTED']))
        else:
            execute_query("""
                UPDATE youtube_orders 
                SET status = ?, completed_at = ?
                WHERE id = ? AND status = ?
            """, (STATUS['COMPLETED'], now, oid, STATUS['ACCEPTED']))
        
        logger.info(f"油管会员充值订单完成: ID={oid}")
        return True
    except Exception as e:
        logger.error(f"完成油管会员充值订单失败: {str(e)}", exc_info=True)
        return False

def fail_youtube_order(oid, reason="Unknown reason"):
    """
    标记油管会员充值订单失败
    
    参数:
    - oid: 订单ID
    - reason: 失败原因
    
    返回:
    - 是否成功
    """
    try:
        now = get_china_time()
        
        if DATABASE_URL.startswith('postgres'):
            execute_query("""
                UPDATE youtube_orders 
                SET status = %s, completed_at = %s, remark = CASE WHEN remark IS NULL OR remark = '' THEN %s ELSE remark || ' | ' || %s END
                WHERE id = %s AND status = %s
            """, (STATUS['FAILED'], now, reason, reason, oid, STATUS['ACCEPTED']))
        else:
            execute_query("""
                UPDATE youtube_orders 
                SET status = ?, completed_at = ?, remark = CASE WHEN remark IS NULL OR remark = '' THEN ? ELSE remark || ' | ' || ? END
                WHERE id = ? AND status = ?
            """, (STATUS['FAILED'], now, reason, reason, oid, STATUS['ACCEPTED']))
        
        logger.info(f"油管会员充值订单失败: ID={oid}, 原因={reason}")
        return True
    except Exception as e:
        logger.error(f"标记油管会员充值订单失败时出错: {str(e)}", exc_info=True)
        return False

def refund_youtube_order(order_id):
    """
    退款油管会员充值订单
    
    参数:
    - order_id: 订单ID
    
    返回:
    - (success, message, new_balance) 元组
      success: 布尔值，表示操作是否成功
      message: 字符串，结果消息
      new_balance: 新的余额
    """
    try:
        # 获取订单信息
        if DATABASE_URL.startswith('postgres'):
            order = execute_query("""
                SELECT yo.*, u.username, u.id as user_id
                FROM youtube_orders yo
                LEFT JOIN users u ON yo.user_id = u.id
                WHERE yo.id = %s
            """, (order_id,), fetch=True)
        else:
            order = execute_query("""
                SELECT yo.*, u.username, u.id as user_id
                FROM youtube_orders yo
                LEFT JOIN users u ON yo.user_id = u.id
                WHERE yo.id = ?
            """, (order_id,), fetch=True)
        
        if not order or len(order) == 0:
            return False, f"订单 #{order_id} 不存在", None
        
        order = order[0]
        
        # 检查订单是否已退款
        if order[13] == 1:  # refunded字段
            return False, f"订单 #{order_id} 已经退款", None
        
        # 计算退款金额
        from modules.constants import YOUTUBE_PRICES, get_user_package_price
        package = order[2]  # package字段
        user_id = order[12]  # user_id字段
        
        # 获取用户定制价格或默认价格
        package_price = get_user_package_price(user_id, package)
        refund_amount = package_price or YOUTUBE_PRICES.get(package, 0)
        
        # 获取当前余额
        current_balance = get_user_balance(user_id)
        
        # 在一个事务中完成所有数据库操作
        if DATABASE_URL.startswith('postgres'):
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False  # 开启事务
        else:
            conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db"))
        
        cursor = conn.cursor()
        
        try:
            # 1. 更新用户余额
            new_balance = current_balance + refund_amount
            
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("UPDATE users SET balance = %s WHERE id = %s", (new_balance, user_id))
            else:
                cursor.execute("UPDATE users SET balance = ? WHERE id = ?", (new_balance, user_id))
            
            # 2. 标记订单为已退款
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("UPDATE youtube_orders SET refunded = 1 WHERE id = %s", (order_id,))
            else:
                cursor.execute("UPDATE youtube_orders SET refunded = 1 WHERE id = ?", (order_id,))
            
            # 3. 添加余额变动记录
            now = get_china_time()
            
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("""
                    INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (user_id, refund_amount, 'refund', f"油管会员充值退款：订单 #{order_id}", order_id, new_balance, now))
            else:
                cursor.execute("""
                    INSERT INTO balance_records (user_id, amount, type, reason, reference_id, balance_after, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (user_id, refund_amount, 'refund', f"油管会员充值退款：订单 #{order_id}", order_id, new_balance, now))
            
            # 提交事务
            conn.commit()
            
            logger.info(f"油管会员充值订单退款成功: ID={order_id}, 用户ID={user_id}, 金额={refund_amount}, 新余额={new_balance}")
            return True, f"退款成功：¥{refund_amount}", new_balance
            
        except Exception as e:
            # 发生错误，回滚事务
            conn.rollback()
            logger.error(f"油管会员充值订单退款事务失败: {str(e)}", exc_info=True)
            return False, f"退款失败: {str(e)}", current_balance
        finally:
            conn.close()
            
    except Exception as e:
        logger.error(f"油管会员充值订单退款失败: {str(e)}", exc_info=True)
        return False, f"退款失败: {str(e)}", None

def get_unnotified_youtube_orders():
    """获取未通知的油管会员充值订单列表
    
    返回:
    - 未通知的订单列表，每个订单为一个元组，包含id, qrcode_path, package, remark等字段
    """
    try:
        if DATABASE_URL.startswith('postgres'):
            result = execute_query("""
                SELECT id, qrcode_path, package, remark, user_id, created_at, status, notified
                FROM youtube_orders 
                WHERE status = %s AND notified = 0
                ORDER BY created_at ASC
            """, (STATUS['SUBMITTED'],), fetch=True)
        else:
            result = execute_query("""
                SELECT id, qrcode_path, package, remark, user_id, created_at, status, notified
                FROM youtube_orders 
                WHERE status = ? AND notified = 0
                ORDER BY created_at ASC
            """, (STATUS['SUBMITTED'],), fetch=True)
            
        return result
    except Exception as e:
        logger.error(f"获取未通知的油管会员充值订单失败: {str(e)}", exc_info=True)
        return []

def get_youtube_order_details(order_id):
    """获取油管会员充值订单详情
    
    参数:
    - order_id: 订单ID
    
    返回:
    - 包含订单详情的元组，如果订单不存在则返回None
    """
    try:
        if DATABASE_URL.startswith('postgres'):
            result = execute_query("""
                SELECT yo.*, u.username
                FROM youtube_orders yo
                LEFT JOIN users u ON yo.user_id = u.id
                WHERE yo.id = %s
            """, (order_id,), fetch=True)
        else:
            result = execute_query("""
                SELECT yo.*, u.username
                FROM youtube_orders yo
                LEFT JOIN users u ON yo.user_id = u.id
                WHERE yo.id = ?
            """, (order_id,), fetch=True)
            
        if result and len(result) > 0:
            return result[0]
        else:
            return None
    except Exception as e:
        logger.error(f"获取油管会员充值订单详情失败: {str(e)}", exc_info=True)
        return None

def set_youtube_order_notified_atomic(oid):
    """原子性地将油管会员充值订单标记为已通知"""
    try:
        if DATABASE_URL.startswith('postgres'):
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False  # 开启事务
        else:
            conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db"))
        
        cursor = conn.cursor()
        
        try:
            # 检查订单状态
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("SELECT status FROM youtube_orders WHERE id = %s", (oid,))
            else:
                cursor.execute("SELECT status FROM youtube_orders WHERE id = ?", (oid,))
            
            result = cursor.fetchone()
            if not result or result[0] != STATUS['SUBMITTED']:
                # 订单不存在或状态不是已提交，不做更新
                conn.close()
                return False
            
            # 更新订单为已通知
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("UPDATE youtube_orders SET notified = 1 WHERE id = %s", (oid,))
            else:
                cursor.execute("UPDATE youtube_orders SET notified = 1 WHERE id = ?", (oid,))
            
            conn.commit()
            return True
            
        except Exception as e:
            conn.rollback()
            logger.error(f"标记油管会员充值订单为已通知失败: {str(e)}", exc_info=True)
            return False
        finally:
            conn.close()
            
    except Exception as e:
        logger.error(f"标记油管会员充值订单为已通知失败: {str(e)}", exc_info=True)
        return False

def set_order_notified_atomic(oid):
    """原子性地将订单标记为已通知"""
    try:
        if DATABASE_URL.startswith('postgres'):
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False  # 开启事务
        else:
            conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db"))
        
        cursor = conn.cursor()
        
        try:
            # 检查订单状态
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("SELECT status FROM orders WHERE id = %s", (oid,))
            else:
                cursor.execute("SELECT status FROM orders WHERE id = ?", (oid,))
            
            result = cursor.fetchone()
            if not result or result[0] != STATUS['SUBMITTED']:
                # 订单不存在或状态不是已提交，不做更新
                conn.close()
                return False
            
            # 更新订单为已通知
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("UPDATE orders SET notified = 1 WHERE id = %s", (oid,))
            else:
                cursor.execute("UPDATE orders SET notified = 1 WHERE id = ?", (oid,))
            
            conn.commit()
            return True
            
        except Exception as e:
            conn.rollback()
            logger.error(f"标记订单为已通知失败: {str(e)}", exc_info=True)
            return False
        finally:
            conn.close()
            
    except Exception as e:
        logger.error(f"标记订单为已通知失败: {str(e)}", exc_info=True)
        return False