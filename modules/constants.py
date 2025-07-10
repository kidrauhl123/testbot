import os
from collections import defaultdict
import threading
import logging
import time

# 设置日志
logger = logging.getLogger(__name__)

# ✅ 写死变量（优先）
if not os.environ.get('BOT_TOKEN'):
    os.environ['BOT_TOKEN'] = '7952478409:AAHdi7_JOjpHu_WAM8mtBewe0m2GWLLmvEk'

BOT_TOKEN = os.environ["BOT_TOKEN"]

# ✅ 管理员默认凭证（优先从环境变量读取）
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', '755439')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '755439')

if ADMIN_USERNAME == '755439' or ADMIN_PASSWORD == '755439':
    logger.warning("正在使用默认的管理员凭证。为了安全，请设置 ADMIN_USERNAME 和 ADMIN_PASSWORD 环境变量。")

# 支持通过环境变量设置卖家ID
SELLER_CHAT_IDS = []
if os.environ.get('SELLER_CHAT_IDS'):
    try:
        # 格式: "123456789,987654321"
        seller_ids_str = os.environ.get('SELLER_CHAT_IDS', '')
        SELLER_CHAT_IDS = [int(x.strip()) for x in seller_ids_str.split(',') if x.strip()]
        logger.info(f"从环境变量加载卖家ID: {SELLER_CHAT_IDS}")
    except Exception as e:
        logger.error(f"解析SELLER_CHAT_IDS环境变量出错: {e}")

# 将环境变量中的卖家ID同步到数据库
def sync_env_sellers_to_db():
    """将环境变量中的卖家ID同步到数据库"""
    if not SELLER_CHAT_IDS:
        return
    
    # 导入放在函数内部，避免循环导入
    from modules.database import execute_query
    
    # 获取数据库中已存在的卖家ID
    try:
        db_seller_ids = execute_query("SELECT telegram_id FROM sellers", fetch=True)
        db_seller_ids = [row[0] for row in db_seller_ids] if db_seller_ids else []
        
        # 将环境变量中的卖家ID添加到数据库
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        for seller_id in SELLER_CHAT_IDS:
            if seller_id not in db_seller_ids:
                logger.info(f"将环境变量中的卖家ID {seller_id} 同步到数据库")
                execute_query(
                    "INSERT INTO sellers (telegram_id, username, first_name, nickname, is_active, added_at, added_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (seller_id, f"env_seller_{seller_id}", f"环境变量卖家 {seller_id}", f"卖家 {seller_id}", 1, timestamp, "环境变量")
                )
    except Exception as e:
        logger.error(f"同步环境变量卖家到数据库失败: {e}")

# ===== 状态常量 =====
STATUS = {
    'SUBMITTED': 'submitted',
    'ACCEPTED': 'accepted',
    'COMPLETED': 'completed',
    'FAILED': 'failed',
    'CANCELLED': 'cancelled',
    'DISPUTING': 'disputing'
}
STATUS_TEXT_ZH = {
    'submitted': '已提交', 'accepted': '已接单', 'completed': '充值成功',
    'failed': '充值失败', 'cancelled': '已撤销', 'disputing': '正在质疑'
}

# 确认状态常量
CONFIRM_STATUS = {
    'PENDING': 'pending',
    'CONFIRMED': 'confirmed',
    'NOT_RECEIVED': 'not_received'
}

CONFIRM_STATUS_TEXT_ZH = {
    'pending': '待确认',
    'confirmed': '确认收到',
    'not_received': '长时间未收到'
}

# 失败原因的中英文映射
REASON_TEXT_ZH = {
    'Wrong password': '密码错误',
    'Membership not expired': '会员未到期',
    'Other reason': '其他原因',
    'Other reason (details pending)': '其他原因',
    'Unknown reason': '未知原因'
}

# ===== 全局变量 =====
user_languages = defaultdict(lambda: 'en')
feedback_waiting = {}
notified_orders = set()
notified_orders_lock = threading.Lock()  # 在主应用中初始化

# 数据库连接URL（用于PostgreSQL判断，默认为SQLite）
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///orders.db')

# 用户信息缓存
user_info_cache = {} 