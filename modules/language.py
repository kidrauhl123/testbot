"""
语言资源文件，包含系统中使用的所有字符串的中英文版本
"""

# 默认语言设置
DEFAULT_LANGUAGE = 'zh'

# 语言选项
LANGUAGES = {
    'zh': '中文',
    'en': 'English'
}

def get_string(key, lang=None):
    """
    获取指定键的本地化字符串
    
    Args:
        key: 字符串键
        lang: 指定语言，如果为None则使用默认语言
    
    Returns:
        本地化后的字符串，如果不存在则返回键本身
    """
    if lang is None:
        lang = DEFAULT_LANGUAGE
        
    return STRINGS.get(lang, {}).get(key, STRINGS.get(DEFAULT_LANGUAGE, {}).get(key, key))

# 字符串资源
STRINGS = {
    'zh': {
        # 通用
        'site_title': '破天充值系统',
        'admin': '管理员',
        'balance': '余额',
        'credit_limit': '额度',
        'loading': '加载中...',
        'recharge': '充值',
        'login': '登录',
        'username': '用户名',
        'password': '密码',
        'enter_username': '请输入用户名',
        'enter_password': '请输入密码',
        'or': '或',
        'no_account': '还没有账号？立即注册',
        'logging_in': '登录中...',
        
        # 导航栏
        'account_info': '账户信息',
        'account_balance': '账户余额',
        'admin_panel': '管理后台',
        'my_dashboard': '我的后台',
        'logout': '退出登录',
        
        # 首页
        'recharge_order': '充值下单',
        'potian_account': '破天账号',
        'password': '密码',
        'package_type': '套餐类型',
        'price': '价格',
        'remark': '备注 (可选)',
        'submit_order': '提交订单',
        'my_orders': '我的订单',
        'system_order_status': '系统订单状态',
        'search_placeholder': '搜索订单号或账号...',
        'refresh_hint': '(显示全部最新订单，每5秒自动更新)',
        'show_more': '显示更多',
        'month': '个月',
        
        # 错误消息
        'error_empty_fields': '请填写所有字段',
        'error_password_mismatch': '两次密码输入不一致',
        'error_username_exists': '用户名已存在',
        'error_login_failed': '用户名或密码错误',
        
        # 订单状态
        'submitted': '已提交',
        'accepted': '已接单',
        'completed': '已完成',
        'failed': '失败',
        'cancelled': '已取消',
        'disputed': '有争议'
    },
    'en': {
        # General
        'site_title': 'Potian Recharge System',
        'admin': 'Admin',
        'balance': 'Balance',
        'credit_limit': 'Credit',
        'loading': 'Loading...',
        'recharge': 'Recharge',
        'login': 'Login',
        'username': 'Username',
        'password': 'Password',
        'enter_username': 'Enter your username',
        'enter_password': 'Enter your password',
        'or': 'OR',
        'no_account': 'No account? Register now',
        'logging_in': 'Logging in...',
        
        # Navigation
        'account_info': 'Account Information',
        'account_balance': 'Account Balance',
        'admin_panel': 'Admin Panel',
        'my_dashboard': 'My Dashboard',
        'logout': 'Logout',
        
        # Home Page
        'recharge_order': 'Create Order',
        'potian_account': 'Potian Account',
        'password': 'Password',
        'package_type': 'Package Type',
        'price': 'Price',
        'remark': 'Remark (Optional)',
        'submit_order': 'Submit Order',
        'my_orders': 'My Orders',
        'system_order_status': 'System Order Status',
        'search_placeholder': 'Search order ID or account...',
        'refresh_hint': '(Showing all latest orders, auto-refresh every 5 seconds)',
        'show_more': 'Show More',
        'month': ' Month',
        
        # Error Messages
        'error_empty_fields': 'Please fill in all fields',
        'error_password_mismatch': 'Passwords do not match',
        'error_username_exists': 'Username already exists',
        'error_login_failed': 'Invalid username or password',
        
        # Order Status
        'submitted': 'Submitted',
        'accepted': 'Accepted',
        'completed': 'Completed',
        'failed': 'Failed',
        'cancelled': 'Cancelled',
        'disputed': 'Disputed'
    }
} 