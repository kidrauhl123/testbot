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
    """初始化数据库"""
    try:
        if DATABASE_URL.startswith('postgres'):
            init_postgres_db()
        else:
            init_sqlite_db()
        
        # 创建充值相关表
        create_recharge_tables()
        
        logger.info("数据库初始化完成")
    except Exception as e:
        logger.error(f"数据库初始化失败: {str(e)}", exc_info=True)

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
        notified INTEGER DEFAULT 0,
        accepted_by_username TEXT,
        accepted_by_first_name TEXT,
        accepted_by_nickname TEXT,
        failed_at TEXT,
        fail_reason TEXT,
        buyer_confirmed INTEGER DEFAULT 0
    )
    ''')
    
    # 检查orders表是否需要添加accepted_by_nickname列
    try:
        c.execute("SELECT accepted_by_nickname FROM orders LIMIT 1")
    except sqlite3.OperationalError:
        logger.info("为orders表添加accepted_by_nickname列")
        c.execute("ALTER TABLE orders ADD COLUMN accepted_by_nickname TEXT")
        conn.commit()
    
    # 检查orders表是否需要添加buyer_confirmed列
    try:
        c.execute("SELECT buyer_confirmed FROM orders LIMIT 1")
    except sqlite3.OperationalError:
        logger.info("为orders表添加buyer_confirmed列")
        c.execute("ALTER TABLE orders ADD COLUMN buyer_confirmed INTEGER DEFAULT 0")
        conn.commit()
    
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
        activity_check_at TEXT,
        distribution_level INTEGER DEFAULT 1,
        max_orders INTEGER DEFAULT 3
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
    
    # 检查sellers表是否需要添加distribution_level列
    try:
        c.execute("SELECT distribution_level FROM sellers LIMIT 1")
    except sqlite3.OperationalError:
        logger.info("为sellers表添加distribution_level列")
        c.execute("ALTER TABLE sellers ADD COLUMN distribution_level INTEGER DEFAULT 1")
        conn.commit()
    
    # 检查sellers表是否需要添加max_orders列
    try:
        c.execute("SELECT max_orders FROM sellers LIMIT 1")
    except sqlite3.OperationalError:
        logger.info("为sellers表添加max_orders列")
        c.execute("ALTER TABLE sellers ADD COLUMN max_orders INTEGER DEFAULT 3")
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
    logger.info("使用PostgreSQL数据库")
    
    # 解析数据库URL
    url = urlparse(DATABASE_URL)
    
    # 连接到数据库
    try:
        conn = psycopg2.connect(
            dbname=url.path[1:],
            user=url.username,
            password=url.password,
            host=url.hostname,
            port=url.port
        )
        conn.autocommit = True
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            user_id INTEGER,
            username TEXT,
            accepted_by TEXT,
            accepted_at TIMESTAMP,
            completed_at TIMESTAMP,
            notified BOOLEAN DEFAULT FALSE,
            accepted_by_username TEXT,
            accepted_by_first_name TEXT,
            accepted_by_nickname TEXT,
            failed_at TIMESTAMP,
            fail_reason TEXT,
            buyer_confirmed BOOLEAN DEFAULT FALSE
        )
        ''')
        
        # 检查orders表是否需要添加buyer_confirmed列
        cursor.execute("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name='orders' AND column_name='buyer_confirmed'
        """)
        if not cursor.fetchone():
            logger.info("为orders表添加buyer_confirmed列")
            cursor.execute("ALTER TABLE orders ADD COLUMN buyer_confirmed BOOLEAN DEFAULT FALSE")
        
        # 创建用户表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            password_hash TEXT,
            email TEXT,
            is_admin BOOLEAN DEFAULT FALSE,
            created_at TEXT,
            balance REAL DEFAULT 0,
            credit_limit REAL DEFAULT 0
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
            activity_check_at TEXT,
            distribution_level INTEGER DEFAULT 1,
            max_orders INTEGER DEFAULT 3
        )
        ''')
        
        # 检查sellers表是否需要添加nickname列
        cursor.execute("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name='sellers' AND column_name='nickname'
        """)
        if not cursor.fetchone():
            logger.info("为sellers表添加nickname列")
            cursor.execute("ALTER TABLE sellers ADD COLUMN nickname TEXT")
        
        # 检查sellers表是否需要添加is_admin列
        cursor.execute("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name='sellers' AND column_name='is_admin'
        """)
        if not cursor.fetchone():
            logger.info("为sellers表添加is_admin列")
            cursor.execute("ALTER TABLE sellers ADD COLUMN is_admin BOOLEAN DEFAULT FALSE")
        
        # 检查sellers表是否需要添加last_active_at列
        cursor.execute("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name='sellers' AND column_name='last_active_at'
        """)
        if not cursor.fetchone():
            logger.info("为sellers表添加last_active_at列")
            cursor.execute("ALTER TABLE sellers ADD COLUMN last_active_at TEXT")
        
        # 检查sellers表是否需要添加desired_orders列
        cursor.execute("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name='sellers' AND column_name='desired_orders'
        """)
        if not cursor.fetchone():
            logger.info("为sellers表添加desired_orders列")
            cursor.execute("ALTER TABLE sellers ADD COLUMN desired_orders INTEGER DEFAULT 0")
        
        # 检查sellers表是否需要添加activity_check_at列
        cursor.execute("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name='sellers' AND column_name='activity_check_at'
        """)
        if not cursor.fetchone():
            logger.info("为sellers表添加activity_check_at列")
            cursor.execute("ALTER TABLE sellers ADD COLUMN activity_check_at TEXT")
        
        # 检查sellers表是否需要添加distribution_level列
        cursor.execute("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name='sellers' AND column_name='distribution_level'
        """)
        if not cursor.fetchone():
            logger.info("为sellers表添加distribution_level列")
            cursor.execute("ALTER TABLE sellers ADD COLUMN distribution_level INTEGER DEFAULT 1")
        
        # 检查sellers表是否需要添加max_orders列
        cursor.execute("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name='sellers' AND column_name='max_orders'
        """)
        if not cursor.fetchone():
            logger.info("为sellers表添加max_orders列")
            cursor.execute("ALTER TABLE sellers ADD COLUMN max_orders INTEGER DEFAULT 3")
        
        # 创建超级管理员账号（如果不存在）
        admin_hash = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()
        cursor.execute("SELECT id FROM users WHERE username = %s", (ADMIN_USERNAME,))
        if not cursor.fetchone():
            logger.info(f"创建默认管理员账号: {ADMIN_USERNAME}")
            cursor.execute("""
                INSERT INTO users (username, password_hash, is_admin, created_at) 
                VALUES (%s, %s, TRUE, %s)
            """, (ADMIN_USERNAME, admin_hash, get_china_time()))
        
        # 关闭连接
        cursor.close()
        conn.close()
        
    except Exception as e:
        logger.error(f"初始化PostgreSQL数据库失败: {str(e)}", exc_info=True)

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

