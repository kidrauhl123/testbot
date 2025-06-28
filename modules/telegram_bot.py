import asyncio
import threading
import logging
from datetime import datetime, timedelta
from collections import defaultdict
import time
import os
from functools import wraps
import pytz
import sys
import functools
import sqlite3
import traceback
import psycopg2
from urllib.parse import urlparse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters
)

from modules.constants import (
    BOT_TOKEN, STATUS, PLAN_LABELS_EN,
    STATUS_TEXT_ZH, TG_PRICES, WEB_PRICES, SELLER_CHAT_IDS, DATABASE_URL
)
from modules.database import (
    get_order_details, execute_query, 
    get_unnotified_orders, get_active_seller_ids,
    update_seller_desired_orders, update_seller_last_active, get_active_sellers,
    select_active_seller, get_seller_info
)

# 设置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# 中国时区
CN_TIMEZONE = pytz.timezone('Asia/Shanghai')

# 获取数据库连接
def get_db_connection():
    """获取数据库连接，根据环境变量决定使用SQLite或PostgreSQL"""
    
    try:
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL连接
            url = urlparse(DATABASE_URL)
            dbname = url.path[1:]
            user = url.username
            password = url.password
            host = url.hostname
            port = url.port
            
            logger.info(f"连接PostgreSQL数据库: {host}:{port}/{dbname}")
            
            conn = psycopg2.connect(
                dbname=dbname,
                user=user,
                password=password,
                host=host,
                port=port
            )
            return conn
        else:
            # SQLite连接
            # 使用绝对路径访问数据库
            current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(current_dir, "orders.db")
            logger.info(f"连接SQLite数据库: {db_path}")
            print(f"DEBUG: 连接SQLite数据库: {db_path}")
            
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row  # 使查询结果可以通过列名访问
            return conn
    except Exception as e:
        logger.error(f"获取数据库连接时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 获取数据库连接时出错: {str(e)}")
        return None

# 错误处理装饰器
def callback_error_handler(func):
    """装饰器：捕获并处理回调函数中的异常"""
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except Exception as e:
            user_id = None
            try:
                if update.effective_user:
                    user_id = update.effective_user.id
            except:
                pass
            
            error_msg = f"回调处理错误 [{func.__name__}] "
            if user_id:
                error_msg += f"用户ID: {user_id} "
            error_msg += f"错误: {str(e)}"
            
            logger.error(error_msg, exc_info=True)
            print(f"ERROR: {error_msg}")
            
            # 尝试通知用户
            try:
                if update.callback_query:
                    await update.callback_query.answer("Operation failed, please try again later", show_alert=True)
            except Exception as notify_err:
                logger.error(f"无法通知用户错误: {str(notify_err)}")
                print(f"ERROR: 无法通知用户错误: {str(notify_err)}")
            
            return None
    return wrapper

# 获取中国时间的函数
def get_china_time():
    """获取当前中国时间（UTC+8）"""
    utc_now = datetime.now(pytz.utc)
    china_now = utc_now.astimezone(CN_TIMEZONE)
    return china_now.strftime("%Y-%m-%d %H:%M:%S")

# ===== 全局变量 =====
bot_application = None
BOT_LOOP = None

# 跟踪等待额外反馈的订单
feedback_waiting = {}

# 用户信息缓存
user_info_cache = {}

# 全局变量
notification_queue = None  # 将在run_bot函数中初始化

# ===== TG 辅助函数 =====
def is_seller(chat_id):
    """检查用户是否为已授权的卖家"""
    # 只从数据库中获取卖家信息，因为环境变量中的卖家已经同步到数据库
    return chat_id in get_active_seller_ids()

# 添加处理 Telegram webhook 更新的函数
async def process_telegram_update_async(update_data, notification_queue):
    """异步处理来自Telegram webhook的更新"""
    global bot_application
    
    try:
        if not bot_application:
            logger.error("机器人应用未初始化，无法处理webhook更新")
            print("ERROR: 机器人应用未初始化，无法处理webhook更新")
            return
        
        # 将JSON数据转换为Update对象
        update = Update.de_json(data=update_data, bot=bot_application.bot)
        
        if not update:
            logger.error("无法将webhook数据转换为Update对象")
            print("ERROR: 无法将webhook数据转换为Update对象")
            return
        
        # 处理更新
        logger.info(f"正在处理webhook更新: {update.update_id}")
        print(f"DEBUG: 正在处理webhook更新: {update.update_id}")
        
        # 将更新分派给应用程序处理
        await bot_application.process_update(update)
        
        logger.info(f"webhook更新 {update.update_id} 处理完成")
        print(f"DEBUG: webhook更新 {update.update_id} 处理完成")
    
    except Exception as e:
        logger.error(f"处理webhook更新时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 处理webhook更新时出错: {str(e)}")

def process_telegram_update(update_data, notification_queue):
    """处理来自Telegram webhook的更新（同步包装器）"""
    global BOT_LOOP
    
    try:
        if not BOT_LOOP:
            logger.error("机器人事件循环未初始化，无法处理webhook更新")
            print("ERROR: 机器人事件循环未初始化，无法处理webhook更新")
            return
        
        # 在机器人的事件循环中运行异步处理函数
        asyncio.run_coroutine_threadsafe(
            process_telegram_update_async(update_data, notification_queue),
            BOT_LOOP
        )
        
        logger.info("已将webhook更新提交到机器人事件循环处理")
        print("DEBUG: 已将webhook更新提交到机器人事件循环处理")
    
    except Exception as e:
        logger.error(f"提交webhook更新到事件循环时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 提交webhook更新到事件循环时出错: {str(e)}")

async def get_user_info(user_id):
    """获取Telegram用户信息并缓存"""
    global bot_application, user_info_cache
    
    if not bot_application:
        return {"id": user_id, "username": str(user_id), "first_name": str(user_id), "last_name": ""}
    
    # 检查缓存
    if user_id in user_info_cache:
        return user_info_cache[user_id]
    
    try:
        user = await bot_application.bot.get_chat(user_id)
        user_info = {
            "id": user_id,
            "username": user.username or str(user_id),
            "first_name": user.first_name or str(user_id),
            "last_name": user.last_name or ""
        }
        user_info_cache[user_id] = user_info
        return user_info
    except Exception as e:
        logger.error(f"Failed to get user info for {user_id}: {e}")
        default_info = {"id": user_id, "username": str(user_id), "first_name": str(user_id), "last_name": ""}
        user_info_cache[user_id] = default_info
        return default_info

