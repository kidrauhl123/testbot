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
                return execute_query("""
                    SELECT id, user_id, amount, type, reason, reference_id, balance_after, created_at
                    FROM balance_records
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                """, (user_id, limit, offset), fetch=True)
            else:
                return execute_query("""
                    SELECT id, user_id, amount, type, reason, reference_id, balance_after, created_at
                    FROM balance_records
                    WHERE user_id = ?
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                """, (user_id, limit, offset), fetch=True)
        else:
            if DATABASE_URL.startswith('postgres'):
                return execute_query("""
                    SELECT br.id, br.user_id, u.username, br.amount, br.type, 
                           br.reason, br.reference_id, br.balance_after, br.created_at
                    FROM balance_records br
                    LEFT JOIN users u ON br.user_id = u.id
                    ORDER BY br.created_at DESC
                    LIMIT %s OFFSET %s
                """, (limit, offset), fetch=True)
            else:
                return execute_query("""
                    SELECT br.id, br.user_id, u.username, br.amount, br.type, 
                           br.reason, br.reference_id, br.balance_after, br.created_at
                    FROM balance_records br
                    LEFT JOIN users u ON br.user_id = u.id
                    ORDER BY br.created_at DESC
                    LIMIT ? OFFSET ?
                """, (limit, offset), fetch=True)
    except Exception as e:
        logger.error(f"获取余额记录失败: {str(e)}", exc_info=True)
        return []

def update_user_balance(user_id, amount):
    """
    更新用户余额
    
    参数:
    - user_id: 用户ID
    - amount: 变动金额（正数表示增加，负数表示减少）
    
    返回:
    - (success, message, new_balance)
    """
    try:
        # 开始事务
        if DATABASE_URL.startswith('postgres'):
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False
        else:
            # 使用绝对路径访问数据库
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "orders.db")
            conn = sqlite3.connect(db_path)
        
        cursor = conn.cursor()
        
        # 获取当前余额
        if DATABASE_URL.startswith('postgres'):
            cursor.execute("SELECT balance, credit_limit FROM users WHERE id=%s", (user_id,))
        else:
            cursor.execute("SELECT balance, credit_limit FROM users WHERE id=?", (user_id,))
            
        result = cursor.fetchone()
        if not result:
            conn.close()
            return False, "用户不存在", 0
            
        current_balance, credit_limit = result
        
        # 计算新余额
        new_balance = current_balance + amount
        
        # 如果是减少余额，检查是否超过可用余额
        if amount < 0 and new_balance < -credit_limit:
            conn.close()
            return False, f"余额不足，当前余额: {current_balance}，透支额度: {credit_limit}", current_balance
        
        # 更新余额
        if DATABASE_URL.startswith('postgres'):
            cursor.execute("UPDATE users SET balance=%s WHERE id=%s", (new_balance, user_id))
        else:
            cursor.execute("UPDATE users SET balance=? WHERE id=?", (new_balance, user_id))
        
        conn.commit()
        conn.close()
        
        return True, "余额更新成功", new_balance
    except Exception as e:
        logger.error(f"更新用户余额失败: {str(e)}", exc_info=True)
        return False, f"更新余额失败: {str(e)}", 0

