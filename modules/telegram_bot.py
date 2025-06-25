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
import traceback
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
    BOT_TOKEN, STATUS, STATUS_TEXT_EN,
    user_languages, feedback_waiting, notified_orders, notified_orders_lock, DATABASE_URL
)
from modules.database import (
    get_order_details, execute_query, get_unnotified_orders, get_active_seller_ids,
    update_order_status, set_order_notified, is_admin_seller
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

# 获取中国时间的函数
def get_china_time():
    """获取当前中国时间（UTC+8）"""
    utc_now = datetime.now(pytz.utc)
    china_now = utc_now.astimezone(CN_TIMEZONE)
    return china_now.strftime("%Y-%m-%d %H:%M:%S")

# ===== 全局变量 =====
bot_application = None
BOT_LOOP = None

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
            
            error_msg = f"Callback error [{func.__name__}] "
            if user_id:
                error_msg += f"User ID: {user_id} "
            error_msg += f"Error: {str(e)}"
            
            logger.error(error_msg, exc_info=True)
            
            # 尝试通知用户
            try:
                if update.callback_query:
                    await update.callback_query.answer("Operation failed, please try again later", show_alert=True)
            except Exception as notify_err:
                logger.error(f"Failed to notify user of error: {str(notify_err)}")
            
            return None
    return wrapper

# 添加处理 Telegram webhook 更新的函数
async def process_telegram_update_async(update_data, notification_queue):
    """异步处理来自Telegram webhook的更新"""
    global bot_application
    
    try:
        if not bot_application:
            logger.error("Bot application not initialized, unable to process webhook update")
            return
        
        # 将JSON数据转换为Update对象
        update = Update.de_json(data=update_data, bot=bot_application.bot)
        
        if not update:
            logger.error("Unable to convert webhook data to Update object")
            return
        
        # 处理更新
        logger.info(f"Processing webhook update: {update.update_id}")
        
        # 将更新分派给应用程序处理
        await bot_application.process_update(update)
        
        logger.info(f"Webhook update {update.update_id} processed")
    
    except Exception as e:
        logger.error(f"Error processing webhook update: {str(e)}", exc_info=True)

def process_telegram_update(update_data, notification_queue):
    """处理来自Telegram webhook的更新（同步包装器）"""
    global BOT_LOOP
    
    try:
        if not BOT_LOOP:
            logger.error("Bot event loop not initialized, unable to process webhook update")
            return
        
        # 在机器人的事件循环中运行异步处理函数
        asyncio.run_coroutine_threadsafe(
            process_telegram_update_async(update_data, notification_queue),
            BOT_LOOP
        )
        
        logger.info("Webhook update submitted to bot event loop for processing")
    
    except Exception as e:
        logger.error(f"Error submitting webhook update to event loop: {str(e)}", exc_info=True)

# ===== 机器人命令处理函数 =====
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    is_seller = chat_id in get_active_seller_ids()
    
    if is_seller:
        welcome_text = (
            f"Welcome, {user.first_name}! You are registered as a YouTube recharge seller.\n\n"
            f"You will receive notifications for new orders. You can process them by:\n"
            f"- Marking them as Paid once payment is confirmed\n"
            f"- Marking them as Confirmed when the recharge is successful\n"
            f"- Or reporting issues if there are problems"
        )
    else:
        welcome_text = (
            f"Sorry, {user.first_name}. You are not registered as a seller.\n\n"
            f"Please contact the system administrator to get access."
        )
    
    await update.message.reply_text(welcome_text)

async def on_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /admin 命令 - 管理员专用命令"""
    user_id = update.effective_user.id
    
    if not is_admin_seller(user_id):
        await update.message.reply_text("You do not have administrator privileges.")
        return
    
    help_text = (
        "Admin commands:\n\n"
        "/stats - View system statistics\n"
    )
    
    await update.message.reply_text(help_text)

async def on_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /stats 命令 - 显示系统统计信息"""
    user_id = update.effective_user.id
    
    if not is_admin_seller(user_id):
        await update.message.reply_text("You do not have administrator privileges.")
        return
    
    # 构建键盘
    keyboard = [
        [
            InlineKeyboardButton("Today", callback_data="stats_today"),
            InlineKeyboardButton("This Week", callback_data="stats_week"),
        ],
        [
            InlineKeyboardButton("This Month", callback_data="stats_month"),
            InlineKeyboardButton("All Time", callback_data="stats_all"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("Select a time period for statistics:", reply_markup=reply_markup)

@callback_error_handler
async def on_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理统计数据回调"""
    query = update.callback_query
    user_id = query.from_user.id
    
    if not is_admin_seller(user_id):
        await query.answer("You do not have administrator privileges.", show_alert=True)
        return
    
    await query.answer()
    
    data = query.data
    today = datetime.now().date()
    
    if data == "stats_today":
        # 今天的统计
        start_date = today
        end_date = today + timedelta(days=1)
        period_text = "Today"
    elif data == "stats_week":
        # 本周的统计（过去7天）
        start_date = today - timedelta(days=7)
        end_date = today + timedelta(days=1)
        period_text = "Past 7 days"
    elif data == "stats_month":
        # 本月的统计（过去30天）
        start_date = today - timedelta(days=30)
        end_date = today + timedelta(days=1)
        period_text = "Past 30 days"
    elif data == "stats_all":
        # 所有时间的统计
        try:
            # 获取所有订单统计
            total_count = execute_query("SELECT COUNT(*) FROM orders", fetch=True)[0][0]
            
            submitted_count = execute_query(
                "SELECT COUNT(*) FROM orders WHERE status = %s", 
                (STATUS['SUBMITTED'],), 
                fetch=True
            )[0][0]
            
            paid_count = execute_query(
                "SELECT COUNT(*) FROM orders WHERE status = %s", 
                (STATUS['PAID'],), 
                fetch=True
            )[0][0]
            
            confirmed_count = execute_query(
                "SELECT COUNT(*) FROM orders WHERE status = %s", 
                (STATUS['CONFIRMED'],), 
                fetch=True
            )[0][0]
            
            failed_count = execute_query(
                "SELECT COUNT(*) FROM orders WHERE status = %s", 
                (STATUS['FAILED'],), 
                fetch=True
            )[0][0]
            
            need_new_qr_count = execute_query(
                "SELECT COUNT(*) FROM orders WHERE status = %s", 
                (STATUS['NEED_NEW_QR'],), 
                fetch=True
            )[0][0]
            
            other_issue_count = execute_query(
                "SELECT COUNT(*) FROM orders WHERE status = %s", 
                (STATUS['OTHER_ISSUE'],), 
                fetch=True
            )[0][0]
            
            stats_message = (
                f"📊 *All Time Statistics*\n\n"
                f"*Total Orders:* {total_count}\n\n"
                f"*Status Breakdown:*\n"
                f"• Submitted: {submitted_count}\n"
                f"• Paid: {paid_count}\n"
                f"• Confirmed: {confirmed_count}\n"
                f"• Failed: {failed_count}\n"
                f"• Need New QR: {need_new_qr_count}\n"
                f"• Other Issues: {other_issue_count}\n"
            )
            
            await query.edit_message_text(
                text=stats_message,
                parse_mode="Markdown"
            )
            return
            
        except Exception as e:
            logger.error(f"Error getting statistics: {str(e)}", exc_info=True)
            await query.edit_message_text(f"Error getting statistics: {str(e)}")
            return
    else:
        await query.edit_message_text("Invalid selection")
        return
    
    try:
        # 获取时间段内的订单统计
        total_count = execute_query(
            "SELECT COUNT(*) FROM orders WHERE created_at BETWEEN %s AND %s", 
            (start_date, end_date), 
            fetch=True
        )[0][0]
        
        submitted_count = execute_query(
            "SELECT COUNT(*) FROM orders WHERE status = %s AND created_at BETWEEN %s AND %s", 
            (STATUS['SUBMITTED'], start_date, end_date), 
            fetch=True
        )[0][0]
        
        paid_count = execute_query(
            "SELECT COUNT(*) FROM orders WHERE status = %s AND created_at BETWEEN %s AND %s", 
            (STATUS['PAID'], start_date, end_date), 
            fetch=True
        )[0][0]
        
        confirmed_count = execute_query(
            "SELECT COUNT(*) FROM orders WHERE status = %s AND created_at BETWEEN %s AND %s", 
            (STATUS['CONFIRMED'], start_date, end_date), 
            fetch=True
        )[0][0]
        
        failed_count = execute_query(
            "SELECT COUNT(*) FROM orders WHERE status = %s AND created_at BETWEEN %s AND %s", 
            (STATUS['FAILED'], start_date, end_date), 
            fetch=True
        )[0][0]
        
        stats_message = (
            f"📊 *Statistics for {period_text}*\n\n"
            f"*Total Orders:* {total_count}\n\n"
            f"*Status Breakdown:*\n"
            f"• Submitted: {submitted_count}\n"
            f"• Paid: {paid_count}\n"
            f"• Confirmed: {confirmed_count}\n"
            f"• Failed: {failed_count}\n"
        )
        
        await query.edit_message_text(
            text=stats_message,
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error getting period statistics: {str(e)}", exc_info=True)
        await query.edit_message_text(f"Error getting statistics: {str(e)}")

@callback_error_handler
async def on_order_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理订单操作回调"""
    query = update.callback_query
    user_id = query.from_user.id
    
    if user_id not in get_active_seller_ids():
        await query.answer("You are not authorized to process orders.", show_alert=True)
        return
    
    await query.answer()
    
    data = query.data.split("_")
    action = data[0]
    order_id = int(data[1])
    
    # 获取订单详情
    order = get_order_details(order_id)
    
    if not order:
        await query.edit_message_text("Order not found or has been deleted.")
        return
    
    user = query.from_user
    handler_username = user.username if user.username else f"user_{user.id}"
    
    if action == "paid":
        # 标记为已支付
        if update_order_status(order_id, STATUS['PAID'], user_id, handler_username):
            new_message = (
                f"Order #{order_id}\n\n"
                f"Status: *{STATUS_TEXT_EN[STATUS['PAID']]}*\n"
                f"Marked by: @{handler_username}\n\n"
                f"What's the final result of this order?"
            )
            
            # 提供确认或报告问题的选项
            keyboard = [
                [
                    InlineKeyboardButton("✅ Confirm Success", callback_data=f"confirm_{order_id}"),
                ],
                [
                    InlineKeyboardButton("❌ Failed", callback_data=f"fail_{order_id}"),
                    InlineKeyboardButton("🔄 Need New QR", callback_data=f"newqr_{order_id}"),
                ],
                [
                    InlineKeyboardButton("⚠️ Other Issue", callback_data=f"other_{order_id}"),
                ],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                text=new_message,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Failed to update order status. Please try again.")
    
    elif action == "confirm":
        # 标记为已确认（充值成功）
        if update_order_status(order_id, STATUS['CONFIRMED'], user_id, handler_username):
            new_message = (
                f"Order #{order_id}\n\n"
                f"Status: *{STATUS_TEXT_EN[STATUS['CONFIRMED']]}*\n"
                f"Marked by: @{handler_username}\n\n"
                f"✅ Order completed successfully"
            )
            
            await query.edit_message_text(
                text=new_message,
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Failed to update order status. Please try again.")
    
    elif action == "fail":
        # 标记为失败
        feedback_waiting[user_id] = {"type": "fail", "order_id": order_id}
        
        new_message = (
            f"Order #{order_id}\n\n"
            f"Please provide a reason for the failure.\n"
            f"Reply to this message with your explanation."
        )
        
        await query.edit_message_text(text=new_message)
    
    elif action == "newqr":
        # 标记为需要新二维码
        if update_order_status(order_id, STATUS['NEED_NEW_QR'], user_id, handler_username):
            new_message = (
                f"Order #{order_id}\n\n"
                f"Status: *{STATUS_TEXT_EN[STATUS['NEED_NEW_QR']]}*\n"
                f"Marked by: @{handler_username}\n\n"
                f"⚠️ Customer needs to provide a new QR code"
            )
            
            await query.edit_message_text(
                text=new_message,
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Failed to update order status. Please try again.")
    
    elif action == "other":
        # 标记为其他问题
        feedback_waiting[user_id] = {"type": "other", "order_id": order_id}
        
        new_message = (
            f"Order #{order_id}\n\n"
            f"Please describe the issue.\n"
            f"Reply to this message with your explanation."
        )
        
        await query.edit_message_text(text=new_message)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文本消息"""
    user_id = update.effective_user.id
    message_text = update.message.text
    
    # 检查是否在等待此用户的反馈
    if user_id in feedback_waiting:
        feedback_data = feedback_waiting[user_id]
        order_id = feedback_data["order_id"]
        feedback_type = feedback_data["type"]
        
        # 根据反馈类型处理不同的状态更新
        if feedback_type == "fail":
            status = STATUS['FAILED']
        elif feedback_type == "other":
            status = STATUS['OTHER_ISSUE']
        else:
            await update.message.reply_text("Invalid feedback type. Please try again.")
            return
        
        # 更新订单状态并添加反馈
        handler_username = update.effective_user.username if update.effective_user.username else f"user_{user_id}"
        
        if update_order_status(order_id, status, user_id, handler_username, message_text):
            # 清除等待状态
            del feedback_waiting[user_id]
            
            status_text = STATUS_TEXT_EN[status]
            await update.message.reply_text(
                f"Order #{order_id} status updated to *{status_text}*\n"
                f"Feedback recorded. Thank you!",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("Failed to update order status. Please try again.")
    
    else:
        # 如果不是在等待反馈，则回复帮助信息
        if user_id in get_active_seller_ids():
            help_text = (
                "Available commands:\n"
                "/start - Show welcome message\n"
                "/stats - View statistics (admin only)\n"
            )
            await update.message.reply_text(help_text)

# ===== 通知函数 =====
async def check_and_push_orders():
    """检查并推送新订单"""
    try:
        # 获取未通知的订单
        unnotified_orders = get_unnotified_orders()
        
        if not unnotified_orders:
            return
        
        logger.info(f"Found {len(unnotified_orders)} unnotified orders")
        
        # 获取活跃的卖家ID
        seller_ids = get_active_seller_ids()
        
        if not seller_ids:
            logger.warning("No active sellers found to notify")
            return
        
        for order_row in unnotified_orders:
            order_id = order_row[0]
            status = order_row[1]
            
            # 使用锁确保不会重复通知
            with notified_orders_lock:
                if order_id in notified_orders:
                    continue
                notified_orders.add(order_id)
            
            # 获取订单详情
            order_details = get_order_details(order_id)
            
            if not order_details:
                logger.warning(f"Order details not found for order {order_id}")
                continue
            
            # 根据订单状态发送不同的通知
            if status == STATUS['SUBMITTED']:
                # 新订单通知
                await send_new_order_notification(order_details, seller_ids)
            elif status in [STATUS['PAID'], STATUS['CONFIRMED'], STATUS['FAILED'], STATUS['NEED_NEW_QR'], STATUS['OTHER_ISSUE']]:
                # 状态更新通知
                await send_status_update_notification(order_details, seller_ids)
            
            # 标记订单为已通知
            set_order_notified(order_id)
    
    except Exception as e:
        logger.error(f"Error checking and pushing orders: {str(e)}", exc_info=True)

async def send_new_order_notification(order_details, seller_ids):
    """发送新订单通知"""
    order_id = order_details["id"]
    qr_code_path = order_details["qr_code_path"]
    created_at = order_details["created_at"]
    
    # 构建消息
    message = (
        f"🆕 *New YouTube Recharge Order*\n\n"
        f"Order ID: *#{order_id}*\n"
        f"Created at: {created_at}\n\n"
        f"Please review the QR code image and process this order."
    )
    
    # 构建键盘
    keyboard = [
        [
            InlineKeyboardButton("✅ Mark as Paid", callback_data=f"paid_{order_id}"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # 发送通知到所有活跃卖家
    for seller_id in seller_ids:
        try:
            # 发送二维码图片
            full_qr_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), qr_code_path.lstrip('/'))
            
            if os.path.exists(full_qr_path):
                with open(full_qr_path, 'rb') as photo:
                    await bot_application.bot.send_photo(
                        chat_id=seller_id,
                        photo=photo,
                        caption=message,
                        reply_markup=reply_markup,
                        parse_mode="Markdown"
                    )
            else:
                # 如果找不到图片，则只发送文本
                logger.warning(f"QR code image not found at {full_qr_path}")
                await bot_application.bot.send_message(
                    chat_id=seller_id,
                    text=message + "\n\n⚠️ *QR code image not found*",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Error sending new order notification to seller {seller_id}: {str(e)}", exc_info=True)

async def send_status_update_notification(order_details, seller_ids):
    """发送订单状态更新通知"""
    order_id = order_details["id"]
    status = order_details["status"]
    handler_username = order_details["handled_by_username"]
    feedback = order_details["feedback"]
    
    status_text = STATUS_TEXT_EN[status]
    status_emoji = {
        STATUS['PAID']: "💰",
        STATUS['CONFIRMED']: "✅",
        STATUS['FAILED']: "❌",
        STATUS['NEED_NEW_QR']: "🔄",
        STATUS['OTHER_ISSUE']: "⚠️"
    }.get(status, "📝")
    
    # 构建消息
    message = (
        f"{status_emoji} *Order Status Update*\n\n"
        f"Order ID: *#{order_id}*\n"
        f"New Status: *{status_text}*\n"
    )
    
    if handler_username:
        message += f"Updated by: @{handler_username}\n"
    
    if feedback and (status == STATUS['FAILED'] or status == STATUS['OTHER_ISSUE']):
        message += f"\n*Feedback:*\n{feedback}\n"
    
    # 发送通知到所有活跃卖家
    for seller_id in seller_ids:
        try:
            await bot_application.bot.send_message(
                chat_id=seller_id,
                text=message,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Error sending status update notification to seller {seller_id}: {str(e)}", exc_info=True)

# ===== 主函数 =====
def run_bot(notification_queue):
    """在单独的线程中运行Telegram机器人"""
    bot_thread = threading.Thread(target=run_bot_in_thread)
    bot_thread.daemon = True
    bot_thread.start()
    logger.info("Telegram bot thread started")

def run_bot_in_thread():
    """在线程中运行机器人的主函数"""
    asyncio.run(bot_main())

async def bot_main():
    """机器人主函数"""
    global bot_application, BOT_LOOP
    
    if not BOT_TOKEN:
        logger.error("Bot token not found. Please set the BOT_TOKEN environment variable.")
        return
    
    try:
        # 保存事件循环的引用
        BOT_LOOP = asyncio.get_event_loop()
        
        # 创建机器人应用
        application = ApplicationBuilder().token(BOT_TOKEN).build()
        bot_application = application
        
        # 注册命令处理函数
        application.add_handler(CommandHandler("start", on_start))
        application.add_handler(CommandHandler("admin", on_admin_command))
        application.add_handler(CommandHandler("stats", on_stats))
        
        # 注册回调查询处理函数
        application.add_handler(CallbackQueryHandler(on_stats_callback, pattern="^stats_"))
        application.add_handler(CallbackQueryHandler(on_order_action, pattern="^(paid|confirm|fail|newqr|other)_"))
        
        # 注册消息处理函数
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        
        # 注册错误处理函数
        application.add_error_handler(error_handler)
        
        # 启动周期性任务
        application.job_queue.run_repeating(periodic_check_callback, interval=30, first=10)
        
        logger.info("Starting bot polling...")
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        
        # 保持机器人运行
        while True:
            await asyncio.sleep(1)
    
    except Exception as e:
        logger.error(f"Error in bot main function: {str(e)}", exc_info=True)

async def error_handler(update, context):
    """处理错误"""
    logger.error(f"Update {update} caused error: {context.error}", exc_info=True)
    
    # 发送错误通知
    try:
        if update and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="An error occurred while processing your request. Please try again later."
            )
    except:
        pass

async def periodic_check_callback(context: ContextTypes.DEFAULT_TYPE):
    """周期性检查回调"""
    await check_and_push_orders()

# 限制访问的装饰器
def restricted(func):
    """限制只有卖家可以访问的装饰器"""
    @wraps(func)
    async def wrapped(update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in get_active_seller_ids():
            await update.message.reply_text("You are not authorized to use this command.")
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
    """处理回调查询"""
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    
    logger.info(f"收到回调查询: {data} 来自用户 {user_id}")
    
    # 处理不同类型的回调
    if data.startswith("accept:"):
        await on_accept(update, context)
    elif data.startswith("feedback:"):
        await on_feedback_button(update, context)
    elif data.startswith("stats:"):
        await on_stats_callback(update, context)
    elif data.startswith("approve_recharge:"):
        await on_approve_recharge(update, context)
    elif data.startswith("reject_recharge:"):
        await on_reject_recharge(update, context)
    else:
        await query.answer("Unknown command")

@callback_error_handler
async def on_approve_recharge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理批准充值请求的回调"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    # 只允许超级管理员处理充值请求
    if user_id != 1878943383:
        await query.answer("您没有权限执行此操作", show_alert=True)
        return
    
    # 获取充值请求ID
    request_id = int(query.data.split(":")[1])
    
    # 批准充值请求
    success, message = approve_recharge_request(request_id, str(user_id))
    
    if success:
        # 更新消息
        keyboard = [[InlineKeyboardButton("✅ 已批准", callback_data="dummy_action")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.edit_message_reply_markup(reply_markup=reply_markup)
            await query.answer("充值请求已批准", show_alert=True)
        except Exception as e:
            logger.error(f"更新消息失败: {str(e)}")
            await query.answer("操作成功，但更新消息失败", show_alert=True)
    else:
        await query.answer(f"操作失败: {message}", show_alert=True)

@callback_error_handler
async def on_reject_recharge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理拒绝充值请求的回调"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    # 只允许超级管理员处理充值请求
    if user_id != 1878943383:
        await query.answer("您没有权限执行此操作", show_alert=True)
        return
    
    # 获取充值请求ID
    request_id = int(query.data.split(":")[1])
    
    # 拒绝充值请求
    success, message = reject_recharge_request(request_id, str(user_id))
    
    if success:
        # 更新消息
        keyboard = [[InlineKeyboardButton("❌ 已拒绝", callback_data="dummy_action")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            await query.edit_message_reply_markup(reply_markup=reply_markup)
            await query.answer("充值请求已拒绝", show_alert=True)
        except Exception as e:
            logger.error(f"更新消息失败: {str(e)}")
            await query.answer("操作成功，但更新消息失败", show_alert=True)
    else:
        await query.answer(f"操作失败: {message}", show_alert=True) 