# ===== TG 命令处理 =====
processing_accepts = set()
processing_accepts_time = {}  # 记录每个接单请求的开始时间

# 清理超时的处理中请求
async def cleanup_processing_accepts():
    """定期清理超时的处理中请求"""
    global processing_accepts, processing_accepts_time
    current_time = time.time()
    timeout_keys = []
    
    try:
        # 检查所有处理中的请求
        for key, start_time in list(processing_accepts_time.items()):
            # 如果请求处理时间超过30秒，认为超时
            if current_time - start_time > 30:
                timeout_keys.append(key)
        
        # 从集合中移除超时的请求
        for key in timeout_keys:
            if key in processing_accepts:
                processing_accepts.remove(key)
                logger.info(f"已清理超时的接单请求: {key}")
            if key in processing_accepts_time:
                del processing_accepts_time[key]
                
        # 检查是否有不一致的数据（在processing_accepts中但不在processing_accepts_time中）
        for key in list(processing_accepts):
            if key not in processing_accepts_time:
                processing_accepts.remove(key)
                logger.warning(f"清理了不一致的接单请求数据: {key}")
        
        # 日志记录当前处理中的请求数量
        if processing_accepts:
            logger.debug(f"当前有 {len(processing_accepts)} 个处理中的接单请求")
    except Exception as e:
        logger.error(f"清理超时的接单请求时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 清理超时的接单请求时出错: {str(e)}")

async def on_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """测试命令处理函数"""
    user_id = update.effective_user.id
    
    if not is_seller(user_id):
        await update.message.reply_text("⚠️ You do not have permission to use this command.")
        return
    
    await update.message.reply_text(
        "✅ Bot is running normally!\n\n"
        f"• Current Time: {get_china_time()}\n"
        f"• Your User ID: {user_id}\n"
        "• Bot Status: Online\n\n"
        "For help, use the /start command to see available functions."
    )
    logger.info(f"用户 {user_id} 执行了测试命令")

async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    user_id = update.effective_user.id
    username = update.effective_user.username
    first_name = update.effective_user.first_name
    
    logger.info(f"收到 /start 命令 用户: {user_id}, 用户名: {username}, 名字: {first_name}")
    print(f"DEBUG: 收到 /start 命令 用户: {user_id}, 用户名: {username}, 名字: {first_name}")
    
    if is_seller(user_id):
        # 更新卖家的活跃时间
        update_seller_last_active(user_id)
        
        # 获取卖家状态
        if DATABASE_URL.startswith('postgres'):
            result = execute_query(
                "SELECT is_active, desired_orders FROM sellers WHERE telegram_id = %s", 
                (str(user_id),), 
                fetch=True
            )
        else:
            result = execute_query(
                "SELECT is_active, desired_orders FROM sellers WHERE telegram_id = ?", 
                (str(user_id),), 
                fetch=True
            )
        
        is_active = "Active" if result and result[0][0] else "Inactive"
        desired_orders = result[0][1] if result and result[0][1] is not None else 0
        
        await update.message.reply_text(
            f"👋 Hello, {first_name}! You are a seller in our system.\n\n"
            f"🔹 Current status: {is_active}\n"
            f"🔹 Max order capacity: {desired_orders}\n\n"
            f"Available commands:\n"
            f"/seller - View available and active orders\n"
            f"/orders <number> - Set your maximum order capacity\n"
            f"/active - Toggle your active status\n"
            f"/test - Test bot status\n"
            f"/test_notify - Test notification feature"
        )
    else:
        await update.message.reply_text(
            f"👋 Hello, {first_name}!\n\n"
            "You are not a seller in our system. Please contact admin to be added as a seller."
        )

async def on_seller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /seller 命令，显示卖家信息、活动订单和可用订单"""
    user_id = update.effective_user.id
    if not is_seller(user_id):
        await update.message.reply_text("You don't have permission to use this command.")
        return
    
    # 获取卖家自己的活动订单
    active_orders = execute_query(
        "SELECT id, package, created_at FROM orders WHERE accepted_by = ? AND status = ?",
        (str(user_id), STATUS['ACCEPTED']),
        fetch=True
    )

    # 获取可用的新订单
    available_orders = execute_query(
        "SELECT id, package, created_at FROM orders WHERE status = ?",
        (STATUS['SUBMITTED'],),
                fetch=True
            )
            
    message = f"🌟 *Seller Console* 🌟\n\n*Hello, {update.effective_user.first_name}!*\n\n"

    if active_orders:
        message += "--- *Your Active Orders* ---\n"
        for order in active_orders:
            message += f"  - `Order #{order[0]}` ({order[1]} month), created at {order[2]}\n"
        message += "\n"
    else:
        message += "✅ You currently have no active orders.\n\n"

    if available_orders:
        message += "--- *Available New Orders* ---\n"
        for order in available_orders:
            message += f"  - `Order #{order[0]}` ({order[1]} month), created at {order[2]}\n"
    else:
        message += "📭 There are no new orders available.\n"

    await update.message.reply_text(message, parse_mode='Markdown')

# ====== 恢复 /orders 命令处理 ======
async def on_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle command to set maximum order capacity"""
    user_id = update.effective_user.id
    
    if not is_seller(user_id):
        await update.message.reply_text("You are not a seller, cannot use this command")
        return
    
    # 检查参数
    if not context.args or len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text(
            "Please provide your current maximum order capacity, for example:\n/orders 5"
        )
        return
    
    desired_orders = int(context.args[0])
    desired_orders = max(0, min(desired_orders, 20))  # 0~20 范围
    
    update_seller_desired_orders(user_id, desired_orders)
    update_seller_last_active(user_id)
    
    await update.message.reply_text(
        f"✅ Your maximum order capacity has been set to: {desired_orders}"
    )
    logger.info(f"卖家 {user_id} 设置最大接单数量为 {desired_orders}")

