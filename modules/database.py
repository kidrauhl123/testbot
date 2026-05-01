import time
import hashlib
import logging
import psycopg2
from functools import wraps
from datetime import datetime
import pytz

from modules.constants import STATUS, ADMIN_USERNAME, ADMIN_PASSWORD
from modules.db_core import (
    ensure_postgres_configured,
    execute_postgres_query,
    execute_query,
    get_postgres_connection,
)
from modules.order_balance import (
    accept_order_atomic,
    add_balance_record,
    check_balance_for_package,
    create_order_with_deduction_atomic,
    get_balance_records,
    get_china_time,
    get_order_details,
    get_unnotified_orders,
    get_user_balance,
    get_user_credit_limit,
    refund_order,
    set_user_balance,
    set_user_credit_limit,
    update_user_balance,
)
from modules.recharge import (
    approve_recharge_request,
    create_recharge_request,
    create_recharge_tables,
    get_pending_recharge_requests,
    get_user_recharge_requests,
    reject_recharge_request,
)
from modules.activation_codes import (
    create_activation_code,
    create_activation_code_table,
    generate_activation_code,
    get_activation_code,
    get_admin_activation_codes,
    mark_activation_code_used,
)
from modules.custom_prices import (
    delete_user_custom_price,
    get_user_custom_prices,
    set_user_custom_price,
)

# 设置日志
logger = logging.getLogger(__name__)

# 中国时区
CN_TIMEZONE = pytz.timezone('Asia/Shanghai')



# ===== 数据库 =====
def init_db():
    """初始化 PostgreSQL 数据库。"""
    ensure_postgres_configured()
    logger.info("初始化 PostgreSQL 数据库...")
    init_postgres_db()
    
    # 创建充值记录表和余额记录表
    logger.info("正在创建充值记录表和余额记录表...")
    create_recharge_tables()
    logger.info("充值记录表和余额记录表创建完成")
    
    # 创建激活码表
    logger.info("正在创建激活码表...")
    create_activation_code_table()
    logger.info("激活码表创建完成")

    logger.info("正在创建/确认数据库索引...")
    create_performance_indexes()
    logger.info("数据库索引检查完成")


def init_postgres_db():
    """初始化PostgreSQL数据库"""
    conn = get_postgres_connection()
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

def create_performance_indexes():
    """创建常用查询索引；只做 IF NOT EXISTS，重复启动安全。"""
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status)",
        "CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders (user_id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_web_user_id ON orders (web_user_id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_accepted_by ON orders (accepted_by)",
        "CREATE INDEX IF NOT EXISTS idx_orders_notified_status ON orders (notified, status)",
        "CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders (created_at)",
        "CREATE INDEX IF NOT EXISTS idx_orders_status_created_at ON orders (status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_balance_records_user_created ON balance_records (user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_balance_records_created_at ON balance_records (created_at)",
        "CREATE INDEX IF NOT EXISTS idx_recharge_requests_user_status ON recharge_requests (user_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_recharge_requests_status_created ON recharge_requests (status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_activation_codes_is_used ON activation_codes (is_used)",
    ]
    for query in indexes:
        execute_query(query)


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
