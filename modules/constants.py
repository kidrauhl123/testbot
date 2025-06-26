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
    from modules.database import add_seller
    
    seller_ids = get_env_sellers()
    if not seller_ids:
        logger.warning("环境变量中未设置卖家ID，跳过同步")
        return
    
    for seller_id in seller_ids:
        try:
            add_seller(seller_id, f"env_seller_{seller_id}", f"Seller {seller_id}", f"卖家 {seller_id}", "环境变量")
            logger.info(f"已同步卖家ID: {seller_id}")
        except Exception as e:
            logger.error(f"同步卖家ID {seller_id} 到数据库失败: {str(e)}")

# ===== 价格系统 =====
# 网页端价格（美元USDT）
WEB_PRICES = {'12': 20}
# Telegram端卖家薪资（美元）
TG_PRICES = {'12': 10}

# 获取用户套餐价格
def get_user_package_price(user_id, package):
    """
    获取特定用户的套餐价格
    
    参数:
    - user_id: 用户ID
    - package: 套餐（如'1'，'2'等）
    
    返回:
    - 用户的套餐价格，如果没有定制价格则返回默认价格
    """
    # 如果没有用户ID，返回默认价格
    if not user_id:
        return WEB_PRICES.get(package, 0)
        
    # 避免循环导入
    from modules.database import get_user_custom_prices
    
    # 获取用户定制价格
    custom_prices = get_user_custom_prices(user_id)
    
    # 如果该套餐有定制价格，返回定制价格，否则返回默认价格
    return custom_prices.get(package, WEB_PRICES.get(package, 0))

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
PLAN_OPTIONS = [('12', '一年个人会员')]
PLAN_LABELS_ZH = {v: l for v, l in PLAN_OPTIONS}
PLAN_LABELS_EN = {'12': '1 Year Premium'}

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