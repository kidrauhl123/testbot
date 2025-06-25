import os
import time
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

# ===== 数据库初始化 =====
def init_db():
    """初始化PostgreSQL数据库"""
    try:
        url = urlparse(DATABASE_URL)
        dbname = url.path[1:]
        user = url.username
        password = url.password
        host = url.hostname
        port = url.port
        
        logger.info(f"连接到PostgreSQL数据库: {host}:{port}/{dbname}")
        
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
                customer_name TEXT,
                package TEXT NOT NULL,
                qr_image TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL,
                paid_at TEXT,
                confirmed_at TEXT,
                seller_id TEXT,
                seller_username TEXT,
                seller_first_name TEXT,
                notified INTEGER DEFAULT 0
            )
        """)
        
        # 卖家表
        c.execute("""
            CREATE TABLE IF NOT EXISTS sellers (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                is_admin BOOLEAN DEFAULT FALSE,
                added_at TEXT NOT NULL,
                added_by TEXT
            )
        """)
        
        # 创建超级管理员账号，使用一个特定的数字ID
        admin_id = 999999999  # 一个特定的ID号，保留给管理员
        admin_hash = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()
        c.execute("""
            INSERT INTO sellers (telegram_id, username, first_name, is_active, is_admin, added_at, added_by)
            VALUES (%s, %s, %s, TRUE, TRUE, %s, 'system')
            ON CONFLICT (telegram_id) DO NOTHING
        """, (admin_id, ADMIN_USERNAME, 'SuperAdmin', get_china_time()))
        
        conn.close()
        logger.info("数据库初始化完成")
    except Exception as e:
        logger.error(f"初始化数据库失败: {str(e)}", exc_info=True)
        raise

# ===== 数据库操作函数 =====
def execute_query(query, params=(), fetch=False):
    """执行PostgreSQL查询"""
    conn = None
    cursor = None
    try:
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
        
        # 替换SQL查询中的问号为PostgreSQL参数标记
        query = query.replace('?', '%s')
        
        cursor.execute(query, params)
        
        if fetch:
            result = cursor.fetchall()
            return result
        else:
            conn.commit()
            return None
    except Exception as e:
        logger.error(f"执行查询失败: {str(e)}, 查询: {query}, 参数: {params}", exc_info=True)
        if conn:
            conn.rollback()
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

def hash_password(password):
    """对密码进行哈希处理"""
    return hashlib.sha256(password.encode()).hexdigest()

# ===== 订单操作函数 =====
def create_order(customer_name, package, qr_image):
    """创建新订单"""
    try:
        created_at = get_china_time()
        order_id = execute_query(
            """
            INSERT INTO orders (customer_name, package, qr_image, status, created_at, notified)
            VALUES (%s, %s, %s, %s, %s, 0)
            RETURNING id
            """,
            (customer_name, package, qr_image, STATUS['SUBMITTED'], created_at),
            fetch=True
        )
        
        return order_id[0][0] if order_id else None
    except Exception as e:
        logger.error(f"创建订单失败: {str(e)}", exc_info=True)
        return None

def get_order_details(order_id):
    """获取订单详情"""
    try:
        order = execute_query(
            """
            SELECT id, customer_name, package, qr_image, status, message, 
                   created_at, paid_at, confirmed_at, 
                   seller_id, seller_username, seller_first_name
            FROM orders
            WHERE id = %s
            """,
            (order_id,),
            fetch=True
        )
        
        if not order or len(order) == 0:
            return None
            
        order_data = {
            'id': order[0][0],
            'customer_name': order[0][1],
            'package': order[0][2],
            'qr_image': order[0][3],
            'status': order[0][4],
            'message': order[0][5],
            'created_at': order[0][6],
            'paid_at': order[0][7],
            'confirmed_at': order[0][8],
            'seller_id': order[0][9],
            'seller_username': order[0][10],
            'seller_first_name': order[0][11]
        }
        
        return order_data
    except Exception as e:
        logger.error(f"获取订单详情失败: {str(e)}", exc_info=True)
        return None

def update_order_status(order_id, status, seller_id=None, seller_username=None, seller_first_name=None, message=None):
    """更新订单状态"""
    try:
        now = get_china_time()
        
        # 根据状态确定要更新的时间字段
        time_field = None
        if status == STATUS['PAID']:
            time_field = 'paid_at'
        elif status == STATUS['CONFIRMED']:
            time_field = 'confirmed_at'
        
        # 构建更新查询
        query_parts = ["UPDATE orders SET status = %s"]
        params = [status]
        
        if time_field:
            query_parts.append(f"{time_field} = %s")
            params.append(now)
        
        if seller_id:
            query_parts.append("seller_id = %s")
            params.append(seller_id)
            
        if seller_username:
            query_parts.append("seller_username = %s")
            params.append(seller_username)
            
        if seller_first_name:
            query_parts.append("seller_first_name = %s")
            params.append(seller_first_name)
            
        if message:
            query_parts.append("message = %s")
            params.append(message)
        
        query_parts.append("WHERE id = %s")
        params.append(order_id)
        
        query = " ".join(query_parts)
        
        execute_query(query, tuple(params))
        return True
    except Exception as e:
        logger.error(f"更新订单状态失败: {str(e)}", exc_info=True)
        return False

def get_unnotified_orders():
    """获取未通知的订单"""
    try:
        orders = execute_query(
            """
            SELECT id, customer_name, package, qr_image, status
            FROM orders
            WHERE notified = 0 AND status = %s
            """,
            (STATUS['SUBMITTED'],),
            fetch=True
        )
        
        result = []
        for order in orders:
            result.append({
                'id': order[0],
                'customer_name': order[1],
                'package': order[2],
                'qr_image': order[3],
                'status': order[4]
            })
        
        return result
    except Exception as e:
        logger.error(f"获取未通知订单失败: {str(e)}", exc_info=True)
        return []

def set_order_notified(order_id):
    """将订单标记为已通知"""
    try:
        execute_query(
            "UPDATE orders SET notified = 1 WHERE id = %s",
            (order_id,)
        )
        return True
    except Exception as e:
        logger.error(f"标记订单已通知失败: {str(e)}", exc_info=True)
        return False

# ===== 卖家操作函数 =====
def get_all_sellers():
    """获取所有卖家信息"""
    try:
        sellers = execute_query(
            """
            SELECT telegram_id, username, first_name, is_active, is_admin, added_at
            FROM sellers
            ORDER BY is_admin DESC, added_at
            """,
            fetch=True
        )
        
        result = []
        for seller in sellers:
            result.append({
                'telegram_id': seller[0],
                'username': seller[1],
                'first_name': seller[2],
                'is_active': seller[3],
                'is_admin': seller[4],
                'added_at': seller[5]
            })
        
        return result
    except Exception as e:
        logger.error(f"获取卖家信息失败: {str(e)}", exc_info=True)
        return []

def get_active_seller_ids():
    """获取所有活跃卖家的ID"""
    try:
        sellers = execute_query(
            "SELECT telegram_id FROM sellers WHERE is_active = TRUE",
            fetch=True
        )
        
        return [seller[0] for seller in sellers] if sellers else []
    except Exception as e:
        logger.error(f"获取活跃卖家ID失败: {str(e)}", exc_info=True)
        return []

def add_seller(telegram_id, username, first_name, added_by):
    """添加新卖家"""
    try:
        execute_query(
            """
            INSERT INTO sellers (telegram_id, username, first_name, is_active, is_admin, added_at, added_by)
            VALUES (%s, %s, %s, TRUE, FALSE, %s, %s)
            ON CONFLICT (telegram_id) DO NOTHING
            """,
            (telegram_id, username, first_name, get_china_time(), added_by)
        )
        return True
    except Exception as e:
        logger.error(f"添加卖家失败: {str(e)}", exc_info=True)
        return False

def toggle_seller_status(telegram_id):
    """切换卖家激活状态"""
    try:
        execute_query(
            "UPDATE sellers SET is_active = CASE WHEN is_active = TRUE THEN FALSE ELSE TRUE END WHERE telegram_id = %s",
            (telegram_id,)
        )
        return True
    except Exception as e:
        logger.error(f"切换卖家状态失败: {str(e)}", exc_info=True)
        return False

def remove_seller(telegram_id):
    """删除卖家"""
    try:
        execute_query(
            "DELETE FROM sellers WHERE telegram_id = %s AND is_admin = 0",
            (telegram_id,)
        )
        return True
    except Exception as e:
        logger.error(f"删除卖家失败: {str(e)}", exc_info=True)
        return False

def toggle_seller_admin(telegram_id):
    """切换卖家管理员状态"""
    try:
        execute_query(
            "UPDATE sellers SET is_admin = CASE WHEN is_admin = TRUE THEN FALSE ELSE TRUE END WHERE telegram_id = %s",
            (telegram_id,)
        )
        return True
    except Exception as e:
        logger.error(f"切换卖家管理员状态失败: {str(e)}", exc_info=True)
        return False

def is_admin_seller(telegram_id):
    """检查卖家是否为管理员"""
    try:
        result = execute_query(
            "SELECT is_admin FROM sellers WHERE telegram_id = %s AND is_active = TRUE",
            (telegram_id,),
            fetch=True
        )
        
        if result and len(result) > 0:
            return result[0][0] == TRUE
        return False
    except Exception as e:
        logger.error(f"检查卖家管理员状态失败: {str(e)}", exc_info=True)
        return False 