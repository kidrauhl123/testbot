import os
import logging
import threading
from collections import defaultdict
import time

# 设置日志
logger = logging.getLogger(__name__)

# ✅ Telegram Bot Token
if not os.environ.get('BOT_TOKEN'):
    os.environ['BOT_TOKEN'] = '12345:ABCDEFG'  # 需要替换为真实的 Telegram Bot Token

BOT_TOKEN = os.environ["BOT_TOKEN"]

# ✅ 管理员默认凭证
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin')

if ADMIN_USERNAME == 'admin' or ADMIN_PASSWORD == 'admin':
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
        db_seller_ids = [int(row[0]) for row in db_seller_ids] if db_seller_ids else []
        
        # 将环境变量中的卖家ID添加到数据库
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        for seller_id in SELLER_CHAT_IDS:
            # 确保seller_id是整数
            seller_id = int(seller_id)
            if seller_id not in db_seller_ids:
                logger.info(f"将环境变量中的卖家ID {seller_id} 同步到数据库")
                execute_query(
                    "INSERT INTO sellers (telegram_id, username, first_name, is_active, added_at, added_by) VALUES (%s, %s, %s, %s, %s, %s)",
                    (seller_id, f"env_seller_{seller_id}", f"环境变量卖家 {seller_id}", 1, timestamp, "环境变量")
                )
    except Exception as e:
        logger.error(f"同步环境变量卖家到数据库失败: {e}")

# ===== YouTube充值系统价格 =====
# 不同套餐的价格 (人民币)
DEFAULT_PACKAGE = 'default'
RECHARGE_PRICES = {'7': 20, '30': 50, '90': 120, '365': 400, DEFAULT_PACKAGE: 50}

# ===== 订单状态常量 =====
STATUS = {
    'SUBMITTED': 'submitted',  # 已提交
    'PAID': 'paid',            # 已支付
    'CONFIRMED': 'confirmed',  # 已确认
    'FAILED': 'failed',        # 失败
    'NEED_NEW_QR': 'need_new_qr' # 需要新二维码
}

STATUS_TEXT_ZH = {
    'submitted': '已提交', 
    'paid': '已支付', 
    'confirmed': '已确认',
    'failed': '充值失败', 
    'need_new_qr': '需要新二维码'
}

STATUS_TEXT_EN = {
    'submitted': 'Submitted', 
    'paid': 'Paid', 
    'confirmed': 'Confirmed',
    'failed': 'Failed', 
    'need_new_qr': 'Need New QR'
}

PLAN_OPTIONS = [('7', '7天'), ('30', '30天'), ('90', '90天'), ('365', '365天')]
PLAN_LABELS_ZH = {v: l for v, l in PLAN_OPTIONS}
PLAN_LABELS_EN = {'7': '7 Days', '30': '30 Days', '90': '90 Days', '365': '365 Days'}

# 全局变量
notified_orders = set()
notified_orders_lock = threading.Lock()

# 数据库连接URL
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgres://postgres:postgres@localhost:5432/youtube_recharge')

# 用户信息缓存
user_info_cache = {} 