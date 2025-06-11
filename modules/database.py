import os
import time
import sqlite3
import hashlib
import logging
import psycopg2
from functools import wraps
from datetime import datetime
from urllib.parse import urlparse

from modules.constants import DATABASE_URL

# 设置日志
logger = logging.getLogger(__name__)

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
    conn = sqlite3.connect("orders.db")
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
            last_login TEXT
        )
    """)
    
    # 检查是否需要添加新列
    c.execute("PRAGMA table_info(orders)")
    columns = [column[1] for column in c.fetchall()]
    if 'user_id' not in columns:
        logger.info("为orders表添加user_id列")
        c.execute("ALTER TABLE orders ADD COLUMN user_id INTEGER")
    
    # 检查是否需要添加accepted_by_username列（Telegram用户名）
    if 'accepted_by_username' not in columns:
        logger.info("为orders表添加accepted_by_username列")
        c.execute("ALTER TABLE orders ADD COLUMN accepted_by_username TEXT")
    
    # 检查是否需要添加accepted_by_first_name列（Telegram昵称）
    if 'accepted_by_first_name' not in columns:
        logger.info("为orders表添加accepted_by_first_name列")
        c.execute("ALTER TABLE orders ADD COLUMN accepted_by_first_name TEXT")
    
    # 创建超级管理员账号（如果不存在）
    admin_hash = hashlib.sha256("755439".encode()).hexdigest()
    c.execute("SELECT id FROM users WHERE username = ?", ("755439",))
    if not c.fetchone():
        logger.info("创建默认管理员账号")
        c.execute("""
            INSERT INTO users (username, password_hash, is_admin, created_at) 
            VALUES (?, ?, 1, ?)
        """, ("755439", admin_hash, time.strftime("%Y-%m-%d %H:%M:%S")))
    
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
            user_id INTEGER
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
            last_login TEXT
        )
    """)
    
    # 检查是否需要添加新列
    try:
        c.execute("SELECT user_id FROM orders LIMIT 1")
    except psycopg2.errors.UndefinedColumn:
        c.execute("ALTER TABLE orders ADD COLUMN user_id INTEGER")
    
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
    admin_hash = hashlib.sha256("755439".encode()).hexdigest()
    c.execute("SELECT id FROM users WHERE username = %s", ("755439",))
    if not c.fetchone():
        c.execute("""
            INSERT INTO users (username, password_hash, is_admin, created_at) 
            VALUES (%s, %s, 1, %s)
        """, ("755439", admin_hash, time.strftime("%Y-%m-%d %H:%M:%S")))
    
    conn.close()

# 数据库执行函数
def execute_query(query, params=(), fetch=False):
    """执行数据库查询并返回结果"""
    logger.debug(f"执行查询: {query[:50]}... 参数: {params}")
    if DATABASE_URL.startswith('postgres'):
        return execute_postgres_query(query, params, fetch)
    else:
        return execute_sqlite_query(query, params, fetch)

def execute_sqlite_query(query, params=(), fetch=False):
    """执行SQLite查询并返回结果"""
    try:
        conn = sqlite3.connect("orders.db")
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

def execute_postgres_query(query, params=(), fetch=False):
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
    return execute_query("SELECT id, account, password, package FROM orders WHERE notified = 0 AND status = 'submitted'", fetch=True)

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
                if user_id in user_info_cache:
                    username = user_info_cache[user_id].get('username')
                    first_name = user_info_cache[user_id].get('first_name')
                
                # 更新订单
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute("UPDATE orders SET status = 'accepted', accepted_at = %s, accepted_by = %s, accepted_by_username = %s, accepted_by_first_name = %s WHERE id = %s",
                            (timestamp, str(user_id), username, first_name, oid))
            except Exception as e:
                logger.error(f"获取用户信息失败: {str(e)}")
                # 如果获取用户信息失败，仍然更新订单，但不设置用户名和昵称
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        conn = sqlite3.connect("orders.db")
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
                if user_id in user_info_cache:
                    username = user_info_cache[user_id].get('username')
                    first_name = user_info_cache[user_id].get('first_name')
                
                # 更新订单
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute("UPDATE orders SET status = 'accepted', accepted_at = ?, accepted_by = ?, accepted_by_username = ?, accepted_by_first_name = ? WHERE id = ?",
                            (timestamp, str(user_id), username, first_name, oid))
            except Exception as e:
                logger.error(f"获取用户信息失败: {str(e)}")
                # 如果获取用户信息失败，仍然更新订单，但不设置用户名和昵称
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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