# ===== 卖家管理 =====
def get_all_sellers():
    """获取所有卖家列表"""
    try:
        if DATABASE_URL.startswith('postgres'):
            return execute_query("""
                SELECT telegram_id, username, first_name, nickname, is_active, 
                       added_at, added_by, 
                       COALESCE(is_admin, FALSE) as is_admin,
                       COALESCE(distribution_level, 1) as distribution_level,
                       COALESCE(max_orders, 3) as max_orders
                FROM sellers
                ORDER BY added_at DESC
            """, fetch=True)
        else:
            conn = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders.db"))
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("""
                SELECT telegram_id, username, first_name, nickname, is_active, added_at, added_by, is_admin, distribution_level, max_orders
                FROM sellers
                ORDER BY added_at DESC
            """)
            results = c.fetchall()
            conn.close()
            return results
    except Exception as e:
        logger.error(f"获取卖家列表失败: {str(e)}", exc_info=True)
        return []

def get_active_seller_ids():
    """获取所有活跃的卖家ID"""
    if DATABASE_URL.startswith('postgres'):
        sellers = execute_query("SELECT telegram_id FROM sellers WHERE is_active = TRUE", fetch=True)
    else:
        sellers = execute_query("SELECT telegram_id FROM sellers WHERE is_active = 1", fetch=True)
    
    return [seller[0] for seller in sellers] if sellers else []

