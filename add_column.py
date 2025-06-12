import sqlite3
import os
import logging

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def add_balance_column():
    """为users表添加balance字段"""
    try:
        # 连接数据库
        conn = sqlite3.connect("orders.db")
        cursor = conn.cursor()
        
        # 检查balance列是否存在
        cursor.execute("PRAGMA table_info(users)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'balance' not in columns:
            logger.info("添加balance列到users表")
            cursor.execute("ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0")
            conn.commit()
            logger.info("成功添加balance列")
        else:
            logger.info("balance列已存在")
        
        conn.close()
        logger.info("数据库更新完成")
        
    except Exception as e:
        logger.error(f"更新数据库失败: {str(e)}")

if __name__ == "__main__":
    add_balance_column() 