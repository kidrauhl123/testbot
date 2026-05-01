import logging

from modules.db_core import execute_query, get_postgres_connection
from modules.order_balance import get_china_time

logger = logging.getLogger(__name__)


# ===== 激活码系统 =====
def create_activation_code_table():
    """创建激活码表。"""
    try:
        execute_query("""
            CREATE TABLE IF NOT EXISTS activation_codes (
                id SERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                package TEXT NOT NULL,
                is_used INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                used_at TEXT,
                used_by INTEGER,
                created_by INTEGER,
                FOREIGN KEY (used_by) REFERENCES users (id),
                FOREIGN KEY (created_by) REFERENCES users (id)
            )
        """)
        logger.info("已确保激活码表存在(PostgreSQL)")
        return True
    except Exception as e:
        logger.error(f"创建激活码表失败: {str(e)}", exc_info=True)
        return False

def generate_activation_code(length=16):
    """生成唯一的激活码。"""
    import random
    import string

    while True:
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
        existing = execute_query(
            "SELECT id FROM activation_codes WHERE code = %s",
            (code,),
            fetch=True,
        )
        if not existing:
            return code

def create_activation_code(package, created_by=None, count=1):
    """创建激活码。"""
    codes = []
    now = get_china_time()

    for _ in range(count):
        code = generate_activation_code()
        result = execute_query("""
            INSERT INTO activation_codes (code, package, created_at, created_by, is_used)
            VALUES (%s, %s, %s, %s, 0)
            RETURNING id
        """, (code, package, now, created_by), fetch=True)
        codes.append({"id": result[0][0], "code": code})

    return codes

def get_activation_code(code):
    """获取激活码信息。"""
    try:
        result = execute_query("""
            SELECT id, code, package, is_used, created_at, used_at, used_by
            FROM activation_codes
            WHERE code = %s
        """, (code,), fetch=True)

        if result and len(result) > 0:
            return {
                "id": result[0][0],
                "code": result[0][1],
                "package": result[0][2],
                "is_used": result[0][3],
                "created_at": result[0][4],
                "used_at": result[0][5],
                "used_by": result[0][6]
            }
        return None
    except Exception as e:
        logger.error(f"获取激活码信息失败: {str(e)}", exc_info=True)
        return None

def mark_activation_code_used(code_id, user_id):
    """标记激活码为已使用。"""
    now = get_china_time()
    conn = None
    try:
        if user_id <= 0:
            user_id = None

        conn = get_postgres_connection()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE activation_codes
            SET is_used = 1, used_at = %s, used_by = %s
            WHERE id = %s AND is_used = 0
        """, (now, user_id, code_id))
        rows_updated = cursor.rowcount

        if rows_updated > 0:
            conn.commit()
            return True

        conn.rollback()
        return False
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"标记激活码已使用失败: {str(e)}", exc_info=True)
        return False
    finally:
        if conn:
            conn.close()

def get_admin_activation_codes(limit=100, offset=0, conditions=None, params=None):
    """获取所有激活码（管理员用）。"""
    try:
        where_clause = ""
        query_params = []

        if conditions and params:
            where_clause = " WHERE " + " AND ".join(conditions)
            query_params.extend(params)

        query_params.extend([limit, offset])
        result = execute_query(f"""
            SELECT a.id, a.code, a.package, a.is_used, a.created_at, a.used_at,
                   c.username as creator, u.username as user
            FROM activation_codes a
            LEFT JOIN users c ON a.created_by = c.id
            LEFT JOIN users u ON a.used_by = u.id
            {where_clause}
            ORDER BY a.created_at DESC
            LIMIT %s OFFSET %s
        """, query_params, fetch=True)

        codes = []
        for r in result:
            codes.append({
                "id": r[0],
                "code": r[1],
                "package": r[2],
                "is_used": r[3],
                "created_at": r[4],
                "used_at": r[5],
                "creator": r[6],
                "user": r[7]
            })
        return codes
    except Exception as e:
        logger.error(f"获取激活码列表失败: {str(e)}", exc_info=True)
        return []