def get_seller_info(telegram_id):
    """
    获取指定卖家的信息
    
    参数:
    - telegram_id: 卖家的Telegram ID
    
    返回:
    - 包含卖家信息的字典，如果卖家不存在则返回None
    """
    try:
        if DATABASE_URL.startswith('postgres'):
            result = execute_query("""
                SELECT telegram_id, nickname, username, first_name, is_active
                FROM sellers
                WHERE telegram_id = %s
            """, (telegram_id,), fetch=True)
        else:
            result = execute_query("""
                SELECT telegram_id, nickname, username, first_name, is_active
                FROM sellers
                WHERE telegram_id = ?
            """, (telegram_id,), fetch=True)
        
        if not result:
            logger.warning(f"卖家 {telegram_id} 不存在")
            return None
            
        seller = result[0]
        telegram_id, nickname, username, first_name, is_active = seller
        
        # 如果没有设置昵称，则使用first_name或username作为默认昵称
        display_name = nickname or first_name or f"Seller {telegram_id}"
        
        return {
            "telegram_id": telegram_id,
            "nickname": nickname,
            "username": username,
            "first_name": first_name, 
            "display_name": display_name,
            "is_active": bool(is_active)
        }
    except Exception as e:
        logger.error(f"获取卖家 {telegram_id} 信息失败: {str(e)}", exc_info=True)
        return None

