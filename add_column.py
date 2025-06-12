import sqlite3
import os
import logging

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def update_database():
    """更新数据库结构，添加余额、透支额度和退款字段"""
    try:
        # 连接数据库
        conn = sqlite3.connect("orders.db")
        cursor = conn.cursor()
        
        # 检查users表中是否存在balance列
        cursor.execute("PRAGMA table_info(users)")
        users_columns = [column[1] for column in cursor.fetchall()]
        
        if 'balance' not in users_columns:
            logger.info("添加balance列到users表")
            cursor.execute("ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0")
            conn.commit()
            logger.info("成功添加balance列")
        else:
            logger.info("balance列已存在")
        
        # 检查users表中是否存在credit_limit列
        if 'credit_limit' not in users_columns:
            logger.info("添加credit_limit列到users表")
            cursor.execute("ALTER TABLE users ADD COLUMN credit_limit REAL DEFAULT 0")
            conn.commit()
            logger.info("成功添加credit_limit列")
        else:
            logger.info("credit_limit列已存在")
        
        # 检查orders表中是否存在refunded列
        cursor.execute("PRAGMA table_info(orders)")
        orders_columns = [column[1] for column in cursor.fetchall()]
        
        if 'refunded' not in orders_columns:
            logger.info("添加refunded列到orders表")
            cursor.execute("ALTER TABLE orders ADD COLUMN refunded INTEGER DEFAULT 0")
            conn.commit()
            logger.info("成功添加refunded列")
        else:
            logger.info("refunded列已存在")
        
        conn.close()
        logger.info("数据库更新完成")
        
    except Exception as e:
        logger.error(f"更新数据库失败: {str(e)}")

if __name__ == "__main__":
    update_database() 