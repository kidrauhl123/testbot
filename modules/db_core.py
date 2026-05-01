import logging

import psycopg2

from modules.constants import DATABASE_URL

logger = logging.getLogger(__name__)


def ensure_postgres_configured():
    """确保应用只连接 PostgreSQL，避免误回退到历史 SQLite 数据库。"""
    if not DATABASE_URL.startswith(('postgres://', 'postgresql://')):
        raise RuntimeError(
            "DATABASE_URL 必须配置为 PostgreSQL 连接串；本项目已停用 SQLite。"
        )


def get_postgres_connection():
    """创建 PostgreSQL 连接。后续可以在这里替换成连接池。"""
    ensure_postgres_configured()
    return psycopg2.connect(DATABASE_URL)


def execute_postgres_query(query, params=(), fetch=False, return_cursor=False):
    """执行PostgreSQL查询并返回结果"""
    conn = get_postgres_connection()
    cursor = conn.cursor()

    if params:
        cursor.execute(query, params)
    else:
        cursor.execute(query)

    if return_cursor:
        conn.commit()
        return cursor

    result = None
    if fetch:
        result = cursor.fetchall()

    conn.commit()
    conn.close()
    return result


# 数据库执行函数
def execute_query(query, params=(), fetch=False, return_cursor=False):
    """执行 PostgreSQL 查询并返回结果。"""
    ensure_postgres_configured()
    logger.debug(f"执行查询: {query[:50]}... 参数: {params}")
    return execute_postgres_query(query, params, fetch, return_cursor)
