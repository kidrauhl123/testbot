import os
import time
import sqlite3
import hashlib
import logging
import psycopg2
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

def init_sqlite_db():
    """初始化SQLite数据库"""
    logger.info("使用SQLite数据库")
    
    try:
        # 连接数据库（如果不存在会自动创建）
        conn = sqlite3.connect('orders.db')
        cursor = conn.cursor()
        
        # 创建用户表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin BOOLEAN DEFAULT 0,
            balance REAL DEFAULT 0,
            credit_limit REAL DEFAULT 0,
            created_at TEXT,
            updated_at TEXT,
            last_login TEXT
        )
        ''')
        
        # 创建订单表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            email TEXT,
            package TEXT NOT NULL,
            status TEXT NOT NULL,
            remark TEXT,
            creator_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            accepted_at TEXT,
            completed_at TEXT,
            accepted_by TEXT,
            accepted_by_id INTEGER
        )
        ''')
        
        # 创建卖家表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS sellers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            nickname TEXT,
            is_active BOOLEAN DEFAULT 1,
            last_active_at TEXT,
            desired_orders INTEGER DEFAULT 0,
            added_at TEXT,
            added_by TEXT
        )
        ''')
        
        # 创建订单通知表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS order_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            telegram_message_id TEXT,
            notified_at TEXT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        )
        ''')
        
        # 创建用户定制价格表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_custom_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            package TEXT NOT NULL,
            price REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id),
            UNIQUE(user_id, package)
        )
        ''')
        
        # 创建管理员账号（如果不存在）
        cursor.execute('SELECT id FROM users WHERE username=?', (ADMIN_USERNAME,))
        admin = cursor.fetchone()
        
        if not admin:
            admin_pass_hash = hash_password(ADMIN_PASSWORD)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            cursor.execute('''
            INSERT INTO users (username, password_hash, is_admin, created_at)
            VALUES (?, ?, 1, ?)
            ''', (ADMIN_USERNAME, admin_pass_hash, now))
            
            logger.info(f"已创建管理员账号: {ADMIN_USERNAME}")
        
        # 提交更改
        conn.commit()
        conn.close()
        
        logger.info("SQLite数据库初始化成功")
        
    except Exception as e:
        logger.error(f"初始化SQLite数据库时出错: {str(e)}", exc_info=True)

def init_postgres_db():
    """初始化PostgreSQL数据库"""
    logger.info("使用PostgreSQL数据库")
    
    try:
        # 解析连接URL
        url = urlparse(DATABASE_URL)
        dbname = url.path[1:]
        user = url.username
        password = url.password
        host = url.hostname
        port = url.port
        
        # 连接数据库
        conn = psycopg2.connect(
            dbname=dbname,
            user=user,
            password=password,
            host=host,
            port=port
        )
        conn.autocommit = True  # 自动提交
        cursor = conn.cursor()
        
        # 创建用户表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin BOOLEAN DEFAULT FALSE,
            balance REAL DEFAULT 0,
            credit_limit REAL DEFAULT 0,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            last_login TIMESTAMP
        )
        ''')
        
        # 创建订单表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            account TEXT NOT NULL,
            email TEXT,
            package TEXT NOT NULL,
            status TEXT NOT NULL,
            remark TEXT,
            creator_id INTEGER,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP,
            accepted_at TIMESTAMP,
            completed_at TIMESTAMP,
            accepted_by TEXT,
            accepted_by_id INTEGER
        )
        ''')
        
        # 创建卖家表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS sellers (
            id SERIAL PRIMARY KEY,
            telegram_id TEXT UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            nickname TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            last_active_at TIMESTAMP,
            desired_orders INTEGER DEFAULT 0,
            added_at TIMESTAMP,
            added_by TEXT
        )
        ''')
        
        # 创建订单通知表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS order_notifications (
            id SERIAL PRIMARY KEY,
            order_id INTEGER NOT NULL,
            telegram_message_id TEXT,
            notified_at TIMESTAMP NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders (id)
        )
        ''')
        
        # 创建用户定制价格表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_custom_prices (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            package TEXT NOT NULL,
            price REAL NOT NULL,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            UNIQUE(user_id, package)
        )
        ''')
        
        # 创建管理员账号（如果不存在）
        cursor.execute('SELECT id FROM users WHERE username=%s', (ADMIN_USERNAME,))
        admin = cursor.fetchone()
        
        if not admin:
            admin_pass_hash = hash_password(ADMIN_PASSWORD)
            now = datetime.now()
            
            cursor.execute('''
            INSERT INTO users (username, password_hash, is_admin, created_at)
            VALUES (%s, %s, TRUE, %s)
            ''', (ADMIN_USERNAME, admin_pass_hash, now))
            
            logger.info(f"已创建管理员账号: {ADMIN_USERNAME}")
        
        # 关闭连接
        conn.close()
        
        logger.info("PostgreSQL数据库初始化成功")
        
    except Exception as e:
        logger.error(f"初始化PostgreSQL数据库时出错: {str(e)}", exc_info=True)

# ===== 数据库操作 =====
def execute_query(query, params=None, fetch=False):
    """执行SQL查询，自动处理SQLite和PostgreSQL的差异"""
    try:
        is_postgres = DATABASE_URL.startswith('postgres')
        
        if is_postgres:
            # PostgreSQL连接
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
            
            # 不同的参数占位符
            if params and '?' in query:
                query = query.replace('?', '%s')
        else:
            # SQLite连接
            conn = sqlite3.connect('orders.db')
        
        cursor = conn.cursor()
        
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        
        result = None
        if fetch:
            result = cursor.fetchall()
        
        conn.commit()
        conn.close()
        
        return result
    
    except Exception as e:
        logger.error(f"执行查询时出错: {str(e)}\n查询: {query}\n参数: {params}", exc_info=True)
        return None

