import os
import logging
from collections import defaultdict

# 设置日志
logger = logging.getLogger(__name__)

# ✅ Telegram Bot Token
if not os.environ.get('BOT_TOKEN'):
    os.environ['BOT_TOKEN'] = '7952478409:AAHdi7_JOjpHu_WAM8mtBewe0m2GWLLmvEk'

BOT_TOKEN = os.environ["BOT_TOKEN"]

# ✅ 管理员默认凭证
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
        logger.info(f"从环境变量加载了 {len(SELLER_CHAT_IDS)} 个卖家ID")
    except Exception as e:
        logger.error(f"从环境变量加载卖家ID失败: {str(e)}")

# 订单状态常量
STATUS = {
    "SUBMITTED": "submitted",  # 已提交，等待接单
    "ACCEPTED": "accepted",    # 已接单，处理中
    "COMPLETED": "completed",  # 已完成
    "FAILED": "failed",        # 充值失败
    "CANCELLED": "cancelled",  # 已取消
}

# 状态显示文本
STATUS_TEXT_ZH = {
    STATUS["SUBMITTED"]: "待处理",
    STATUS["ACCEPTED"]: "处理中",
    STATUS["COMPLETED"]: "已完成",
    STATUS["FAILED"]: "充值失败",
    STATUS["CANCELLED"]: "已取消",
}

# 套餐选项
PLAN_OPTIONS = [
    ('12', '一年YouTube会员'),
]

# 套餐标签（英文版）
PLAN_LABELS_EN = {
    '12': '1 Year Premium',
}

# 数据库URL
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite')

# 将环境变量中的卖家同步到数据库
def sync_env_sellers_to_db():
    """将环境变量中的卖家ID同步到数据库"""
    if not SELLER_CHAT_IDS:
        return

    try:
        from modules.database import execute_query
        
        for seller_id in SELLER_CHAT_IDS:
            # 检查卖家是否已存在
            existing = execute_query(
                "SELECT telegram_id FROM sellers WHERE telegram_id = ?", 
                (str(seller_id),), 
                fetch=True
            )
            
            if not existing:
                # 添加新卖家
                execute_query(
                    """
                    INSERT INTO sellers (telegram_id, username, first_name, is_active, added_at, added_by)
                    VALUES (?, ?, ?, 1, datetime('now'), 'system')
                    """,
                    (str(seller_id), f"Seller_{seller_id}", f"Seller {seller_id}")
                )
                logger.info(f"已将卖家 {seller_id} 添加到数据库")
            else:
                # 确保卖家是活跃状态
                execute_query(
                    "UPDATE sellers SET is_active = 1 WHERE telegram_id = ?",
                    (str(seller_id),)
                )
                logger.info(f"已将卖家 {seller_id} 状态更新为活跃")
    
    except Exception as e:
        logger.error(f"同步卖家到数据库失败: {str(e)}")

# ===== 价格系统 =====
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
        return 0
        
    # 避免循环导入
    from modules.database import get_user_custom_prices
    
    # 获取用户定制价格
    custom_prices = get_user_custom_prices(user_id)
    
    # 如果该套餐有定制价格，返回定制价格，否则返回默认价格
    return custom_prices.get(package, 0)

# ===== 状态常量 =====
STATUS_TEXT_ZH = {
    'submitted': '已提交', 'accepted': '已接单', 'completed': '充值成功',
    'failed': '充值失败', 'cancelled': '已撤销', 'disputing': '正在质疑'
}
PLAN_LABELS_ZH = {v: l for v, l in PLAN_OPTIONS}

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

# 用户信息缓存
user_info_cache = {} 