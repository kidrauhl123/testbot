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
        
        # 尝试添加可能缺失的列
        try:
            # 使用PL/pgSQL函数检查并添加所有可能缺失的列
            c.execute("""
                DO $$
                BEGIN
                    -- 检查customer_name列
                    IF NOT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='orders' AND column_name='customer_name'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN customer_name TEXT;
                        RAISE NOTICE 'Added customer_name column';
                    END IF;
                    
                    -- 检查account列的约束情况
                    BEGIN
                        -- 尝试将account列的NOT NULL约束删除（如果存在）
                        ALTER TABLE orders ALTER COLUMN account DROP NOT NULL;
                        RAISE NOTICE 'Removed NOT NULL constraint from account column';
                    EXCEPTION
                        WHEN undefined_column THEN
                            -- account列不存在，尝试添加
                            IF NOT EXISTS (
                                SELECT 1 
                                FROM information_schema.columns 
                                WHERE table_name='orders' AND column_name='account'
                            ) THEN
                                ALTER TABLE orders ADD COLUMN account TEXT;
                                RAISE NOTICE 'Added account column';
                            END IF;
                        WHEN OTHERS THEN
                            -- 其他错误，忽略
                            RAISE NOTICE 'Error handling account column: %', SQLERRM;
                    END;
                    
                    -- 检查qr_image列
                    IF NOT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='orders' AND column_name='qr_image'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN qr_image TEXT;
                        RAISE NOTICE 'Added qr_image column';
                    END IF;
                    
                    -- 检查package列
                    IF NOT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='orders' AND column_name='package'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN package TEXT;
                        RAISE NOTICE 'Added package column';
                    END IF;
                    
                    -- 检查status列
                    IF NOT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='orders' AND column_name='status'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN status TEXT;
                        RAISE NOTICE 'Added status column';
                    END IF;
                    
                    -- 检查message列
                    IF NOT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='orders' AND column_name='message'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN message TEXT;
                        RAISE NOTICE 'Added message column';
                    END IF;
                    
                    -- 检查created_at列
                    IF NOT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='orders' AND column_name='created_at'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN created_at TEXT;
                        RAISE NOTICE 'Added created_at column';
                    END IF;
                    
                    -- 检查paid_at列
                    IF NOT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='orders' AND column_name='paid_at'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN paid_at TEXT;
                        RAISE NOTICE 'Added paid_at column';
                    END IF;
                    
                    -- 检查confirmed_at列
                    IF NOT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='orders' AND column_name='confirmed_at'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN confirmed_at TEXT;
                        RAISE NOTICE 'Added confirmed_at column';
                    END IF;
                    
                    -- 检查seller_id列
                    IF NOT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='orders' AND column_name='seller_id'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN seller_id TEXT;
                        RAISE NOTICE 'Added seller_id column';
                    END IF;
                    
                    -- 检查seller_username列
                    IF NOT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='orders' AND column_name='seller_username'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN seller_username TEXT;
                        RAISE NOTICE 'Added seller_username column';
                    END IF;
                    
                    -- 检查seller_first_name列
                    IF NOT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='orders' AND column_name='seller_first_name'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN seller_first_name TEXT;
                        RAISE NOTICE 'Added seller_first_name column';
                    END IF;
                    
                    -- 检查notified列
                    IF NOT EXISTS (
                        SELECT 1 
                        FROM information_schema.columns 
                        WHERE table_name='orders' AND column_name='notified'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN notified INTEGER DEFAULT 0;
                        RAISE NOTICE 'Added notified column';
                    END IF;
                END
                $$;
            """)
            logger.info("已检查并确保所有必要列存在")
        except Exception as column_e:
            logger.warning(f"检查或添加列时出错: {str(column_e)}")
        
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
        try:
            # 首先尝试带有customer_name和qr_image的完整插入，包括默认account值
            order_id = execute_query(
                """
                INSERT INTO orders (customer_name, package, qr_image, status, created_at, notified, account)
                VALUES (%s, %s, %s, %s, %s, 0, '')
                RETURNING id
                """,
                (customer_name, package, qr_image, STATUS['SUBMITTED'], created_at),
                fetch=True
            )
            return order_id[0][0] if order_id else None
        except Exception as e:
            error_msg = str(e)
            
            # 如果某列不存在，尝试备用方案
            if 'column "customer_name" of relation "orders" does not exist' in error_msg:
                # 如果customer_name列不存在，尝试不使用该字段
                logger.warning("customer_name列不存在，尝试不使用该字段进行插入")
                try:
                    order_id = execute_query(
                        """
                        INSERT INTO orders (package, qr_image, status, created_at, notified, account)
                        VALUES (%s, %s, %s, %s, 0, '')
                        RETURNING id
                        """,
                        (package, qr_image, STATUS['SUBMITTED'], created_at),
                        fetch=True
                    )
                    return order_id[0][0] if order_id else None
                except Exception as sub_e:
                    # 继续检查其他列问题
                    error_msg = str(sub_e)
                    
            if 'column "qr_image" of relation "orders" does not exist' in error_msg:
                # 如果qr_image列不存在，尝试使用最小化字段
                logger.warning("qr_image列不存在，尝试使用最小化字段进行插入")
                # 只使用最基本的字段进行插入
                try:
                    order_id = execute_query(
                        """
                        INSERT INTO orders (status, created_at, account)
                        VALUES (%s, %s, '')
                        RETURNING id
                        """,
                        (STATUS['SUBMITTED'], created_at),
                        fetch=True
                    )
                    # 如果插入成功，尝试用UPDATE添加其他字段的值
                    if order_id:
                        order_id_val = order_id[0][0]
                        # 尝试更新qr_image
                        try:
                            execute_query(
                                "UPDATE orders SET qr_image = %s WHERE id = %s",
                                (qr_image, order_id_val)
                            )
                        except:
                            logger.warning(f"无法更新订单 {order_id_val} 的qr_image字段")
                            
                        # 尝试更新package
                        try:
                            execute_query(
                                "UPDATE orders SET package = %s WHERE id = %s",
                                (package, order_id_val)
                            )
                        except:
                            logger.warning(f"无法更新订单 {order_id_val} 的package字段")
                            
                        # 尝试更新customer_name
                        try:
                            execute_query(
                                "UPDATE orders SET customer_name = %s WHERE id = %s",
                                (customer_name, order_id_val)
                            )
                        except:
                            logger.warning(f"无法更新订单 {order_id_val} 的customer_name字段")
                            
                        return order_id_val
                    return None
                except Exception as min_e:
                    logger.error(f"使用最小化字段创建订单失败: {str(min_e)}")
                    # 再次失败，尝试最后一种方法
                    try:
                        # 尝试只提供关键字段，最大化成功率
                        logger.warning("尝试最简单的插入方式，仅提供必需字段")
                        query = "INSERT INTO orders (account) VALUES ('') RETURNING id"
                        order_id = execute_query(query, (), fetch=True)
                        if order_id:
                            return order_id[0][0]
                    except Exception as last_e:
                        logger.error(f"最后尝试创建订单失败: {str(last_e)}")
                        raise
            
            # 如果错误与account字段有关
            if 'null value in column "account"' in error_msg:
                logger.warning("account字段不允许为空，尝试提供空字符串")
                try:
                    order_id = execute_query(
                        """
                        INSERT INTO orders (customer_name, package, qr_image, status, created_at, notified, account)
                        VALUES (%s, %s, %s, %s, %s, 0, '')
                        RETURNING id
                        """,
                        (customer_name, package, qr_image, STATUS['SUBMITTED'], created_at),
                        fetch=True
                    )
                    return order_id[0][0] if order_id else None
                except Exception as acc_e:
                    logger.error(f"提供account字段后创建订单失败: {str(acc_e)}")
                    # 尝试最后一种方法
                    try:
                        # 尝试只提供关键字段，最大化成功率
                        logger.warning("尝试最简单的插入方式，仅提供必需字段")
                        query = "INSERT INTO orders (account) VALUES ('') RETURNING id"
                        order_id = execute_query(query, (), fetch=True)
                        if order_id:
                            order_id_val = order_id[0][0]
                            # 后续尝试更新其他字段
                            for field, value in [('customer_name', customer_name), 
                                               ('package', package), 
                                               ('qr_image', qr_image), 
                                               ('status', STATUS['SUBMITTED']), 
                                               ('created_at', created_at),
                                               ('notified', 0)]:
                                try:
                                    execute_query(f"UPDATE orders SET {field} = %s WHERE id = %s", (value, order_id_val))
                                except:
                                    pass
                            return order_id_val
                    except Exception as last_e:
                        logger.error(f"最后尝试创建订单失败: {str(last_e)}")
                        raise
            
            # 其他错误直接抛出
            raise
    except Exception as e:
        logger.error(f"创建订单失败: {str(e)}", exc_info=True)
        return None

