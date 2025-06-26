import os
import logging

# 设置日志
logger = logging.getLogger(__name__)

# Telegram Bot Token - 从环境变量获取
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
if not BOT_TOKEN:
    logger.warning("未设置TELEGRAM_BOT_TOKEN环境变量，Telegram机器人功能将不可用")

# 订单状态常量
STATUS = {
    'SUBMITTED': 'submitted',  # 已提交
    'ACCEPTED': 'accepted',    # 已接单
    'COMPLETED': 'completed',  # 已完成
    'FAILED': 'failed'         # 失败
}

# 状态对应的中文文本
STATUS_TEXT_ZH = {
    'submitted': '待处理',
    'accepted': '已接单',
    'completed': '已完成',
    'failed': '充值失败'
}

# 从环境变量获取卖家ID
def get_env_sellers():
    """从环境变量获取卖家ID列表"""
    seller_ids_str = os.environ.get('SELLER_IDS', '')
    if not seller_ids_str:
        return []
    
    try:
        return [s.strip() for s in seller_ids_str.split(',') if s.strip()]
    except Exception as e:
        logger.error(f"解析卖家ID环境变量出错: {str(e)}")
        return []

# 同步环境变量中的卖家到数据库
def sync_env_sellers_to_db():
    """同步环境变量中的卖家ID到数据库"""
    from modules_minimal.database import add_seller
    
    seller_ids = get_env_sellers()
    if not seller_ids:
        logger.warning("环境变量中未设置卖家ID，跳过同步")
        return
    
    for seller_id in seller_ids:
        try:
            add_seller(seller_id, f"env_seller_{seller_id}", f"Seller {seller_id}", f"卖家 {seller_id}")
            logger.info(f"已同步卖家ID: {seller_id}")
        except Exception as e:
            logger.error(f"同步卖家ID {seller_id} 到数据库失败: {str(e)}") 