def set_user_balance(user_id, balance):
    """
    设置用户余额（仅限管理员使用）
    
    参数:
    - user_id: 用户ID
    - balance: 新余额
    
    返回:
    - (success, message, new_balance)
    """
    try:
        if DATABASE_URL.startswith('postgres'):
            conn = psycopg2.connect(DATABASE_URL)
        else:
            # 使用绝对路径访问数据库
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "orders.db")
            conn = sqlite3.connect(db_path)
            
        cursor = conn.cursor()
        
        # 检查用户是否存在
        if DATABASE_URL.startswith('postgres'):
            cursor.execute("SELECT id FROM users WHERE id=%s", (user_id,))
        else:
            cursor.execute("SELECT id FROM users WHERE id=?", (user_id,))
            
        if not cursor.fetchone():
            conn.close()
            return False, "用户不存在", 0
            
        # 获取当前余额
        if DATABASE_URL.startswith('postgres'):
            cursor.execute("SELECT balance FROM users WHERE id=%s", (user_id,))
        else:
            cursor.execute("SELECT balance FROM users WHERE id=?", (user_id,))
            
        current_balance = cursor.fetchone()[0]
        
        # 更新余额
        if DATABASE_URL.startswith('postgres'):
            cursor.execute("UPDATE users SET balance=%s WHERE id=%s", (balance, user_id))
        else:
            cursor.execute("UPDATE users SET balance=? WHERE id=?", (balance, user_id))
            
        conn.commit()
        
        # 记录余额变动
        change = balance - current_balance
        if change != 0:
            timestamp = get_china_time()
            
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("""
                    INSERT INTO balance_records (user_id, amount, type, reason, balance_after, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (user_id, change, 'admin', '管理员调整', balance, timestamp))
            else:
                cursor.execute("""
                    INSERT INTO balance_records (user_id, amount, type, reason, balance_after, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (user_id, change, 'admin', '管理员调整', balance, timestamp))
                
            conn.commit()
        
        conn.close()
        return True, "余额设置成功", balance
    except Exception as e:
        logger.error(f"设置用户余额失败: {str(e)}", exc_info=True)
        return False, f"设置余额失败: {str(e)}", 0

def check_balance_for_package(user_id, package):
    """
    检查用户余额是否足够购买指定套餐
    
    参数:
    - user_id: 用户ID
    - package: 套餐ID
    
    返回:
    - (enough, balance, price, credit_limit)
    """
    from modules.constants import get_user_package_price
    
    # 获取套餐价格
    price = get_user_package_price(user_id, package)
    
    # 获取用户余额和透支额度
    balance = get_user_balance(user_id)
    credit_limit = get_user_credit_limit(user_id)
    
    # 检查余额是否足够
    enough = balance + credit_limit >= price
    
    return enough, balance, price, credit_limit

def refund_order(order_id):
    """
    退款订单
    
    参数:
    - order_id: 订单ID
    
    返回:
    - (success, message_or_new_balance)
    """
    try:
        # 开始事务
        if DATABASE_URL.startswith('postgres'):
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False
        else:
            # 使用绝对路径访问数据库
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "orders.db")
            conn = sqlite3.connect(db_path)
            
        cursor = conn.cursor()
        
        # 获取订单信息
        if DATABASE_URL.startswith('postgres'):
            cursor.execute("""
                SELECT o.id, o.package, o.web_user_id, o.status, o.refunded 
                FROM orders o WHERE o.id = %s
            """, (order_id,))
        else:
            cursor.execute("""
                SELECT o.id, o.package, o.web_user_id, o.status, o.refunded 
                FROM orders o WHERE o.id = ?
            """, (order_id,))
            
        order_info = cursor.fetchone()
        
        if not order_info:
            conn.close()
            return False, "订单不存在"
            
        order_id, package, user_id, status, refunded = order_info
        
        # 检查是否已退款
        if refunded:
            conn.close()
            return False, "订单已退款"
        
        # 使用常量
        from modules.constants import STATUS, get_user_package_price
        
        # 检查订单状态是否允许退款（只有已取消或失败的订单可以退款）
        if status not in [STATUS['CANCELLED'], STATUS['FAILED']]:
            conn.close()
            return False, f"订单状态不允许退款：{status}"
            
        # 获取退款金额（套餐价格）
        refund_amount = get_user_package_price(user_id, package)
        
        # 更新用户余额
        if DATABASE_URL.startswith('postgres'):
            cursor.execute("""
                UPDATE users 
                SET balance = balance + %s 
                WHERE id = %s
                RETURNING balance
            """, (refund_amount, user_id))
            
            new_balance = cursor.fetchone()[0]
        else:
            cursor.execute("""
                UPDATE users 
                SET balance = balance + ? 
                WHERE id = ?
            """, (refund_amount, user_id))
            
            cursor.execute("SELECT balance FROM users WHERE id = ?", (user_id,))
            new_balance = cursor.fetchone()[0]
            
        # 标记订单为已退款
        if DATABASE_URL.startswith('postgres'):
            cursor.execute("""
                UPDATE orders 
                SET refunded = 1
                WHERE id = %s
            """, (order_id,))
        else:
            cursor.execute("""
                UPDATE orders 
                SET refunded = 1
                WHERE id = ?
            """, (order_id,))
            
        # 记录余额变动
        timestamp = get_china_time()
        
        if DATABASE_URL.startswith('postgres'):
            cursor.execute("""
                INSERT INTO balance_records 
                (user_id, amount, type, reason, reference_id, balance_after, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (user_id, refund_amount, 'refund', f'订单退款 #{order_id}', order_id, new_balance, timestamp))
        else:
            cursor.execute("""
                INSERT INTO balance_records 
                (user_id, amount, type, reason, reference_id, balance_after, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, refund_amount, 'refund', f'订单退款 #{order_id}', order_id, new_balance, timestamp))
            
        conn.commit()
        conn.close()
        
        return True, new_balance
    except Exception as e:
        logger.error(f"退款订单 {order_id} 失败: {str(e)}", exc_info=True)
        return False, f"退款失败: {str(e)}"

