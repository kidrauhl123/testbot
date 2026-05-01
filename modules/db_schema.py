import hashlib
import logging

import psycopg2

from modules.constants import ADMIN_PASSWORD, ADMIN_USERNAME
from modules.db_core import ensure_postgres_configured, execute_query, get_postgres_connection
from modules.order_balance import get_china_time
from modules.recharge import create_recharge_tables
from modules.activation_codes import create_activation_code_table

logger = logging.getLogger(__name__)


# ===== 数据库 schema / 初始化 =====
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
