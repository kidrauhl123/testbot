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
    
    # 创建订单表
    c.execute('''
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account TEXT,
        password TEXT,
        package TEXT,
        remark TEXT,
        status TEXT DEFAULT 'submitted',
        created_at TEXT,
        updated_at TEXT,
        user_id INTEGER,
        username TEXT,
        accepted_by TEXT,
        accepted_at TEXT,
        completed_at TEXT,
        notified INTEGER DEFAULT 0
    )
    ''')
    
    # 创建用户表
    c.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        email TEXT,
        is_admin INTEGER DEFAULT 0,
        created_at TEXT,
        balance REAL DEFAULT 0,
        credit_limit REAL DEFAULT 0
    )
    ''')
    
    # 创建卖家表
    c.execute('''
    CREATE TABLE IF NOT EXISTS sellers (
        telegram_id TEXT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        nickname TEXT,
        is_active INTEGER DEFAULT 1,
        added_at TEXT,
        added_by TEXT,
        is_admin INTEGER DEFAULT 0,
        last_active_at TEXT,
        desired_orders INTEGER DEFAULT 0,
        activity_check_at TEXT
    )
    ''')
    
    # 检查sellers表是否需要添加新字段
    try:
        c.execute("SELECT last_active_at FROM sellers LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE sellers ADD COLUMN last_active_at TEXT")
        conn.commit()
    
    try:
        c.execute("SELECT desired_orders FROM sellers LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE sellers ADD COLUMN desired_orders INTEGER DEFAULT 0")
        conn.commit()
    
    try:
        c.execute("SELECT activity_check_at FROM sellers LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE sellers ADD COLUMN activity_check_at TEXT")
        conn.commit()
    
    # 检查sellers表是否需要添加nickname列
    try:
        c.execute("SELECT nickname FROM sellers LIMIT 1")
    except sqlite3.OperationalError:
        logger.info("为sellers表添加nickname列")
        c.execute("ALTER TABLE sellers ADD COLUMN nickname TEXT")
        conn.commit()
    
    # 检查sellers表是否需要添加is_admin列
    try:
        c.execute("SELECT is_admin FROM sellers LIMIT 1")
    except sqlite3.OperationalError:
        logger.info("为sellers表添加is_admin列")
        c.execute("ALTER TABLE sellers ADD COLUMN is_admin INTEGER DEFAULT 0")
        conn.commit()
    
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
    
    # 创建管理员账户（如果不存在）
    c.execute("SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,))
    if not c.fetchone():
        hashed_password = hash_password(ADMIN_PASSWORD)
        timestamp = get_china_time()
        c.execute(
            "INSERT INTO users (username, password, is_admin, created_at) VALUES (?, ?, ?, ?)", 
            (ADMIN_USERNAME, hashed_password, 1, timestamp)
        )
        logger.info(f"已创建管理员账户: {ADMIN_USERNAME}")
    
    conn.commit()
    conn.close()

def init_postgres_db():
    """初始化PostgreSQL数据库"""
    logger.info("使用PostgreSQL数据库")
    
    try:
        # 解析数据库URL
        url = urlparse(DATABASE_URL)
        dbname = url.path[1:]
        user = url.username
        password = url.password
        host = url.hostname
        port = url.port
        
        logger.info(f"初始化数据库: {host}:{port}/{dbname}")
        
        # 连接数据库
        conn = psycopg2.connect(
            dbname=dbname,
            user=user,
            password=password,
            host=host,
            port=port
        )
        cursor = conn.cursor()
        
        # 创建订单表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            account TEXT,
            password TEXT,
            package TEXT,
            remark TEXT,
            status TEXT DEFAULT 'submitted',
            created_at TEXT,
            updated_at TEXT,
            user_id INTEGER,
            username TEXT,
            accepted_by TEXT,
            accepted_at TEXT,
            completed_at TEXT,
            notified INTEGER DEFAULT 0,
            web_user_id INTEGER,
            handler_id TEXT
        )
        ''')
        
        # 检查是否需要添加web_user_id列
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='orders' AND column_name='web_user_id'")
        if not cursor.fetchone():
            logger.info("为orders表添加web_user_id列")
            cursor.execute("ALTER TABLE orders ADD COLUMN web_user_id INTEGER")
            
        # 检查是否需要添加handler_id列
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='orders' AND column_name='handler_id'")
        if not cursor.fetchone():
            logger.info("为orders表添加handler_id列")
            cursor.execute("ALTER TABLE orders ADD COLUMN handler_id TEXT")
        
        # 创建用户表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            password TEXT,
            email TEXT,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT,
            balance NUMERIC DEFAULT 0,
            credit_limit NUMERIC DEFAULT 0
        )
        ''')
        
        # 创建卖家表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS sellers (
            telegram_id TEXT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            nickname TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            added_at TEXT,
            added_by TEXT,
            is_admin BOOLEAN DEFAULT FALSE,
            last_active_at TEXT,
            desired_orders INTEGER DEFAULT 0,
            activity_check_at TEXT
        )
        ''')
        
        # 检查sellers表是否需要添加新字段
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='sellers' AND column_name='last_active_at'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE sellers ADD COLUMN last_active_at TEXT")
        
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='sellers' AND column_name='desired_orders'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE sellers ADD COLUMN desired_orders INTEGER DEFAULT 0")
        
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='sellers' AND column_name='activity_check_at'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE sellers ADD COLUMN activity_check_at TEXT")
        
        # 检查sellers表是否需要添加nickname列
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='sellers' AND column_name='nickname'")
        if not cursor.fetchone():
            logger.info("为sellers表添加nickname列")
            cursor.execute("ALTER TABLE sellers ADD COLUMN nickname TEXT")
        
        # 检查sellers表是否需要添加is_admin列
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='sellers' AND column_name='is_admin'")
        if not cursor.fetchone():
            logger.info("为sellers表添加is_admin列")
            cursor.execute("ALTER TABLE sellers ADD COLUMN is_admin BOOLEAN DEFAULT FALSE")
        
        # 创建管理员账户（如果不存在）
        cursor.execute("SELECT id FROM users WHERE username=%s", (ADMIN_USERNAME,))
        if not cursor.fetchone():
            hashed_password = hash_password(ADMIN_PASSWORD)
            timestamp = get_china_time()
            cursor.execute(
                "INSERT INTO users (username, password, is_admin, created_at) VALUES (%s, %s, %s, %s)", 
                (ADMIN_USERNAME, hashed_password, 1, timestamp)
            )
            logger.info(f"已创建管理员账户: {ADMIN_USERNAME}")
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"初始化PostgreSQL数据库失败: {str(e)}", exc_info=True)
        raise