def create_recharge_tables():
    """创建充值记录表和余额记录表"""
    try:
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL版本
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            
            # 检查充值记录表是否存在
            cursor.execute("SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='recharge_requests')")
            table_exists = cursor.fetchone()[0]
            
            # 如果不存在，创建充值记录表
            if not table_exists:
                cursor.execute("""
                    CREATE TABLE recharge_requests (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        amount NUMERIC NOT NULL,
                        status TEXT NOT NULL,
                        payment_method TEXT NOT NULL,
                        proof_image TEXT,
                        details TEXT,
                        created_at TEXT NOT NULL,
                        processed_at TEXT,
                        processed_by TEXT,
                        reference_order_id INTEGER
                    )
                """)
                conn.commit()
                logger.info("已创建充值记录表(PostgreSQL)")
            
            # 检查余额明细表是否存在
            cursor.execute("SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='balance_records')")
            table_exists = cursor.fetchone()[0]
            
            # 如果不存在，创建余额明细表
            if not table_exists:
                cursor.execute("""
                    CREATE TABLE balance_records (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        amount NUMERIC NOT NULL,
                        type TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        reference_id INTEGER,
                        balance_after NUMERIC NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """)
                conn.commit()
                logger.info("已创建余额明细表(PostgreSQL)")
            
            # 检查recharge_requests表中是否存在reference_order_id字段
            cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='recharge_requests' AND column_name='reference_order_id'")
            if not cursor.fetchone():
                # 添加reference_order_id字段
                cursor.execute("ALTER TABLE recharge_requests ADD COLUMN reference_order_id INTEGER")
                conn.commit()
                logger.info("已向充值记录表添加reference_order_id字段")
            
            conn.close()
        else:
            # SQLite版本
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "orders.db")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # 检查充值记录表是否存在
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
                        reference_order_id INTEGER
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
                
            # 检查recharge_requests表中是否存在reference_order_id字段
            cursor.execute("PRAGMA table_info(recharge_requests)")
            columns = cursor.fetchall()
            column_names = [column[1] for column in columns]
            
            if 'reference_order_id' not in column_names:
                # 添加reference_order_id字段
                cursor.execute("ALTER TABLE recharge_requests ADD COLUMN reference_order_id INTEGER")
                conn.commit()
                logger.info("已向充值记录表添加reference_order_id字段")
            
            conn.close()
        
        return True
    except Exception as e:
        logger.error(f"创建充值记录表或余额明细表失败: {str(e)}", exc_info=True)
        return False

def create_activation_code_table():
    """创建激活码表"""
    try:
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL版本
            conn = psycopg2.connect(DATABASE_URL)
            cursor = conn.cursor()
            
            # 检查激活码表是否存在
            cursor.execute("SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='activation_codes')")
            table_exists = cursor.fetchone()[0]
            
            # 如果不存在，创建激活码表
            if not table_exists:
                cursor.execute("""
                    CREATE TABLE activation_codes (
                        id SERIAL PRIMARY KEY,
                        code TEXT NOT NULL UNIQUE,
                        package TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        created_by TEXT,
                        used_at TEXT,
                        used_by INTEGER,
                        status TEXT NOT NULL DEFAULT 'active'
                    )
                """)
                conn.commit()
                logger.info("已创建激活码表(PostgreSQL)")
            
            conn.close()
        else:
            # SQLite版本
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
                        code TEXT NOT NULL UNIQUE,
                        package TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        created_by TEXT,
                        used_at TEXT,
                        used_by INTEGER,
                        status TEXT NOT NULL DEFAULT 'active'
                    )
                """)
                conn.commit()
                logger.info("已创建激活码表(SQLite)")
            
            conn.close()
        
        return True
    except Exception as e:
        logger.error(f"创建激活码表失败: {str(e)}", exc_info=True)
        return False 