def get_order_details(order_id):
    """获取订单详情"""
    try:
        try:
            # 首先尝试查询包含所有字段的完整数据
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
                'seller_first_name': order[0][11],
                'account': ""  # 默认添加account字段，避免前端可能用到但我们不主动查询
            }
            
            return order_data
            
        except Exception as e:
            error_msg = str(e)
            
            # 处理customer_name列不存在的情况
            if 'column "customer_name" of relation "orders" does not exist' in error_msg:
                logger.warning("customer_name列不存在，使用替代查询获取订单详情")
                try:
                    # 尝试查询不包含customer_name的数据
                    order = execute_query(
                        """
                        SELECT id, package, qr_image, status, message, 
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
                        'customer_name': "",  # 提供默认空值
                        'package': order[0][1],
                        'qr_image': order[0][2],
                        'status': order[0][3],
                        'message': order[0][4],
                        'created_at': order[0][5],
                        'paid_at': order[0][6],
                        'confirmed_at': order[0][7],
                        'seller_id': order[0][8],
                        'seller_username': order[0][9],
                        'seller_first_name': order[0][10]
                    }
                    
                    return order_data
                except Exception as sub_e:
                    error_msg = str(sub_e)
            
            # 处理qr_image列不存在的情况
            if 'column "qr_image" of relation "orders" does not exist' in error_msg:
                logger.warning("qr_image列不存在，使用最小化查询获取订单详情")
                try:
                    # 尝试获取最基本的订单数据
                    # 使用一系列单独的查询，逐个获取可能存在的字段
                    basic_data = execute_query(
                        """
                        SELECT id, status, created_at
                        FROM orders
                        WHERE id = %s
                        """,
                        (order_id,),
                        fetch=True
                    )
                    
                    if not basic_data or len(basic_data) == 0:
                        return None
                    
                    # 创建基本订单数据
                    order_data = {
                        'id': basic_data[0][0],
                        'status': basic_data[0][1],
                        'created_at': basic_data[0][2],
                        'customer_name': "",
                        'package': "default",
                        'qr_image': "",
                        'message': "",
                        'paid_at': "",
                        'confirmed_at': "",
                        'seller_id': "",
                        'seller_username': "",
                        'seller_first_name': ""
                    }
                    
                    # 尝试获取可选字段
                    try_get_field(order_data, order_id, 'customer_name')
                    try_get_field(order_data, order_id, 'package')
                    try_get_field(order_data, order_id, 'qr_image')
                    try_get_field(order_data, order_id, 'message')
                    try_get_field(order_data, order_id, 'paid_at')
                    try_get_field(order_data, order_id, 'confirmed_at')
                    try_get_field(order_data, order_id, 'seller_id')
                    try_get_field(order_data, order_id, 'seller_username')
                    try_get_field(order_data, order_id, 'seller_first_name')
                    
                    return order_data
                    
                except Exception as min_e:
                    logger.error(f"获取最小化订单详情失败: {str(min_e)}")
                    # 再次失败，继续抛出
                    raise
                
            # 其他错误直接抛出
            raise
                
    except Exception as e:
        logger.error(f"获取订单详情失败: {str(e)}", exc_info=True)
        return None

def try_get_field(order_data, order_id, field_name):
    """尝试从订单表获取特定字段的值"""
    try:
        result = execute_query(
            f"SELECT {field_name} FROM orders WHERE id = %s",
            (order_id,),
            fetch=True
        )
        if result and len(result) > 0 and result[0][0] is not None:
            order_data[field_name] = result[0][0]
    except:
        # 如果字段不存在或查询失败，保持默认值
        pass

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
            
        # 确保account字段有值
        try:
            # 检查订单是否存在account值
            result = execute_query(
                """
                SELECT 1 FROM orders 
                WHERE id = %s AND (account IS NULL OR account = '')
                """,
                (order_id,),
                fetch=True
            )
            
            # 如果结果非空，说明account为空或NULL，需要更新
            if result and len(result) > 0:
                query_parts.append("account = %s")
                params.append("")  # 提供一个空字符串
        except:
            # 如果查询失败（例如account列不存在），忽略这部分
            logger.warning("检查account字段失败，可能不存在该列")
        
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