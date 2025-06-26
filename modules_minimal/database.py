import os
import sqlite3
import logging
import hashlib
from datetime import datetime
import pytz

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

# 数据库路径
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders_minimal.db")

# 初始化数据库
def init_db():
    """初始化SQLite数据库"""
    logger.info(f"初始化数据库: {DB_PATH}")
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 创建订单表 - 简化版，只包含必要字段
    c.execute('''
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account TEXT,
        status TEXT DEFAULT 'submitted',
        created_at TEXT,
        accepted_by TEXT,
        accepted_at TEXT,
        completed_at TEXT,
        notified INTEGER DEFAULT 0
    )
    ''')
    
    # 创建卖家表 - 简化版，只包含必要字段
    c.execute('''
    CREATE TABLE IF NOT EXISTS sellers (
        telegram_id TEXT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        nickname TEXT,
        is_active INTEGER DEFAULT 1
    )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("SQLite数据库初始化完成")

# 执行查询
def execute_query(query, params=(), fetch=False, return_cursor=False):
    """执行SQLite查询"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row if fetch else None
    cursor = conn.cursor()
    
    try:
        cursor.execute(query, params)
        conn.commit()
        
        if fetch:
            result = cursor.fetchall()
            if not return_cursor:
                cursor.close()
                conn.close()
            return result
        elif return_cursor:
            return cursor
        else:
            cursor.close()
            conn.close()
            return True
    except Exception as e:
        logger.error(f"执行查询出错: {str(e)}", exc_info=True)
        conn.rollback()
        if not return_cursor:
            cursor.close()
            conn.close()
        return False if not fetch else []

# 获取未通知的订单
def get_unnotified_orders():
    """获取未通知的订单"""
    return execute_query(
        "SELECT id, account, created_at FROM orders WHERE notified = 0 AND status = 'submitted'",
        fetch=True
    )

# 获取所有卖家
def get_all_sellers():
    """获取所有卖家"""
    return execute_query("SELECT telegram_id, username, first_name, nickname, is_active FROM sellers", fetch=True)

# 获取活跃卖家IDs
def get_active_seller_ids():
    """获取活跃卖家的Telegram ID列表"""
    sellers = execute_query("SELECT telegram_id FROM sellers WHERE is_active = 1", fetch=True)
    return [str(seller[0]) for seller in sellers]

# 添加卖家
def add_seller(telegram_id, username, first_name, nickname):
    """添加卖家"""
    execute_query(
        "INSERT OR REPLACE INTO sellers (telegram_id, username, first_name, nickname, is_active) VALUES (?, ?, ?, ?, 1)",
        (str(telegram_id), username, first_name, nickname)
    )

# 切换卖家状态
def toggle_seller_status(telegram_id):
    """切换卖家的活跃状态"""
    execute_query(
        "UPDATE sellers SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE telegram_id = ?",
        (str(telegram_id),)
    ) 