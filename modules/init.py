import logging
from modules.database import init_db, create_notifications_table, get_db_connection

# 设置日志
logger = logging.getLogger(__name__)

def initialize_app():
    """初始化应用程序"""
    try:
        # 初始化数据库
        logger.info("开始初始化数据库...")
        init_db()
        logger.info("数据库初始化完成")
        
        # 创建通知表
        logger.info("开始创建通知表...")
        create_notifications_table()
        logger.info("通知表创建完成")
        
        # 测试数据库连接
        conn = get_db_connection()
        if conn:
            logger.info("数据库连接测试成功")
            conn.close()
        else:
            logger.error("数据库连接测试失败")
            
    except Exception as e:
        logger.error(f"初始化应用程序失败: {str(e)}", exc_info=True) 