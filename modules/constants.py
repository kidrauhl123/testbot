import os
from collections import defaultdict

# ✅ 写死变量（优先）
if not os.environ.get('BOT_TOKEN'):
    os.environ['BOT_TOKEN'] = '7952478409:AAHdi7_JOjpHu_WAM8mtBewe0m2GWLLmvEk'

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_IDS = [int(x) for x in os.environ.get("ADMIN_CHAT_IDS", "").split(",") if x.strip()]

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
notified_orders_lock = None  # 在主应用中初始化

# 数据库连接URL（用于PostgreSQL判断，默认为SQLite）
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///orders.db')

# 用户信息缓存
user_info_cache = {} 