# ===== 用户认证 =====
def hash_password(password):
    """对密码进行哈希处理"""
    # 使用SHA-256哈希算法
    return hashlib.sha256(password.encode()).hexdigest()

# ===== 余额操作 =====
def get_user_balance(user_id):
    """获取用户余额"""
    if not user_id:
        return 0
        
    result = execute_query("SELECT balance FROM users WHERE id=?", (user_id,), fetch=True)
    
    if result and len(result) > 0:
        return result[0][0]
    return 0

def get_user_credit_limit(user_id):
    """获取用户信用额度"""
    if not user_id:
        return 0
        
    result = execute_query("SELECT credit_limit FROM users WHERE id=?", (user_id,), fetch=True)
    
    if result and len(result) > 0:
        return result[0][0]
    return 0

def adjust_user_balance(user_id, amount, description=None):
    """调整用户余额"""
    if not user_id:
        return False, "无效的用户ID"
        
    # 获取当前余额
    current_balance = get_user_balance(user_id)
    
    # 计算新余额
    new_balance = current_balance + amount
    
    # 更新余额
    execute_query(
        "UPDATE users SET balance=?, updated_at=? WHERE id=?",
        (new_balance, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id)
    )
    
    return True, new_balance

# ===== 订单操作 =====
def create_order_with_deduction_atomic(user_id, account, package, remark=None):
    """创建订单并扣款（原子操作）"""
    try:
        # 获取用户信息
        user_data = execute_query("SELECT balance, credit_limit FROM users WHERE id=?", (user_id,), fetch=True)
        if not user_data:
            return {"success": False, "error": "找不到用户信息"}
            
        current_balance = user_data[0][0] or 0
        
        # 检查余额是否足够
        if current_balance < 0:
            return {"success": False, "error": f"账户余额不足: {current_balance} USDT"}
            
        # 创建订单
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        order_id = execute_query(
            """
            INSERT INTO orders (account, package, status, remark, creator_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (account, package, STATUS["SUBMITTED"], remark, user_id, timestamp, timestamp),
            fetch=True
        )
        
        if not order_id:
            return {"success": False, "error": "创建订单失败"}
            
        order_id = order_id[0][0]
        
        return {
            "success": True, 
            "order_id": order_id,
            "new_balance": current_balance
        }
    
    except Exception as e:
        logger.error(f"创建订单和扣款时出错: {str(e)}", exc_info=True)
        return {"success": False, "error": f"系统错误: {str(e)}"}

# 订单详情查询
def get_order_details(order_id):
    """获取订单详情"""
    if not order_id:
        return None
        
    result = execute_query(
        """
        SELECT o.id, o.account, o.package, o.status, o.remark, 
               o.created_at, o.updated_at, o.accepted_at, o.completed_at,
               o.accepted_by, u.username as creator
        FROM orders o
        LEFT JOIN users u ON o.creator_id = u.id
        WHERE o.id=?
        """,
        (order_id,),
        fetch=True
    )
    
    if result and len(result) > 0:
        return result[0]
    return None

# 订单接单
def accept_order(order_id, accepted_by, accepted_by_id=None):
    """接单处理"""
    if not order_id or not accepted_by:
        return False, "参数错误"
        
    # 检查订单状态
    order = execute_query("SELECT status FROM orders WHERE id=?", (order_id,), fetch=True)
    
    if not order:
        return False, "订单不存在"
        
    if order[0][0] != STATUS["SUBMITTED"]:
        return False, f"订单状态不正确，当前状态: {order[0][0]}"
        
    # 更新订单状态
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query(
        """
        UPDATE orders 
        SET status=?, accepted_by=?, accepted_by_id=?, accepted_at=?, updated_at=?
        WHERE id=?
        """,
        (STATUS["ACCEPTED"], accepted_by, accepted_by_id, timestamp, timestamp, order_id)
    )
    
    return True, "接单成功"

# 订单完成
def complete_order(order_id):
    """完成订单"""
    if not order_id:
        return False, "参数错误"
        
    # 检查订单状态
    order = execute_query("SELECT status, accepted_by FROM orders WHERE id=?", (order_id,), fetch=True)
    
    if not order:
        return False, "订单不存在"
        
    if order[0][0] not in [STATUS["ACCEPTED"], STATUS["SUBMITTED"]]:
        return False, f"订单状态不正确，当前状态: {order[0][0]}"
        
    # 更新订单状态
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query(
        """
        UPDATE orders 
        SET status=?, completed_at=?, updated_at=?
        WHERE id=?
        """,
        (STATUS["COMPLETED"], timestamp, timestamp, order_id)
    )
    
    return True, "订单已完成"

# 订单失败
def fail_order(order_id, reason=None):
    """订单失败处理"""
    if not order_id:
        return False, "参数错误"
        
    # 检查订单状态
    order = execute_query("SELECT status FROM orders WHERE id=?", (order_id,), fetch=True)
    
    if not order:
        return False, "订单不存在"
        
    if order[0][0] not in [STATUS["ACCEPTED"], STATUS["SUBMITTED"]]:
        return False, f"订单状态不正确，当前状态: {order[0][0]}"
        
    # 更新订单状态
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    execute_query(
        """
        UPDATE orders 
        SET status=?, remark=?, updated_at=?
        WHERE id=?
        """,
        (STATUS["FAILED"], reason, timestamp, order_id)
    )
    
    return True, "订单已标记为失败" 