import os
import time
import sqlite3
import hashlib
import psycopg2
from functools import wraps
from datetime import datetime
from urllib.parse import urlparse

from modules.constants import DATABASE_URL

# ===== 数据库 =====
def init_db():
    """根据环境配置初始化数据库"""
    if DATABASE_URL.startswith('postgres'):
        init_postgres_db()
    else:
        init_sqlite_db()
        
def init_sqlite_db():
    """初始化SQLite数据库"""
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
        c.execute("ALTER TABLE orders ADD COLUMN user_id INTEGER")
    
    # 创建超级管理员账号（如果不存在）
    admin_hash = hashlib.sha256("755439".encode()).hexdigest()
    c.execute("SELECT id FROM users WHERE username = ?", ("755439",))
    if not c.fetchone():
        c.execute("""
            INSERT INTO users (username, password_hash, is_admin, created_at) 
            VALUES (?, ?, 1, ?)
        """, ("755439", admin_hash, time.strftime("%Y-%m-%d %H:%M:%S")))
    
    conn.commit()
    conn.close()

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
    if DATABASE_URL.startswith('postgres'):
        return execute_postgres_query(query, params, fetch)
    else:
        return execute_sqlite_query(query, params, fetch)

def execute_sqlite_query(query, params=(), fetch=False):
    """执行SQLite查询并返回结果"""
    conn = sqlite3.connect("orders.db")
    cursor = conn.cursor()
    cursor.execute(query, params)
    
    result = None
    if fetch:
        result = cursor.fetchall()
    
    conn.commit()
    conn.close()
    return result

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
        
        # 检查订单状态
        cursor.execute("SELECT status FROM orders WHERE id = %s", (oid,))
        order = cursor.fetchone()
        if not order or order[0] != 'submitted':
            conn.close()
            return False
        
        # 更新订单
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("UPDATE orders SET status = 'accepted', accepted_at = %s, accepted_by = %s WHERE id = %s",
                      (timestamp, str(user_id), oid))
    else:
        # SQLite版本
        conn = sqlite3.connect("orders.db")
        cursor = conn.cursor()
        
        # 检查订单状态
        cursor.execute("SELECT status FROM orders WHERE id = ?", (oid,))
        order = cursor.fetchone()
        if not order or order[0] != 'submitted':
            conn.close()
            return False
        
        # 更新订单
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("UPDATE orders SET status = 'accepted', accepted_at = ?, accepted_by = ? WHERE id = ?",
                      (timestamp, str(user_id), oid))
    
    # 提交并关闭
    conn.commit()
    conn.close()
    return True

# 获取订单详情
def get_order_details(oid):
    return execute_query("SELECT id, account, password, package, status, remark FROM orders WHERE id = ?", (oid,), fetch=True) 