async def on_active_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle command to toggle seller active status"""
    user_id = update.effective_user.id
    
    if not is_seller(user_id):
        await update.message.reply_text("You are not a seller, cannot use this command")
        return
    
    # 获取当前卖家状态
    if DATABASE_URL.startswith('postgres'):
        result = execute_query(
            "SELECT is_active FROM sellers WHERE telegram_id = %s", 
            (str(user_id),), 
            fetch=True
        )
    else:
        result = execute_query(
            "SELECT is_active FROM sellers WHERE telegram_id = ?", 
            (str(user_id),), 
            fetch=True
        )
    
    if not result:
        await update.message.reply_text("Unable to get your seller status, please contact admin")
        return
    
    current_status = result[0][0]
    new_status = not bool(current_status)
    
    # 切换状态
    toggle_seller_status(str(user_id))
    
    # 更新最后活跃时间
    update_seller_last_active(user_id)
    
    status_text = "Active" if new_status else "Inactive"
    emoji = "✅" if new_status else "❌"
    
    await update.message.reply_text(
        f"{emoji} Your status has been changed to: {status_text}\n\n"
        f"{'You will now receive order notifications' if new_status else 'You will no longer receive order notifications'}"
    )
    logger.info(f"卖家 {user_id} 将状态切换为: {status_text}")

# ===== 主函数 =====
def run_bot(queue):
    """在单独的线程中运行机器人"""
    global BOT_LOOP
    global bot_application
    global notified_orders_lock
    global notified_orders
    global notification_queue
    
    # 初始化锁和集合
    notified_orders_lock = threading.Lock()
    notified_orders = set()
    globals()['notification_queue'] = queue  # 设置全局变量
    
    try:
        # 创建事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        BOT_LOOP = loop
        
        # 运行机器人
        loop.run_until_complete(bot_main(queue))
    except Exception as e:
        logger.critical(f"运行机器人时发生严重错误: {str(e)}", exc_info=True)
        print(f"CRITICAL: 运行机器人时发生严重错误: {str(e)}")

async def bot_main(queue):
    """机器人的主异步函数"""
    global bot_application
    
    logger.info("正在启动Telegram机器人...")
    print("DEBUG: 正在启动Telegram机器人...")
    
    try:
        # 初始化，增加连接池大小和超时设置
        bot_application = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .connection_pool_size(16)
            .connect_timeout(30.0)
            .read_timeout(30.0)
            .write_timeout(30.0)
            .pool_timeout(30.0)
            .build()
        )
        
        logger.info("Telegram机器人应用已构建")
        print("DEBUG: Telegram机器人应用已构建")
        print(f"DEBUG: 使用的BOT_TOKEN: {BOT_TOKEN[:5]}...{BOT_TOKEN[-5:]}")
        
        # 添加处理程序
        bot_application.add_handler(CommandHandler("start", on_start))
        bot_application.add_handler(CommandHandler("seller", on_seller_command))
        bot_application.add_handler(CommandHandler("orders", on_orders))  # 添加新命令
        bot_application.add_handler(CommandHandler("active", on_active_command))  # 添加活跃状态切换命令
        
        # 添加测试命令处理程序
        bot_application.add_handler(CommandHandler("test", on_test))
        bot_application.add_handler(CommandHandler("test_notify", on_test_notify))  # 添加测试通知命令
        print("DEBUG: 已添加测试命令处理程序")
        
        # 添加通用回调处理程序，处理所有回调查询
        recharge_handler = CallbackQueryHandler(on_callback_query)
        bot_application.add_handler(recharge_handler)
        print(f"DEBUG: 已添加通用回调处理程序: {recharge_handler}")
        
        # 添加文本消息处理程序
        bot_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        print("DEBUG: 已添加文本消息处理程序")
        
        logger.info("已添加所有处理程序")
        print("DEBUG: 已添加所有处理程序")
        
        # 添加错误处理程序
        bot_application.add_error_handler(error_handler)

        # 初始化应用
        logger.info("初始化Telegram应用...")
        await bot_application.initialize()
        
        # 获取Railway应用URL
        railway_url = os.environ.get('RAILWAY_STATIC_URL')
        if not railway_url:
            railway_url = os.environ.get('RAILWAY_PUBLIC_DOMAIN')
            if railway_url:
                railway_url = f"https://{railway_url}"
        
        # 总是尝试设置 Webhook，因为我们是在 Web 应用中运行
        if railway_url:
            webhook_url = f"{railway_url}/telegram-webhook"
            logger.info(f"设置 Telegram webhook: {webhook_url}")
            print(f"DEBUG: 设置 Telegram webhook: {webhook_url}")
            await bot_application.bot.set_webhook(
                url=webhook_url,
                allowed_updates=Update.ALL_TYPES
            )
        else:
            logger.warning("无法获取公开URL，未设置webhook。机器人可能无法接收更新。")

        # 启动后台任务
        logger.info("启动后台任务...")
        asyncio.create_task(periodic_order_check())
        asyncio.create_task(process_notification_queue(queue))
        
        logger.info("Telegram机器人主循环已启动，等待更新...")
        print("DEBUG: Telegram机器人主循环已启动，等待更新...")
        
        # 保持此协程运行以使后台任务可以执行
        while True:
            await asyncio.sleep(3600) # 每小时唤醒一次，但主要目的是保持运行

    except Exception as e:
        logger.critical(f"Telegram机器人主函数 `bot_main` 发生严重错误: {str(e)}", exc_info=True)
        print(f"CRITICAL: Telegram机器人主函数 `bot_main` 发生严重错误: {str(e)}")

# 添加错误处理函数
async def error_handler(update, context):
    """处理Telegram机器人的错误"""
    logger.error(f"Telegram机器人发生错误: {context.error}", exc_info=context.error)
    print(f"ERROR: Telegram机器人发生错误: {context.error}")
    
    # 尝试获取错误来源
    if update:
        if update.effective_message:
            logger.error(f"错误发生在消息: {update.effective_message.text}")
            print(f"ERROR: 错误发生在消息: {update.effective_message.text}")
        elif update.callback_query:
            logger.error(f"错误发生在回调查询: {update.callback_query.data}")
            print(f"ERROR: 错误发生在回调查询: {update.callback_query.data}")
    
    # 如果是回调查询错误，尝试回复用户
    try:
        if update and update.callback_query:
            await update.callback_query.answer("An error occurred. Please try again later.", show_alert=True)
    except Exception as e:
        logger.error(f"尝试回复错误通知失败: {str(e)}")
        print(f"ERROR: 尝试回复错误通知失败: {str(e)}")

async def periodic_order_check():
    """定期检查新订单的任务"""
    check_count = 0
    while True:
        try:
            logger.debug(f"执行第 {check_count + 1} 次订单检查")
            await check_and_push_orders()
            await cleanup_processing_accepts()
            check_count += 1
        except Exception as e:
            logger.error(f"订单检查任务出错: {e}", exc_info=True)
        
        await asyncio.sleep(5) # 每5秒检查一次

async def process_notification_queue(queue):
    """处理来自Flask的通知队列"""
    loop = asyncio.get_running_loop()
    while True:
        try:
            # 在执行器中运行阻塞的 queue.get()，这样不会阻塞事件循环
            data = await loop.run_in_executor(None, queue.get)
            logger.info(f"从队列中获取到通知任务: {data.get('type')}, 数据: {data}")
            
            # 确保调用send_notification_from_queue并等待其完成
            await send_notification_from_queue(data)
            
            # 标记任务完成
            queue.task_done()
            logger.info(f"通知任务 {data.get('type')} 处理完成")
        except asyncio.CancelledError:
            logger.info("通知队列处理器被取消。")
            break
        except Exception as e:
            # 捕获并记录所有其他异常
            logger.error(f"处理通知队列任务时发生未知错误: {repr(e)}", exc_info=True)
            # 等待一会避免在持续出错时刷屏
            await asyncio.sleep(5)
    
async def send_notification_from_queue(data):
    """处理通知队列中的订单通知"""
    try:
        logger.info(f"开始处理通知: {data.get('type')}")
        print(f"DEBUG: 开始处理通知: {data.get('type')}")
        
        if data.get('type') == 'new_order':
            # 获取订单数据
            order_id = data.get('order_id')
            account = data.get('account')  # 这是二维码图片路径
            remark = data.get('remark', '')  # 获取备注信息
            preferred_seller = data.get('preferred_seller')
            
            # 检查订单是否存在
            order = get_order_by_id(order_id)
            if not order:
                logger.error(f"通知失败，找不到订单: {order_id}")
                return
                
            # 检查订单是否已经被接单，避免重复发送通知
            if order.get('accepted_by'):
                logger.warning(f"订单 {order_id} 已经被卖家 {order.get('accepted_by')} 接单，不再发送通知")
                return
            
            # 获取活跃卖家列表
            active_sellers = get_active_sellers()
            logger.info(f"获取到活跃卖家列表: {active_sellers}")
            print(f"DEBUG: 获取到活跃卖家列表: {active_sellers}")
            
            if not active_sellers:
                logger.warning(f"没有活跃的卖家可以接收订单通知: {order_id}")
                print(f"WARNING: 没有活跃的卖家可以接收订单通知: {order_id}")
                return
                
            image_path = account # 路径现在是相对的
            
            # 尝试不同的路径格式
            image_paths_to_try = [
                image_path,  # 原始路径
                image_path.replace('/', '\\'),  # Windows 风格路径
                os.path.join(os.getcwd(), image_path),  # 绝对路径
                os.path.join(os.getcwd(), image_path.replace('/', '\\')),  # 绝对 Windows 路径
            ]
            
            logger.info(f"将尝试以下图片路径:")
            for idx, path in enumerate(image_paths_to_try):
                logger.info(f"  路径 {idx+1}: {path} (存在: {os.path.exists(path)})")
                print(f"DEBUG: 尝试路径 {idx+1}: {path} (存在: {os.path.exists(path)})")
                
            # 找到第一个存在的路径
            valid_path = None
            for path in image_paths_to_try:
                if os.path.exists(path):
                    valid_path = path
                    logger.info(f"找到有效的图片路径: {valid_path}")
                    print(f"DEBUG: 找到有效的图片路径: {valid_path}")
                    break
                    
            if valid_path:
                image_path = valid_path
            else:
                logger.error(f"所有尝试的图片路径都不存在")
                print(f"ERROR: 所有尝试的图片路径都不存在")
                
            logger.info(f"将发送图片: {image_path}")
            print(f"DEBUG: 将发送图片: {image_path}")
            
            # 检查图片是否存在
            if not os.path.exists(image_path):
                logger.error(f"图片文件不存在: {image_path}")
                print(f"ERROR: 图片文件不存在: {image_path}")
                # 尝试列出目录内容
                try:
                    dir_path = os.path.dirname(image_path)
                    if os.path.exists(dir_path):
                        files = os.listdir(dir_path)
                        logger.info(f"目录 {dir_path} 中的文件: {files}")
                        print(f"DEBUG: 目录 {dir_path} 中的文件: {files}")
                    else:
                        logger.error(f"目录不存在: {dir_path}")
                        print(f"ERROR: 目录不存在: {dir_path}")
                except Exception as e:
                    logger.error(f"列出目录内容时出错: {str(e)}")
                    print(f"ERROR: 列出目录内容时出错: {str(e)}")
                return
                
            # 确定目标卖家（从活跃卖家中选择一个）
            if preferred_seller:
                # 如果指定了特定卖家，检查该卖家是否活跃
                target_sellers = [seller for seller in active_sellers if str(seller.get('id', seller.get('telegram_id'))) == str(preferred_seller)]
                if not target_sellers:
                    logger.warning(f"指定的卖家不存在或不活跃: {preferred_seller}，将选择一位活跃卖家")
                    # 选择一位活跃卖家
                    selected_seller_id = select_active_seller()
                    if selected_seller_id:
                        target_sellers = [seller for seller in active_sellers if str(seller.get('id', seller.get('telegram_id'))) == str(selected_seller_id)]
            else:
                        # 如果没有选出合适的卖家，使用所有活跃卖家
                        logger.warning(f"未能选择合适的卖家，使用随机卖家")
                        import random
                        if active_sellers:
                            random_seller = random.choice(active_sellers)
                            target_sellers = [random_seller]
                        else:
                            logger.error("没有活跃卖家可用")
                            return
                
            # 为订单添加状态标记
            await mark_order_as_processing(order_id)
            
            # 发送通知给选中的卖家
            if target_sellers:
                seller = target_sellers[0]
                seller_id = seller.get('id', seller.get('telegram_id'))
                try:
                    # 使用备注作为标题，如果没有备注则显示订单号
                    caption = f"*{remark}*" if remark else f"订单 #{order_id}"
                    
                    # 创建按钮
                    keyboard = [
                        [InlineKeyboardButton("✅ Complete", callback_data=f"done_{order_id}"),
                         InlineKeyboardButton("❓ Any Problem", callback_data=f"fail_{order_id}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    # 发送图片和备注
                    await bot_application.bot.send_photo(
                        chat_id=seller_id,
                        photo=open(image_path, 'rb'),
                        caption=caption,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                    logger.info(f"已发送订单 #{order_id} 通知到卖家 {seller_id}")
                    
                    # 自动接单（标记该订单已被该卖家接受）
                    await auto_accept_order(order_id, seller_id)
                    
                except Exception as e:
                    logger.error(f"向卖家 {seller_id} 发送订单通知时出错: {str(e)}", exc_info=True)
            else:
                logger.error(f"没有选中任何卖家来处理订单 {order_id}")
                    
    except Exception as e:
        logger.error(f"处理通知数据失败: {str(e)}", exc_info=True)
        print(f"ERROR: 处理通知数据失败: {str(e)}")

async def mark_order_as_processing(order_id):
    """标记订单为处理中状态"""
    try:
        # 更新订单状态为处理中
        execute_query(
            "UPDATE orders SET status=? WHERE id=? AND status=?",
            (STATUS['SUBMITTED'], order_id, STATUS['SUBMITTED'])
        )
        logger.info(f"已标记订单 #{order_id} 为处理中状态")
    except Exception as e:
        logger.error(f"标记订单 #{order_id} 状态时出错: {str(e)}")

async def auto_accept_order(order_id, seller_id):
    """自动接单处理"""
    try:
        # 获取卖家信息
        # 优先使用数据库中的卖家信息，包括管理员设置的昵称
        seller_info = get_seller_info(seller_id)
        
        if seller_info:
            # 使用从数据库获取的信息，包含管理员设置的昵称
            username = seller_info.get('username', '')
            first_name = seller_info.get('first_name', '')
            nickname = seller_info.get('nickname', '')
            display_name = seller_info.get('display_name', '')  # 优先使用昵称
        else:
            # 作为备用，使用Telegram API获取卖家信息
            user_info = await get_user_info(seller_id)
            username = user_info.get('username', '')
            first_name = user_info.get('first_name', '')
            nickname = ''
            display_name = first_name or username or str(seller_id)
        
        # 更新订单为已接受状态
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        execute_query(
            "UPDATE orders SET status=?, accepted_by=?, accepted_at=?, accepted_by_username=?, accepted_by_first_name=?, accepted_by_nickname=? WHERE id=?",
            (STATUS['ACCEPTED'], str(seller_id), timestamp, username, first_name, nickname, order_id)
        )
        logger.info(f"卖家 {display_name} ({seller_id}) 已自动接受订单 #{order_id}")
    except Exception as e:
        logger.error(f"自动接单过程中出错: {str(e)}")
    
def run_bot_in_thread():
    """在单独的线程中运行机器人"""
    # 这个函数现在可以被废弃或重构，因为启动逻辑已移至app.py
    logger.warning("run_bot_in_thread 已被调用，但可能已废弃。")
    pass

def restricted(func):
    """限制只有卖家才能访问的装饰器"""
    async def wrapped(update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_seller(user_id):
            logger.warning(f"未经授权的访问: {user_id}")
            await update.message.reply_text("Sorry, you are not authorized to use this bot.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped 

def get_order_by_id(order_id):
    """根据ID获取订单信息"""
    try:
        conn = get_db_connection()
        if not conn:
            logger.error(f"获取订单 {order_id} 信息时无法获取数据库连接")
            print(f"ERROR: 获取订单 {order_id} 信息时无法获取数据库连接")
            return None
            
        cursor = conn.cursor()
        
        # 根据数据库类型执行不同的查询
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL使用%s作为占位符
            cursor.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
            order = cursor.fetchone()
            
            if order:
                # 将结果转换为字典
                columns = [desc[0] for desc in cursor.description]
                result = {columns[i]: order[i] for i in range(len(columns))}
                conn.close()
                return result
        else:
            # SQLite
            cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
            order = cursor.fetchone()
            
            if order:
                # 将结果转换为字典
                columns = [column[0] for column in cursor.description]
                result = {columns[i]: order[i] for i in range(len(columns))}
                conn.close()
                return result
                
        conn.close()
        return None
    except Exception as e:
        logger.error(f"获取订单 {order_id} 信息时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 获取订单 {order_id} 信息时出错: {str(e)}")
        return None

def check_order_exists(order_id):
    """检查数据库中是否存在指定ID的订单"""
    try:
        conn = get_db_connection()
        if not conn:
            logger.error(f"检查订单 {order_id} 存在性时无法获取数据库连接")
            print(f"ERROR: 检查订单 {order_id} 存在性时无法获取数据库连接")
            return False
            
        cursor = conn.cursor()
        logger.info(f"正在检查订单ID={order_id}是否存在...")
        print(f"DEBUG: 正在检查订单ID={order_id}是否存在...")
        
        # 根据数据库类型执行不同的查询
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL使用%s作为占位符
            cursor.execute("SELECT COUNT(*) FROM orders WHERE id = %s", (order_id,))
        else:
            # SQLite
            cursor.execute("SELECT COUNT(*) FROM orders WHERE id = ?", (order_id,))
            
        count = cursor.fetchone()[0]
        
        # 增加更多查询记录debug问题
        if count == 0:
            logger.warning(f"订单 {order_id} 在数据库中不存在")
            print(f"WARNING: 订单 {order_id} 在数据库中不存在")
            
            # 检查是否有任何订单
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("SELECT COUNT(*) FROM orders")
            else:
                cursor.execute("SELECT COUNT(*) FROM orders")
                
            total_count = cursor.fetchone()[0]
            logger.info(f"数据库中总共有 {total_count} 个订单")
            print(f"INFO: 数据库中总共有 {total_count} 个订单")
            
            # 列出最近的几个订单ID
            if DATABASE_URL.startswith('postgres'):
                cursor.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 5")
            else:
                cursor.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 5")
                
            recent_orders = cursor.fetchall()
            if recent_orders:
                recent_ids = [str(order[0]) for order in recent_orders]
                logger.info(f"最近的订单ID: {', '.join(recent_ids)}")
                print(f"INFO: 最近的订单ID: {', '.join(recent_ids)}")
        else:
            logger.info(f"订单 {order_id} 存在于数据库中")
            print(f"DEBUG: 订单 {order_id} 存在于数据库中")
            
        conn.close()
        return count > 0
    except Exception as e:
        logger.error(f"检查订单 {order_id} 是否存在时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 检查订单 {order_id} 是否存在时出错: {str(e)}")
        return False

def update_order_status(order_id, status, handler_id=None):
    """更新订单状态"""
    try:
        conn = get_db_connection()
        if not conn:
            logger.error(f"更新订单 {order_id} 状态时无法获取数据库连接")
            print(f"ERROR: 更新订单 {order_id} 状态时无法获取数据库连接")
            return False
            
        cursor = conn.cursor()
        
        # 根据数据库类型执行不同的查询
        if DATABASE_URL.startswith('postgres'):
            # PostgreSQL使用%s作为占位符，并且时间戳函数不同
            if handler_id:
                cursor.execute(
                    "UPDATE orders SET status = %s, handler_id = %s, updated_at = NOW() WHERE id = %s",
                    (status, handler_id, order_id)
                )
            else:
                cursor.execute(
                    "UPDATE orders SET status = %s, updated_at = NOW() WHERE id = %s",
                    (status, order_id)
                )
        else:
            # SQLite
            if handler_id:
                cursor.execute(
                    "UPDATE orders SET status = ?, handler_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (status, handler_id, order_id)
                )
            else:
                cursor.execute(
                    "UPDATE orders SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (status, order_id)
                )
        
        conn.commit()
        conn.close()
        
        logger.info(f"已更新订单 {order_id} 状态为 {status}")
        print(f"INFO: 已更新订单 {order_id} 状态为 {status}")
        return True
    except Exception as e:
        logger.error(f"更新订单 {order_id} 状态时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 更新订单 {order_id} 状态时出错: {str(e)}")
        return False 

@callback_error_handler
async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理回调查询，主要用于处理订单按钮的点击"""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    logger.info(f"收到回调查询：{data}，用户：{user_id}")
    
    # 首先验证是否为卖家
    if not is_seller(user_id):
        await query.answer("You don't have permission to use this feature", show_alert=True)
        return

    # 如果是简单的接收订单操作
    if data.startswith("accept_"):
        oid = int(data.split("_")[1])
            
        # 首先检查订单是否存在
        order = get_order_by_id(oid)
        if not order:
            await query.answer("Order does not exist", show_alert=True)
            return
        
        # 检查订单是否已被接单
        if order.get('status') != STATUS['SUBMITTED']:
            await query.answer("This order has already been accepted", show_alert=True)
            return
        
        # 接单处理逻辑
        try:
            global processing_accepts, processing_accepts_time
            
            # 防止重复提交
            accept_key = f"{oid}_{user_id}"
            if accept_key in processing_accepts:
                logger.info(f"已在处理 {user_id} 对订单 {oid} 的接单请求，忽略重复请求")
                return
            
            # 标记为处理中
            processing_accepts.add(accept_key)
            processing_accepts_time[accept_key] = time.time()
            
            # 接单处理
            timestamp = get_china_time()
            conn = get_db_connection()
            cursor = conn.cursor()
            if DATABASE_URL.startswith('postgres'):
                cursor.execute(
                    "UPDATE orders SET status=%s, accepted_by=%s, accepted_at=%s, accepted_by_username=%s WHERE id=%s AND status=%s",
                    (STATUS['ACCEPTED'], str(user_id), timestamp, query.from_user.username, oid, STATUS['SUBMITTED'])
                )
            else:
                cursor.execute(
                    "UPDATE orders SET status=?, accepted_by=?, accepted_at=?, accepted_by_username=? WHERE id=? AND status=?",
                    (STATUS['ACCEPTED'], str(user_id), timestamp, query.from_user.username, oid, STATUS['SUBMITTED'])
                )
            affected = cursor.rowcount
            conn.commit()
            conn.close()
            
            # 处理完成，移除标记
            processing_accepts.discard(accept_key)
            if accept_key in processing_accepts_time:
                del processing_accepts_time[accept_key]
            
            if affected > 0:
                # 推送通知给网页端
                if notification_queue:
                    notification_queue.put({
                        'type': 'order_status_change',
                        'order_id': oid,
                        'status': STATUS['ACCEPTED'],
                        'handler_id': user_id
                    })
                await query.answer("Order accepted", show_alert=True)
                logger.info(f"卖家 {user_id} 接受了订单 {oid}")
            else:
                await query.answer("Failed to accept order, please try again later", show_alert=True)
                logger.warning(f"卖家 {user_id} 接单 {oid} 失败，可能已被其他卖家接单")
        except Exception as e:
            logger.error(f"处理订单接受(accept_)时出错: {str(e)}", exc_info=True)
            
            # 发生错误时清理处理标记
            accept_key = f"{oid}_{user_id}"
            processing_accepts.discard(accept_key)
            if accept_key in processing_accepts_time:
                del processing_accepts_time[accept_key]
                
            await query.answer("Invalid feedback data", show_alert=True)
    
    elif data.startswith("feedback_"):
        parts = data.split("_")
        if len(parts) >= 3:
            oid = int(parts[1])
            action = parts[2]
            
            try:
                if action == "done":
                    # 处理完成反馈
                    feedback_waiting[oid] = {"user_id": user_id, "action": "done"}
                elif action == "fail":
                    # 处理失败反馈
                    feedback_waiting[oid] = {"user_id": user_id, "action": "fail"}
                else:
                    await query.answer("Unknown feedback action", show_alert=True)
            except Exception as e:
                logger.error(f"处理反馈时出错: {str(e)}", exc_info=True)
                await query.answer("Failed to process feedback, please try again later", show_alert=True)
    
    elif data.startswith("done_"):
        oid = int(data.split('_')[1])
        try:
            # 验证订单存在
            order = get_order_by_id(oid)
            if not order:
                await query.answer("Order does not exist", show_alert=True)
                return
                
            # 更新订单状态
            timestamp = get_china_time()
            conn = get_db_connection()
            cursor = conn.cursor()
            if DATABASE_URL.startswith('postgres'):
                cursor.execute(
                    "UPDATE orders SET status=%s, completed_at=%s WHERE id=%s",
                    (STATUS['COMPLETED'], timestamp, oid)
                )
            else:
                cursor.execute(
                    "UPDATE orders SET status=?, completed_at=? WHERE id=?",
                    (STATUS['COMPLETED'], timestamp, oid)
                )
            conn.commit()
            conn.close()

            # 推送通知给网页端
            if notification_queue:
                notification_queue.put({
                    'type': 'order_status_change',
                    'order_id': oid,
                    'status': STATUS['COMPLETED'],
                    'handler_id': user_id
                })
                logger.info(f"已将订单 #{oid} 状态变更(完成)添加到通知队列")

            # 更新按钮显示
            keyboard = [[InlineKeyboardButton("✅ Completed", callback_data="noop")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=reply_markup)
            await query.answer("Order marked as completed", show_alert=True)
        except Exception as e:
            logger.error(f"处理订单完成(done_)时出错: {str(e)}", exc_info=True)
            await query.answer("Error processing order, please try again later", show_alert=True)
        return
    
    elif data.startswith("problem_"):
        parts = data.split("_")
        if len(parts) < 3:
            await query.answer("Please select a problem type", show_alert=True)
            return

        oid = int(parts[1])
        problem_type = parts[2]

        try:
            # 更新订单状态
            reason = {
                "password": "Wrong password",
                "expired": "Membership not expired",
                "other": "Other reason"
            }.get(problem_type, "Other reason (details pending)")

            timestamp = get_china_time()
            conn = get_db_connection()
            cursor = conn.cursor()
            if DATABASE_URL.startswith('postgres'):
                cursor.execute(
                    "UPDATE orders SET status=%s, failed_at=%s, fail_reason=%s WHERE id=%s",
                    (STATUS['FAILED'], timestamp, reason, oid)
                )
            else:
                cursor.execute(
                    "UPDATE orders SET status=?, failed_at=?, fail_reason=? WHERE id=?",
                    (STATUS['FAILED'], timestamp, reason, oid)
                )
            conn.commit()
            conn.close()
            
            # 推送通知给网页端
            if notification_queue:
                notification_queue.put({
                    'type': 'order_status_change',
                    'order_id': oid,
                    'status': STATUS['FAILED'],
                    'reason': reason,
                    'handler_id': user_id
                })
                logger.info(f"已将订单 #{oid} 状态变更(问题反馈)添加到通知队列")
            
            # 更新按钮显示
            keyboard = [[InlineKeyboardButton("❌ Failed", callback_data="noop")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=reply_markup)
            await query.answer(f"Reported issue: {reason}", show_alert=True)
        except Exception as e:
            logger.error(f"处理订单问题(problem_)时出错: {str(e)}", exc_info=True)
            await query.answer("Error processing order, please try again later", show_alert=True)
        return
    
    elif data.startswith("fail_"):
        # 显示问题选择按钮
        oid = data.split("_")[1]
        
        keyboard = [
            [InlineKeyboardButton("Wrong Password", callback_data=f"problem_{oid}_password")],
            [InlineKeyboardButton("Membership Not Expired", callback_data=f"problem_{oid}_expired")],
            [InlineKeyboardButton("Other Issue", callback_data=f"problem_{oid}_other")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.edit_message_reply_markup(reply_markup=reply_markup)
            await query.answer()
        except Exception as e:
            logger.error(f"显示问题选择按钮时出错: {str(e)}", exc_info=True)
            await query.answer("Error displaying options, please try again later", show_alert=True)
        return
        
    elif data.startswith("fail2_"):
        oid = int(data.split('_')[1])
        try:
            # 更新订单状态
            timestamp = get_china_time()
            conn = get_db_connection()
            cursor = conn.cursor()
            if DATABASE_URL.startswith('postgres'):
                cursor.execute(
                    "UPDATE orders SET status=%s, failed_at=%s WHERE id=%s",
                    (STATUS['FAILED'], timestamp, oid)
                )
            else:
                cursor.execute(
                    "UPDATE orders SET status=?, failed_at=? WHERE id=?",
                    (STATUS['FAILED'], timestamp, oid)
                )
            conn.commit()
            conn.close()

            # 推送通知给网页端
            if notification_queue:
                notification_queue.put({
                    'type': 'order_status_change',
                    'order_id': oid,
                    'status': STATUS['FAILED'],
                    'handler_id': user_id
                })
                logger.info(f"已将订单 #{oid} 状态变更(失败)添加到通知队列")

            # 更新按钮显示
            keyboard = [[InlineKeyboardButton("❌ Failed", callback_data="noop")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=reply_markup)
            await query.answer("Order marked as failed", show_alert=True)
        except Exception as e:
            logger.error(f"处理订单失败(fail_)时出错: {str(e)}", exc_info=True)
            await query.answer("Error processing order, please try again later", show_alert=True)
        return
    elif data == "activity_confirm":
        # 更新卖家最后活跃时间
        update_seller_last_active(user_id)
        
        # 回复确认
        await query.answer("Thank you for confirming, your online status has been updated", show_alert=True)
        
        # 更新消息，移除按钮
        await query.edit_message_text(
            text=f"✅ *Activity confirmation successful*\n\nYou have confirmed your online status.\n\n⏰ Confirmation time: {get_china_time()}",
            parse_mode='Markdown'
        )
        
        logger.info(f"卖家 {user_id} 已确认活跃状态")
        return
    elif data.startswith("complete_"):
        oid = int(data.split('_')[1])

        # 与 done_ 分支相同的处理逻辑
        try:
            timestamp = get_china_time()
            conn = get_db_connection()
            cursor = conn.cursor()
            if DATABASE_URL.startswith('postgres'):
                cursor.execute(
                    "UPDATE orders SET status=%s, completed_at=%s WHERE id=%s",
                    (STATUS['COMPLETED'], timestamp, oid)
                )
            else:
                cursor.execute(
                    "UPDATE orders SET status=?, completed_at=? WHERE id=?",
                    (STATUS['COMPLETED'], timestamp, oid)
                )
            conn.commit()
            conn.close()

            # 推送通知给网页端
            if notification_queue:
                notification_queue.put({
                    'type': 'order_status_change',
                    'order_id': oid,
                    'status': STATUS['COMPLETED'],
                    'handler_id': user_id
                })
                logger.info(f"已将订单 #{oid} 状态变更(完成)添加到通知队列 (complete_)")

            # 更新按钮显示
            keyboard = [[InlineKeyboardButton("✅ Completed", callback_data="noop")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=reply_markup)
            await query.answer("Order marked as completed", show_alert=True)
            logger.info(f"用户 {user_id} 已将订单 {oid} 标记为完成 (complete_)")
        except Exception as e:
            logger.error(f"处理订单完成(complete_)时出错: {str(e)}", exc_info=True)
            await query.answer("Error processing order, please try again later", show_alert=True)
        return
    else:
        await query.answer("Unknown command")

# ====== 自动修复：添加测试通知命令处理函数 ======
async def on_test_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """测试通知命令处理函数"""
    user_id = update.effective_user.id
    
    if not is_seller(user_id):
        await update.message.reply_text("⚠️ You don't have permission to use this command.")
        return
        
    try:
        await update.message.reply_text("Testing notification feature, will send test notification...")
        
        # 创建测试数据
        test_image_path = "static/uploads/test_notify.png"
        
        # 创建一个简单的测试图片
        try:
            from PIL import Image, ImageDraw, ImageFont
            import random
            
            # 创建一个白色背景图片
            img = Image.new('RGB', (300, 300), color=(255, 255, 255))
            d = ImageDraw.Draw(img)
            
            # 添加一些随机彩色矩形
            for i in range(10):
                x1 = random.randint(0, 250)
                y1 = random.randint(0, 250)
                x2 = x1 + random.randint(10, 50)
                y2 = y1 + random.randint(10, 50)
                color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
                d.rectangle([x1, y1, x2, y2], fill=color)
            
            # 添加文本
            d.text((10, 10), f"Test notification {time.time()}", fill=(0, 0, 0))
            
            # 确保目录存在
            os.makedirs(os.path.dirname(test_image_path), exist_ok=True)
            
            # 保存图片
            img.save(test_image_path)
            logger.info(f"已创建测试图片: {test_image_path}")
        except Exception as e:
            logger.error(f"创建测试图片失败: {str(e)}")
            test_image_path = None
        
        if test_image_path and os.path.exists(test_image_path):
            # 发送测试通知
            await send_notification_from_queue({
                'type': 'new_order',
                'order_id': 999999,  # 测试订单ID
                'account': test_image_path,
                'remark': 'This is a test notification',
                'preferred_seller': str(user_id)  # 只发给测试的卖家
            })
            await update.message.reply_text("Test notification sent, please check if you received it")
        else:
            await update.message.reply_text("Failed to create test image, cannot send test notification")
    except Exception as e:
        logger.error(f"发送测试通知失败: {str(e)}", exc_info=True)
        await update.message.reply_text(f"Failed to send test notification: {str(e)}")

# ====== 自动修复：添加缺失的check_and_push_orders函数 ======
async def check_and_push_orders():
    """检查新订单并推送通知"""
    try:
        # 导入必要的函数
        from modules.database import get_unnotified_orders
        
        # 获取未通知的订单
        unnotified_orders = get_unnotified_orders()
        
        if unnotified_orders:
            logger.info(f"发现 {len(unnotified_orders)} 个未通知的订单")
            print(f"DEBUG: 发现 {len(unnotified_orders)} 个未通知的订单")
            
            # 处理每个未通知的订单
            for order in unnotified_orders:
                # 注意：order是一个元组，不是字典
                # 根据get_unnotified_orders的SQL查询，元素顺序为:
                # id, account, password, package, created_at, web_user_id, remark
                order_id = order[0]
                account = order[1]  # 图片路径
                remark = order[6] if len(order) > 6 else ""
                
                # 使用全局通知队列
                global notification_queue
                if notification_queue:
                    # 添加到通知队列
                    notification_queue.put({
                        'type': 'new_order',
                        'order_id': order_id,
                        'account': account,
                        'remark': remark,
                        'preferred_seller': None  # 不指定特定卖家
                    })
                    logger.info(f"已将订单 #{order_id} 添加到通知队列")
                    print(f"DEBUG: 已将订单 #{order_id} 添加到通知队列")
                else:
                    logger.error("通知队列未初始化")
                    print("ERROR: 通知队列未初始化")
        else:
            logger.debug("没有发现未通知的订单")
    except Exception as e:
        logger.error(f"检查未通知订单时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 检查未通知订单时出错: {str(e)}")

# ====== 添加文本消息处理函数 ======
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process regular text messages"""
    user_id = update.effective_user.id
    text = update.message.text
    
    # 记录接收到的消息
    logger.info(f"收到来自用户 {user_id} 的文本消息: {text}")
    print(f"DEBUG: 收到来自用户 {user_id} 的文本消息: {text}")
    
    # 如果是卖家，可以提供一些帮助信息
    if is_seller(user_id):
        # 只回复第一条消息，避免重复打扰
        if not hasattr(context.user_data, 'welcomed'):
            await update.message.reply_text(
                "👋 Hello! To use the bot features, please use the following commands:\n"
                "/seller - View available and active orders\n"
                "/orders <number> - Set your maximum order capacity\n"
                "/active - Toggle your active status\n"
                "/test - Test bot status\n"
                "/test_notify - Test notification feature"
            )
            context.user_data['welcomed'] = True