def execute_query(query, params=(), fetch=False, return_cursor=False):
    """执行数据库查询"""
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
        
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
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
    
    # 确保整数类型匹配 - 处理status字段类型不匹配的问题
    # 创建新的参数列表，对可能需要类型转换的参数进行处理
    processed_params = []
    for param in params:
        # 如果是整数类型的状态值，转为字符串
        if isinstance(param, int) and param in range(10):  # 假设状态值是0-9之间的整数
            processed_params.append(str(param))
        else:
            processed_params.append(param)
    
    cursor.execute(query, processed_params)
    
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
    if DATABASE_URL.startswith('postgres'):
        # PostgreSQL版本 - 需要使用显式类型转换
        orders = execute_query("""
            SELECT id, account, password, package, created_at, web_user_id 
            FROM orders 
            WHERE notified = 0 AND status = %s
        """, (STATUS['SUBMITTED'],), fetch=True)
    else:
        # SQLite版本
        orders = execute_query("""
            SELECT id, account, password, package, created_at, web_user_id 
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

# ===== 卖家管理 =====
def get_all_sellers():
    """获取所有卖家信息"""
    if DATABASE_URL.startswith('postgres'):
        # PostgreSQL需要显式处理BOOLEAN类型
        return execute_query("""
            SELECT telegram_id, username, first_name, nickname, is_active, 
                   added_at, added_by, 
                   COALESCE(is_admin, FALSE) as is_admin 
            FROM sellers 
            ORDER BY added_at DESC
        """, fetch=True)
    else:
        # SQLite版本
        return execute_query("""
            SELECT telegram_id, username, first_name, nickname, is_active, 
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

def get_active_sellers():
    """获取所有活跃的卖家的ID和昵称"""
    if DATABASE_URL.startswith('postgres'):
        sellers = execute_query("""
            SELECT telegram_id, nickname, username, first_name, 
                   last_active_at, desired_orders
            FROM sellers 
            WHERE is_active = TRUE
        """, fetch=True)
    else:
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

def add_seller(telegram_id, username, first_name, nickname, added_by):
    """添加新卖家"""
    timestamp = get_china_time()
    execute_query(
        "INSERT INTO sellers (telegram_id, username, first_name, nickname, added_at, added_by) VALUES (?, ?, ?, ?, ?, ?)",
        (telegram_id, username, first_name, nickname, timestamp, added_by)
    )

def toggle_seller_status(telegram_id):
    """切换卖家活跃状态"""
    execute_query("UPDATE sellers SET is_active = NOT is_active WHERE telegram_id = ?", (telegram_id,))

def remove_seller(telegram_id):
    """移除卖家"""
    return execute_query("DELETE FROM sellers WHERE telegram_id=?", (telegram_id,))

def update_seller_last_active(telegram_id):
    """更新卖家最后活跃时间"""
    timestamp = get_china_time()
    execute_query(
        "UPDATE sellers SET last_active_at = ? WHERE telegram_id = ?",
        (timestamp, telegram_id)
    )

def update_seller_desired_orders(telegram_id, desired_orders):
    """更新卖家期望接单数量"""
    execute_query(
        "UPDATE sellers SET desired_orders = ? WHERE telegram_id = ?",
        (desired_orders, telegram_id)
    ) 