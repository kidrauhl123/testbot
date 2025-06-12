import os
from collections import defaultdict
import threading

# ✅ 写死变量（优先）
if not os.environ.get('BOT_TOKEN'):
    os.environ['BOT_TOKEN'] = '7952478409:AAHdi7_JOjpHu_WAM8mtBewe0m2GWLLmvEk'

BOT_TOKEN = os.environ["BOT_TOKEN"]

# 确保管理员ID列表中始终包含1878943383
default_admin = 1878943383
SELLER_CHAT_IDS_ENV = os.environ.get("SELLER_CHAT_IDS", str(default_admin))

SELLER_CHAT_IDS = []
if SELLER_CHAT_IDS_ENV.strip():
    # 解析环境变量中的卖家ID
    for x in SELLER_CHAT_IDS_ENV.split(","):
        if x.strip():
            try:
                SELLER_CHAT_IDS.append(int(x.strip()))
            except ValueError:
                pass

# 如果默认管理员不在列表中，则添加
if default_admin not in SELLER_CHAT_IDS:
    SELLER_CHAT_IDS.append(default_admin)

# 同步回环境变量，以确保一致性
os.environ['SELLER_CHAT_IDS'] = ",".join(map(str, SELLER_CHAT_IDS))

# ===== 价格系统 =====
# 网页端价格（人民币）
WEB_PRICES = {'1': 12, '2': 18, '3': 30, '6': 50, '12': 84}
# Telegram端管理员薪资（美元）
TG_PRICES = {'1': 1.35, '2': 1.3, '3': 3.2, '6': 5.7, '12': 9.2}

# ===== 状态常量 =====
STATUS = {
    'SUBMITTED': 'submitted',
    'ACCEPTED': 'accepted',
    'COMPLETED': 'completed',
    'FAILED': 'failed',
    'CANCELLED': 'cancelled'
}
STATUS_TEXT_ZH = {
    'submitted': '已提交', 'accepted': '已接单', 'completed': '充值成功',
    'failed': '充值失败', 'cancelled': '已撤销'
}
PLAN_OPTIONS = [('1', '1个月'), ('2', '2个月'), ('3', '3个月'), ('6', '6个月'), ('12', '12个月')]
PLAN_LABELS_ZH = {v: l for v, l in PLAN_OPTIONS}
PLAN_LABELS_EN = {'1': '1 Month', '2': '2 Months', '3': '3 Months', '6': '6 Months', '12': '12 Months'}

# ===== 全局变量 =====
user_languages = defaultdict(lambda: 'en')
feedback_waiting = {}
notified_orders = set()
notified_orders_lock = threading.Lock()  # 在主应用中初始化

# 数据库连接URL（用于PostgreSQL判断，默认为SQLite）
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///orders.db')

# 用户信息缓存
user_info_cache = {} 