def get_active_sellers():
    """获取所有活跃的卖家的ID和昵称"""
    if DATABASE_URL.startswith('postgres'):
        sellers = execute_query("""
            SELECT telegram_id, nickname, username, first_name, 
                   last_active_at
            FROM sellers 
            WHERE is_active = TRUE
        """, fetch=True)
    else:
        sellers = execute_query("""
            SELECT telegram_id, nickname, username, first_name, 
                   last_active_at
            FROM sellers 
            WHERE is_active = 1
        """, fetch=True)
    
    result = []
    for seller in sellers:
        telegram_id, nickname, username, first_name, last_active_at = seller
        # 如果没有设置昵称，则使用first_name或username作为默认昵称
        display_name = nickname or first_name or f"卖家 {telegram_id}"
        result.append({
            "id": telegram_id,
            "name": display_name,
            "last_active_at": last_active_at or ""
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

# ===== 充值相关函数 =====
def create_recharge_tables():
    """创建充值相关表"""
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

def update_seller_nickname(telegram_id, nickname):
    """更新卖家昵称"""
    execute_query(
        "UPDATE sellers SET nickname = ? WHERE telegram_id = ?",
        (nickname, telegram_id)
    )
    logger.info(f"已更新卖家 {telegram_id} 的昵称为 {nickname}")

def update_seller_last_active(telegram_id):
    """更新卖家最后活跃时间"""
    timestamp = get_china_time()
    execute_query(
        "UPDATE sellers SET last_active_at = ? WHERE telegram_id = ?",
        (timestamp, telegram_id)
    )

def get_seller_completed_orders(telegram_id):
    """获取卖家已完成的订单数（以买家已确认为准）"""
    if DATABASE_URL.startswith('postgres'):
        result = execute_query(
            "SELECT COUNT(*) FROM orders WHERE accepted_by = ? AND buyer_confirmed = TRUE",
            (telegram_id,),
            fetch=True
        )
    else:
        result = execute_query(
            "SELECT COUNT(*) FROM orders WHERE accepted_by = ? AND buyer_confirmed = 1",
            (telegram_id,),
            fetch=True
        )
    
    if result and len(result) > 0:
        return result[0][0]
    return 0

def get_user_today_confirmed_count(user_id):
    """获取指定用户今天已确认的订单数"""
    from datetime import datetime
    import pytz
    today = datetime.now(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d")
    
    try:
        # 根据数据库类型选择不同查询语句
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL使用to_char函数转换日期格式
            query = """
                SELECT COUNT(*) FROM orders 
                WHERE user_id = %s 
                AND status = 'completed' 
                AND to_char(updated_at::timestamp, 'YYYY-MM-DD') = %s
            """
            params = (user_id, today)
        else:
            # SQLite继续使用LIKE
            query = "SELECT COUNT(*) FROM orders WHERE user_id = ? AND status = 'completed' AND updated_at LIKE ?"
            params = (user_id, f"{today}%")
            
        result = execute_query(query, params, fetch=True)
        count = result[0][0] if result and result[0] else 0
        logger.info(f"用户 {user_id} 今日充值成功订单数: {count}")
        return count
    except Exception as e:
        logger.error(f"获取用户今日确认订单数失败: {str(e)}", exc_info=True)
        return 0

def get_all_today_confirmed_count():
    """获取所有用户今天已确认的订单总数"""
    from datetime import datetime
    import pytz
    
    today = datetime.now(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d")
    logger.info(f"查询今日({today})充值成功订单...")
    
    try:
        # 首先，查询所有状态为completed的订单，不考虑日期
        all_completed_query = "SELECT id, status, updated_at FROM orders WHERE status = 'completed'"
        all_completed = execute_query(all_completed_query, (), fetch=True)
        logger.info(f"所有充值成功订单数: {len(all_completed) if all_completed else 0}")
        if all_completed:
            for order in all_completed:
                logger.info(f"订单ID: {order[0]}, 状态: {order[1]}, 更新时间: {order[2]}")
        
        # 根据数据库类型选择不同查询语句
        if DATABASE_URL.startswith('postgres'):
            # 尝试多种方法
            methods = [
                {
                    "name": "to_char方法",
                    "query": """
                        SELECT COUNT(*) FROM orders 
                        WHERE status = 'completed' 
                        AND to_char(updated_at::timestamp, 'YYYY-MM-DD') = %s
                    """,
                    "params": (today,)
                },
                {
                    "name": "substring方法",
                    "query": """
                        SELECT COUNT(*) FROM orders 
                        WHERE status = 'completed' 
                        AND substring(updated_at, 1, 10) = %s
                    """,
                    "params": (today,)
                },
                {
                    "name": "LIKE方法",
                    "query": """
                        SELECT COUNT(*) FROM orders 
                        WHERE status = 'completed' 
                        AND updated_at LIKE %s
                    """,
                    "params": (f"{today}%",)
                }
            ]
            
            # 尝试所有方法
            for method in methods:
                try:
                    result = execute_query(method["query"], method["params"], fetch=True)
                    count = result[0][0] if result and result[0] else 0
                    logger.info(f"使用{method['name']}查询结果: {count}")
                    
                    # 如果找到了结果，就返回
                    if count > 0:
                        logger.info(f"今日全站充值成功订单数: {count}, 查询方法: {method['name']}")
                        return count
                except Exception as e:
                    logger.error(f"使用{method['name']}查询失败: {str(e)}")
            
            # 如果所有方法都没有找到结果，返回0
            logger.warning("所有查询方法都返回0，可能是日期格式问题")
            return 0
        else:
            # SQLite使用LIKE方法
            query = "SELECT COUNT(*) FROM orders WHERE status = 'completed' AND updated_at LIKE ?"
            params = (f"{today}%",)
            
            result = execute_query(query, params, fetch=True)
            count = result[0][0] if result and result[0] else 0
            
            # 如果没有找到结果，尝试其他方法
            if count == 0:
                try:
                    # 尝试使用substr
                    substr_query = "SELECT COUNT(*) FROM orders WHERE status = 'completed' AND substr(updated_at, 1, 10) = ?"
                    substr_result = execute_query(substr_query, (today,), fetch=True)
                    substr_count = substr_result[0][0] if substr_result and substr_result[0] else 0
                    
                    if substr_count > 0:
                        logger.info(f"今日全站充值成功订单数(使用substr): {substr_count}")
                        return substr_count
                except Exception as e:
                    logger.error(f"使用substr查询失败: {str(e)}")
            
            logger.info(f"今日全站充值成功订单数: {count}, 查询参数: {today}")
            return count
    except Exception as e:
        logger.error(f"获取全站今日确认订单数失败: {str(e)}", exc_info=True)
        return 0

def get_seller_today_confirmed_orders_by_user(telegram_id):
    """获取卖家今天已确认的订单数，并按用户分组"""
    from datetime import datetime
    import pytz
    today = datetime.now(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d")
    
    try:
        if DATABASE_URL.startswith('postgres'):
            results = execute_query(
                """
                SELECT web_user_id, COUNT(*) 
                FROM orders 
                WHERE accepted_by = %s 
                AND status = 'completed' 
                AND to_char(updated_at::timestamp, 'YYYY-MM-DD') = %s
                GROUP BY web_user_id
                """,
                (str(telegram_id), today),
                fetch=True
            )
        else:
            results = execute_query(
                """
                SELECT web_user_id, COUNT(*) 
                FROM orders 
                WHERE accepted_by = ? 
                AND status = 'completed' 
                AND updated_at LIKE ?
                GROUP BY web_user_id
                """,
                (str(telegram_id), f"{today}%"),
                fetch=True
            )
        
        logger.info(f"卖家 {telegram_id} 今日充值成功订单数: {len(results) if results else 0}")
        return results if results else []
    except Exception as e:
        logger.error(f"获取卖家今日确认订单数失败: {str(e)}", exc_info=True)
        return []

def get_seller_pending_orders(telegram_id):
    """获取卖家当前未完成的订单数（已接单但未确认）"""
    if DATABASE_URL.startswith('postgres'):
        result = execute_query(
            """
            SELECT COUNT(*) FROM orders 
            WHERE accepted_by = ? 
            AND status != '已取消' 
            AND (buyer_confirmed IS NULL OR buyer_confirmed = FALSE)
            """,
            (telegram_id,),
            fetch=True
        )
    else:
        result = execute_query(
            """
            SELECT COUNT(*) FROM orders 
            WHERE accepted_by = ? 
            AND status != '已取消' 
            AND (buyer_confirmed IS NULL OR buyer_confirmed = 0)
            """,
            (telegram_id,),
            fetch=True
        )
    
    if result and len(result) > 0:
        return result[0][0]
    return 0

def check_seller_completed_orders(telegram_id):
    """检查卖家完成的订单数（现在只是记录，不再自动停用）"""
    # 获取已完成订单数
    completed_orders = get_seller_completed_orders(telegram_id)
    logger.info(f"卖家 {telegram_id} 当前已完成订单数: {completed_orders}")
    return completed_orders

def select_active_seller():
    """
    从所有活跃卖家中选择一个卖家接单
    
    选择逻辑：
    1. 获取所有活跃的卖家
    2. 随机选择一个活跃卖家
    
    返回:
    - 卖家ID，如果没有活跃卖家则返回None
    """
    try:
        active_sellers = get_active_sellers()
        
        if not active_sellers:
            logger.warning("没有活跃的卖家可用于选择")
            return None
            
        # 随机选择一个活跃卖家
        import random
        selected_seller = random.choice(active_sellers)
        logger.info(f"随机选择卖家: {selected_seller['id']}")
        return selected_seller["id"]
    
    except Exception as e:
        logger.error(f"选择活跃卖家失败: {str(e)}", exc_info=True)
        return None

def check_seller_activity(telegram_id):
    """向卖家发送活跃度检查请求"""
    # 记录检查请求时间
    timestamp = get_china_time()
    execute_query(
        "UPDATE sellers SET activity_check_at = ? WHERE telegram_id = ?",
        (timestamp, telegram_id)
    )
    return True

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

def get_admin_sellers():
    """获取所有管理员卖家的Telegram ID"""
    if DATABASE_URL.startswith('postgres'):
        admins = execute_query(
            "SELECT telegram_id FROM sellers WHERE is_admin = TRUE",
            fetch=True
        )
    else:
        admins = execute_query(
            "SELECT telegram_id FROM sellers WHERE is_admin = 1",
            fetch=True
        )
    return [admin[0] for admin in admins] if admins else []

def check_db_connection():
    """检查并确认数据库连接正常"""
    try:
        # 使用execute_query函数测试数据库连接
        execute_query("SELECT 1", fetch=True)
        logger.info("数据库连接成功。")
        return True
    except Exception as e:
        logger.error(f"数据库连接失败: {e}", exc_info=True)
        # 根据需要，这里可以决定是否退出程序
        # exit(1)
        return False

# ===== 余额系统相关函数 =====
def get_user_balance(user_id):
    """获取用户余额"""
    return 0

def get_user_credit_limit(user_id):
    """获取用户透支额度"""
    return 0

def refund_order(order_id):
    """退款功能已移除，此函数仅为兼容性保留"""
    # 标记订单为已退款
    try:
        execute_query("UPDATE orders SET refunded = 1 WHERE id = ?", (order_id,))
        return True, 0
    except Exception as e:
        logger.error(f"标记订单已退款失败: {str(e)}", exc_info=True)
        return False, str(e)

def create_order_with_deduction_atomic(account, password, package, remark, username, user_id):
    """创建订单（已移除余额扣除功能）"""
    try:
        # 创建订单记录
        now = get_china_time()
        
        # 根据数据库类型选择不同的SQL
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL版本 - 不使用username字段
            execute_query(
                """
                INSERT INTO orders (account, password, package, status, created_at, remark, user_id, web_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (account, password, package, 'submitted', now, remark, user_id, username)
            )
        else:
            # SQLite版本
            execute_query(
                """
                INSERT INTO orders (account, password, package, status, created_at, remark, user_id, username)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (account, password, package, 'submitted', now, remark, user_id, username)
            )
            
        return True, "订单创建成功", 0, 0
    except Exception as e:
        logger.error(f"创建订单失败: {str(e)}", exc_info=True)
        return False, f"创建订单失败: {str(e)}", None, None

def check_duplicate_remark(user_id, remark):
    """
    检查当前用户今日订单中是否存在重复的备注
    
    参数:
    - user_id: 用户ID
    - remark: 要检查的备注
    
    返回:
    - 如果存在重复，返回True，否则返回False
    """
    if not remark or remark.strip() == '':
        # 空备注不检查重复
        return False
        
    try:
        # 获取今天的日期，格式为YYYY-MM-DD
        today = datetime.now(CN_TIMEZONE).strftime("%Y-%m-%d")
        
        # 根据数据库类型选择不同查询语句
        if DATABASE_URL.startswith('postgres'):
            query = """
                SELECT COUNT(*) FROM orders 
                WHERE user_id = %s 
                AND remark = %s 
                AND created_at LIKE %s
            """
            params = (user_id, remark, f"{today}%")
        else:
            query = """
                SELECT COUNT(*) FROM orders 
                WHERE user_id = ? 
                AND remark = ? 
                AND created_at LIKE ?
            """
            params = (user_id, remark, f"{today}%")
            
        result = execute_query(query, params, fetch=True)
        count = result[0][0] if result and result[0] else 0
        
        return count > 0
    except Exception as e:
        logger.error(f"检查备注重复失败: {str(e)}", exc_info=True)
        return False

def get_seller_pending_orders_count(telegram_id):
    """
    获取卖家当前正在处理的订单数量（已接单但未完成的订单）
    
    参数:
    - telegram_id: 卖家的Telegram ID
    
    返回:
    - 正在处理的订单数量
    """
    try:
        if DATABASE_URL.startswith('postgres'):
            result = execute_query("""
                SELECT COUNT(*) 
                FROM orders 
                WHERE accepted_by = %s AND status = %s
            """, (telegram_id, STATUS['ACCEPTED']), fetch=True)
        else:
            result = execute_query("""
                SELECT COUNT(*) 
                FROM orders 
                WHERE accepted_by = ? AND status = ?
            """, (telegram_id, STATUS['ACCEPTED']), fetch=True)
        
        return result[0][0] if result else 0
    except Exception as e:
        logger.error(f"获取卖家 {telegram_id} 待处理订单数失败: {str(e)}", exc_info=True)
        return 0

def check_seller_at_max_capacity(telegram_id):
    """
    检查卖家是否已达到最大接单数
    
    参数:
    - telegram_id: 卖家的Telegram ID
    
    返回:
    - True: 卖家已达到最大接单数
    - False: 卖家未达到最大接单数
    """
    try:
        # 获取卖家的最大接单数
        if DATABASE_URL.startswith('postgres'):
            max_orders_result = execute_query("""
                SELECT max_orders FROM sellers WHERE telegram_id = %s
            """, (telegram_id,), fetch=True)
        else:
            max_orders_result = execute_query("""
                SELECT max_orders FROM sellers WHERE telegram_id = ?
            """, (telegram_id,), fetch=True)
        
        if not max_orders_result:
            return True  # 卖家不存在，视为已满
        
        max_orders = max_orders_result[0][0] or 3  # 默认为3
        
        # 获取当前待处理订单数
        pending_count = get_seller_pending_orders_count(telegram_id)
        
        # 检查是否达到最大值
        return pending_count >= max_orders
    except Exception as e:
        logger.error(f"检查卖家 {telegram_id} 接单容量失败: {str(e)}", exc_info=True)
        return True  # 出错时保守返回已满

def check_all_sellers_at_max_capacity():
    """
    检查是否所有活跃卖家都已达到最大接单数
    
    返回:
    - True: 所有活跃卖家都已达到最大接单数
    - False: 至少有一个活跃卖家未达到最大接单数
    """
    try:
        # 获取所有活跃卖家
        active_sellers = get_active_seller_ids()
        
        if not active_sellers:
            return True  # 没有活跃卖家，视为所有卖家都已满
        
        # 检查每个卖家是否已达到最大接单数
        for seller_id in active_sellers:
            if not check_seller_at_max_capacity(seller_id):
                return False  # 找到一个未达到最大接单数的卖家
        
        # 所有卖家都已达到最大接单数
        return True
    except Exception as e:
        logger.error(f"检查所有卖家接单容量失败: {str(e)}", exc_info=True)
        return False  # 出错时保守返回未满