import asyncio
import threading
import logging
import time
import os
from datetime import datetime
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
    BOT_TOKEN, STATUS, PLAN_LABELS_EN, STATUS_TEXT_EN,
    RECHARGE_PRICES
)
from modules.database import (
    get_order_details, update_order_status, execute_query, 
    get_unnotified_orders, get_active_seller_ids, is_admin_seller
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
                logger.error(f"Error notifying user: {str(notify_err)}")
            
            return None
    return wrapper

# ===== 全局变量 =====
bot_application = None
BOT_LOOP = None

# 跟踪等待额外反馈的订单
feedback_waiting = {}

# 用户信息缓存
user_info_cache = {}

# ===== TG 辅助函数 =====
def is_seller(chat_id):
    """检查用户是否为已授权的卖家"""
    try:
        # 确保chat_id是整数
        chat_id = int(chat_id)
        return chat_id in get_active_seller_ids()
    except (ValueError, TypeError):
        return False

# 添加处理 Telegram webhook 更新的函数
async def process_telegram_update_async(update_data, notification_queue):
    """异步处理来自Telegram webhook的更新"""
    global bot_application
    
    try:
        if not bot_application:
            logger.error("Bot application not initialized, can't process webhook update")
            return
        
        # 将JSON数据转换为Update对象
        update = Update.de_json(data=update_data, bot=bot_application.bot)
        
        if not update:
            logger.error("Cannot convert webhook data to Update object")
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
            logger.error("Bot event loop not initialized, can't process webhook update")
            return
        
        # 在机器人的事件循环中运行异步处理函数
        asyncio.run_coroutine_threadsafe(
            process_telegram_update_async(update_data, notification_queue),
            BOT_LOOP
        )
        
        logger.info("Webhook update submitted to bot event loop")
    
    except Exception as e:
        logger.error(f"Error submitting webhook update to event loop: {str(e)}", exc_info=True)

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
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    if is_seller(chat_id):
        await update.message.reply_text(
            f"Hello, {user.first_name}! You are a registered seller.\n\n"
            f"You will receive notifications for new YouTube recharge orders.\n"
            f"Please wait for orders to process.\n\n"
            f"Current time: {get_china_time()}"
        )
    else:
        await update.message.reply_text(
            f"Hello, {user.first_name}!\n\n"
            f"I'm the YouTube Recharge Bot. Only registered sellers can use this bot.\n"
            f"If you should be a seller, please contact the administrator."
        )

@callback_error_handler
async def on_update_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理订单状态更新回调"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    if not is_seller(user_id):
        await query.edit_message_text(
            "You are not authorized to perform this action."
        )
        return
    
    # 解析回调数据，格式: action:order_id
    try:
        action, order_id = query.data.split(":", 1)
        order_id = int(order_id)
    except ValueError:
        await query.edit_message_text("Invalid action format.")
        return
    
    # 获取订单详情
    order = get_order_details(order_id)
    if not order:
        await query.edit_message_text(f"Order #{order_id} not found.")
        return
    
    # 根据动作更新订单状态
    user_info = await get_user_info(user_id)
    seller_id = str(user_id)
    seller_username = user_info.get("username")
    seller_first_name = user_info.get("first_name")
    
    status_updated = False
    new_status = None
    message = None
    
    if action == "confirm_paid":
        # 标记为已支付
        status_updated = update_order_status(
            order_id, STATUS['PAID'], 
            seller_id, seller_username, seller_first_name
        )
        new_status = STATUS['PAID']
    elif action == "confirm_complete":
        # 标记为已确认（完成）
        status_updated = update_order_status(
            order_id, STATUS['CONFIRMED'], 
            seller_id, seller_username, seller_first_name
        )
        new_status = STATUS['CONFIRMED']
    elif action == "mark_failed":
        # 标记为失败
        # 将订单状态存入 feedback_waiting，等待用户输入失败原因
        feedback_waiting[user_id] = {
            "order_id": order_id,
            "action": "failed_reason",
            "expires_at": time.time() + 300  # 5分钟过期
        }
        
        await query.edit_message_text(
            f"Order #{order_id} - Please provide the reason for failure.\n"
            f"Simply reply to this message with your explanation."
        )
        return
    elif action == "request_new_qr":
        # 标记为需要新二维码
        status_updated = update_order_status(
            order_id, STATUS['NEED_NEW_QR'], 
            seller_id, seller_username, seller_first_name,
            message="Seller requested a new QR code"
        )
        new_status = STATUS['NEED_NEW_QR']
    else:
        await query.edit_message_text(f"Unknown action: {action}")
        return
    
    if status_updated:
        # 构建更新后的订单信息消息
        status_text = STATUS_TEXT_EN.get(new_status, new_status)
        package_text = PLAN_LABELS_EN.get(order['package'], order['package'])
        
        message_text = (
            f"✅ Order #{order_id} updated to: {status_text}\n\n"
            f"Customer: {order['customer_name'] or 'N/A'}\n"
            f"Package: {package_text}\n"
            f"Created: {order['created_at']}\n"
        )
        
        if new_status == STATUS['PAID']:
            message_text += f"Paid at: {get_order_details(order_id)['paid_at']}\n"
            
            # 提供完成或失败的按钮
            keyboard = [
                [
                    InlineKeyboardButton("✅ Confirm Complete", callback_data=f"confirm_complete:{order_id}"),
                    InlineKeyboardButton("❌ Mark Failed", callback_data=f"mark_failed:{order_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                text=message_text,
                reply_markup=reply_markup
            )
        elif new_status == STATUS['CONFIRMED']:
            message_text += (
                f"Paid at: {order['paid_at']}\n"
                f"Confirmed at: {get_order_details(order_id)['confirmed_at']}\n\n"
                f"✅ This order has been completed successfully!"
            )
            await query.edit_message_text(message_text)
        elif new_status == STATUS['NEED_NEW_QR']:
            message_text += (
                f"Status: Waiting for new QR code\n\n"
                f"The customer has been notified to provide a new QR code."
            )
            await query.edit_message_text(message_text)
    else:
        await query.edit_message_text(
            f"Failed to update order #{order_id}. Please try again later."
        )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文本消息"""
    user_id = update.effective_user.id
    message_text = update.message.text
    
    # 检查是否是在等待输入失败原因
    if user_id in feedback_waiting:
        feedback_data = feedback_waiting[user_id]
        
        # 检查是否过期
        if time.time() > feedback_data.get("expires_at", 0):
            del feedback_waiting[user_id]
            await update.message.reply_text("Your feedback session has expired. Please try again.")
            return
        
        # 处理失败原因
        if feedback_data["action"] == "failed_reason":
            order_id = feedback_data["order_id"]
            reason = message_text.strip()
            
            if not reason:
                await update.message.reply_text("Please provide a valid reason for failure.")
                return
            
            # 获取用户信息
            user_info = await get_user_info(user_id)
            seller_id = str(user_id)
            seller_username = user_info.get("username")
            seller_first_name = user_info.get("first_name")
            
            # 更新订单状态为失败，并添加原因
            status_updated = update_order_status(
                order_id, STATUS['FAILED'], 
                seller_id, seller_username, seller_first_name,
                message=reason
            )
            
            if status_updated:
                await update.message.reply_text(
                    f"Order #{order_id} has been marked as failed with reason:\n\n"
                    f"{reason}"
                )
            else:
                await update.message.reply_text(
                    f"Failed to update order #{order_id}. Please try again later."
                )
            
            # 清除等待状态
            del feedback_waiting[user_id]
            return
    
    # 如果不是在等待输入，则检查是否是卖家
    if is_seller(user_id):
        await update.message.reply_text(
            "I'm listening for commands. Use /start to see available options."
        )
    else:
        await update.message.reply_text(
            "Only registered sellers can interact with this bot. If you should be a seller, please contact the administrator."
        )

# ===== 通知处理 =====
async def send_notification_from_queue(data):
    """处理来自队列的通知"""
    notification_type = data.get('type')
    
    if notification_type == 'new_order':
        await send_new_order_notification(data)
    elif notification_type == 'status_change':
        await send_status_change_notification(data)
    else:
        logger.warning(f"Unknown notification type: {notification_type}")

async def send_new_order_notification(data):
    """发送新订单通知到所有卖家"""
    global bot_application
    
    if not bot_application:
        logger.error("Bot application not initialized, can't send notifications")
        return
    
    order_id = data.get('order_id')
    
    # 获取订单详情
    order = get_order_details(order_id)
    if not order:
        logger.error(f"Order #{order_id} not found for notification")
        return
    
    # 获取所有活跃卖家
    seller_ids = get_active_seller_ids()
    if not seller_ids:
        logger.warning("No active sellers to notify")
        return
    
    # 构建消息
    package_text = PLAN_LABELS_EN.get(order['package'], order['package'])
    price = RECHARGE_PRICES.get(order['package'], "Unknown")
    
    message_text = (
        f"🆕 NEW ORDER #{order_id}\n\n"
        f"Customer: {order['customer_name'] or 'N/A'}\n"
        f"Package: {package_text}\n"
        f"Price: ¥{price}\n"
        f"Time: {order['created_at']}\n\n"
        f"The customer has uploaded a QR code for YouTube recharge."
    )
    
    # 构建按钮
    keyboard = [
        [InlineKeyboardButton("✅ Mark as Paid", callback_data=f"confirm_paid:{order_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # 获取二维码图片
    qr_image_path = order['qr_image']
    
    # 发送到所有卖家
    for seller_id in seller_ids:
        try:
            # 首先发送图片
            with open(qr_image_path, 'rb') as photo:
                await bot_application.bot.send_photo(
                    chat_id=seller_id,
                    photo=photo,
                    caption=f"QR Code for Order #{order_id}"
                )
            
            # 然后发送订单信息和按钮
            await bot_application.bot.send_message(
                chat_id=seller_id,
                text=message_text,
                reply_markup=reply_markup
            )
            
            logger.info(f"Sent order #{order_id} notification to seller {seller_id}")
        except Exception as e:
            logger.error(f"Failed to send notification to seller {seller_id}: {str(e)}")
    
    # 标记订单为已通知
    from modules.database import set_order_notified
    set_order_notified(order_id)

async def send_status_change_notification(data):
    """发送订单状态变更通知到相关卖家"""
    global bot_application
    
    if not bot_application:
        logger.error("Bot application not initialized, can't send notifications")
        return
    
    order_id = data.get('order_id')
    new_status = data.get('new_status')
    
    # 获取订单详情
    order = get_order_details(order_id)
    if not order:
        logger.error(f"Order #{order_id} not found for status change notification")
        return
    
    # 只通知订单的处理卖家
    seller_id = order.get('seller_id')
    if not seller_id:
        logger.warning(f"Order #{order_id} has no assigned seller, notifying admin")
        # 通知管理员卖家
        admin_sellers = [sid for sid in get_active_seller_ids() if is_admin_seller(sid)]
        if admin_sellers:
            seller_id = admin_sellers[0]
        else:
            logger.error("No admin sellers found to notify about status change")
            return
    
    # 构建消息
    status_text = STATUS_TEXT_EN.get(new_status, new_status)
    package_text = PLAN_LABELS_EN.get(order['package'], order['package'])
    
    message_text = (
        f"🔄 ORDER STATUS UPDATE #{order_id}\n\n"
        f"Customer: {order['customer_name'] or 'N/A'}\n"
        f"Package: {package_text}\n"
        f"New Status: {status_text}\n"
    )
    
    if new_status == STATUS['NEED_NEW_QR']:
        message_text += (
            f"\nThe customer has been asked to provide a new QR code."
            f"You'll receive a notification when they upload it."
        )
    
    # 发送通知
    try:
        await bot_application.bot.send_message(
            chat_id=seller_id,
            text=message_text
        )
        logger.info(f"Sent status change notification for order #{order_id} to seller {seller_id}")
    except Exception as e:
        logger.error(f"Failed to send status change notification to seller {seller_id}: {str(e)}")

# ===== 机器人主函数 =====
def run_bot(notification_queue):
    """在单独的线程中运行Telegram机器人"""
    threading.Thread(target=run_bot_in_thread, args=(notification_queue,), daemon=True).start()

def run_bot_in_thread(notification_queue):
    """在线程中异步运行Telegram机器人"""
    asyncio.run(bot_main(notification_queue))

async def bot_main(notification_queue):
    """Telegram机器人主函数"""
    global bot_application, BOT_LOOP
    
    # 保存当前事件循环，以便webhook处理可以使用
    BOT_LOOP = asyncio.get_running_loop()
    
    # 创建应用程序
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    bot_application = application
    
    # 添加命令处理器
    application.add_handler(CommandHandler("start", on_start))
    
    # 添加回调查询处理器
    application.add_handler(CallbackQueryHandler(on_update_status))
    
    # 添加文本消息处理器
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    
    # 添加错误处理器
    application.add_error_handler(error_handler)
    
    # 启动通知队列处理
    asyncio.create_task(process_notification_queue(notification_queue))
    
    # 启动应用程序
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logger.info("Telegram bot started")
    
    # 持续运行，直到程序结束
    try:
        await application.updater.start_polling()
        await asyncio.Event().wait()  # 永远等待
    except Exception as e:
        logger.error(f"Bot main loop error: {str(e)}", exc_info=True)
    finally:
        # 清理
        await application.updater.stop()
        await application.stop()
        await application.shutdown()

async def error_handler(update, context):
    """处理错误的全局处理程序"""
    logger.error(f"Update {update} caused error: {context.error}", exc_info=context.error)
    
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "Sorry, something went wrong. The error has been logged."
            )
    except:
        pass

async def process_notification_queue(queue):
    """处理通知队列的任务"""
    while True:
        try:
            # 非阻塞方式获取通知
            if not queue.empty():
                notification = queue.get_nowait()
                await send_notification_from_queue(notification)
                queue.task_done()
            
            # 检查有没有未通知的订单
            from modules.database import get_unnotified_orders
            unnotified_orders = get_unnotified_orders()
            
            for order in unnotified_orders:
                notification = {
                    'type': 'new_order',
                    'order_id': order['id']
                }
                await send_notification_from_queue(notification)
                
            # 适当休眠，避免CPU占用过高
            await asyncio.sleep(2)
            
        except Exception as e:
            logger.error(f"Error processing notification queue: {str(e)}", exc_info=True)
            await asyncio.sleep(5)  # 出错后等待更长时间 