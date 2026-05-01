import asyncio
import logging
from datetime import datetime, timedelta
import time
import os
from functools import wraps
import functools
import sqlite3
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
    get_order_details, accept_order_atomic, execute_query,
    get_unnotified_orders, get_active_seller_ids, approve_recharge_request, reject_recharge_request,
    get_china_time
)

logger = logging.getLogger(__name__)

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
            
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row  # 使查询结果可以通过列名访问
            return conn
    except Exception as e:
        logger.error(f"获取数据库连接时出错: {str(e)}", exc_info=True)
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
            
            # 尝试通知用户
            try:
                if update.callback_query:
                    await update.callback_query.answer("Operation failed, please try again later", show_alert=True)
            except Exception as notify_err:
                logger.error(f"无法通知用户错误: {str(notify_err)}")
            
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
    # 只从数据库中获取卖家信息，因为环境变量中的卖家已经同步到数据库
    return chat_id in get_active_seller_ids()

# 添加处理 Telegram webhook 更新的函数
async def process_telegram_update_async(update_data, notification_queue):
    """异步处理来自Telegram webhook的更新"""
    global bot_application
    
    try:
        if not bot_application:
            logger.error("机器人应用未初始化，无法处理webhook更新")
            return
        
        # 将JSON数据转换为Update对象
        update = Update.de_json(data=update_data, bot=bot_application.bot)
        
        if not update:
            logger.error("无法将webhook数据转换为Update对象")
            return
        
        # 处理更新
        logger.info(f"正在处理webhook更新: {update.update_id}")
        
        # 将更新分派给应用程序处理
        await bot_application.process_update(update)
        
        logger.info(f"webhook更新 {update.update_id} 处理完成")
    
    except Exception as e:
        logger.error(f"处理webhook更新时出错: {str(e)}", exc_info=True)

def process_telegram_update(update_data, notification_queue):
    """处理来自Telegram webhook的更新（同步包装器）"""
    global BOT_LOOP
    
    try:
        if not BOT_LOOP:
            logger.error("机器人事件循环未初始化，无法处理webhook更新")
            return
        
        # 在机器人的事件循环中运行异步处理函数
        asyncio.run_coroutine_threadsafe(
            process_telegram_update_async(update_data, notification_queue),
            BOT_LOOP
        )
        
        logger.info("已将webhook更新提交到机器人事件循环处理")
    
    except Exception as e:
        logger.error(f"提交webhook更新到事件循环时出错: {str(e)}", exc_info=True)

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
    """开始命令处理"""
    user_id = update.effective_user.id
    
    if is_seller(user_id):
        await update.message.reply_text(
            "🌟 *Welcome to the Premium Recharge System!* 🌟\n\n"
            "As a verified seller, you have access to:\n"
            "• `/seller` - View available orders and your active orders\n"
            "• `/stats` - Check your performance statistics\n\n"
            "Need assistance? Feel free to contact the administrator.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "⚠️ *Access Restricted* ⚠️\n\n"
            "This bot is exclusively available to authorized sellers.\n"
            "For account inquiries, please contact the administrator.",
            parse_mode='Markdown'
        )

async def on_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理卖家命令"""
    user_id = update.effective_user.id
    
    if not is_seller(user_id):
        await update.message.reply_text(
            "⚠️ *Access Denied* ⚠️\n\n"
            "You are not authorized to use this command.",
            parse_mode='Markdown'
        )
        return
    
    # 首先检查当前用户的活跃订单数
    active_orders_count = execute_query("""
        SELECT COUNT(*) FROM orders 
        WHERE accepted_by = ? AND status = ?
    """, (str(user_id), STATUS['ACCEPTED']), fetch=True)[0][0]
    
    # 发送当前状态
    if active_orders_count >= 3:
        status_icon = "🔴"
        status_message = f"{status_icon} *Seller Status:* {active_orders_count}/3 active orders\n⚠️ *Maximum limit reached.* Please complete existing orders first."
    else:
        status_icon = "🟢" 
        status_message = f"{status_icon} *Seller Status:* {active_orders_count}/3 active orders\n✅ *You can accept new orders.*"
    
    await update.message.reply_text(
        status_message,
        parse_mode='Markdown'
    )
    
    # 查询待处理订单
    new_orders = execute_query("""
        SELECT id, account, password, package, created_at FROM orders 
        WHERE status = ? ORDER BY id DESC LIMIT 5
    """, (STATUS['SUBMITTED'],), fetch=True)
    
    my_orders = execute_query("""
        SELECT id, account, password, package, status FROM orders 
        WHERE accepted_by = ? AND status IN (?, ?) ORDER BY id DESC LIMIT 5
    """, (str(user_id), STATUS['ACCEPTED'], STATUS['FAILED']), fetch=True)
    
    # 发送订单信息
    if new_orders:
        await update.message.reply_text(
            "📋 *Available Orders*",
            parse_mode='Markdown'
        )
        for order in new_orders:
            oid, account, password, package, created_at = order
            
            keyboard = [[InlineKeyboardButton("✅ Accept Order", callback_data=f"accept_{oid}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # 接单前不显示密码
            await update.message.reply_text(
                f"🔹 *Order #{oid}* - {created_at}\n\n"
                f"• 👤 Account: `{account}`\n"
                f"• 📦 Package: *{PLAN_LABELS_EN[package]}*\n"
                f"• 💰 Payment: *${TG_PRICES[package]}*",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
    else:
        await update.message.reply_text(
            "📭 *No pending orders available at this time.*",
            parse_mode='Markdown'
        )
    
    # 发送我的订单
    if my_orders:
        await update.message.reply_text(
            "🔄 *Your Active Orders*", 
            parse_mode='Markdown'
        )
        for order in my_orders:
            oid, account, password, package, status = order
            
            if status == STATUS['ACCEPTED']:
                keyboard = [
                    [InlineKeyboardButton("✅ Mark Complete", callback_data=f"done_{oid}"),
                     InlineKeyboardButton("❌ Mark Failed", callback_data=f"fail_{oid}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    f"🔸 *Order #{oid}*\n\n"
                    f"• 👤 Account: `{account}`\n"
                    f"• 🔑 Password: `{password}`\n"
                    f"• 📦 Package: *{PLAN_LABELS_EN[package]}*\n"
                    f"• 💰 Payment: *${TG_PRICES[package]}*",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )

# ===== TG 回调处理 =====
@callback_error_handler
async def on_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理接单回调"""
    query = update.callback_query
    user_id = query.from_user.id
    
    logger.info(f"收到接单回调: 用户ID={user_id}, data={repr(query.data)}")
    
    # 防止重复点击
    if (user_id, query.data) in processing_accepts:
        await query.answer("Processing, please don't click repeatedly")
        logger.info(f"用户 {user_id} 重复点击了 {query.data}")
        return
        
    try:
        parts = query.data.split('_')
        logger.info(f"分割后的数据: {parts}")
        
        if len(parts) < 2:
            logger.error(f"接单回调数据格式错误: {query.data}")
            await query.answer("Invalid order data format", show_alert=True)
            return
            
        oid_str = parts[1]
        try:
            oid = int(oid_str)
            logger.info(f"成功将订单ID转换为整数: {oid}")
        except ValueError as e:
            logger.error(f"接单回调数据无效，无法转换为整数: {oid_str}, 错误: {str(e)}")
            await query.answer("Invalid order ID", show_alert=True)
            return
    except (IndexError, ValueError) as e:
        logger.error(f"接单回调数据无效: {query.data}", exc_info=True)
        await query.answer("Invalid order data", show_alert=True)
        return

    # 添加到处理集合
    processing_accepts.add((user_id, query.data))
    processing_accepts_time[(user_id, query.data)] = time.time()

    logger.info(f"接单回调解析: 订单ID={oid}")
    
    try:
        # 使用accept_order_atomic函数处理接单
        success, message = accept_order_atomic(oid, user_id)
        
        if not success:
            # 从处理集合中移除
            if (user_id, query.data) in processing_accepts:
                processing_accepts.remove((user_id, query.data))
            if (user_id, query.data) in processing_accepts_time:
                del processing_accepts_time[(user_id, query.data)]
            
            # 根据不同的错误消息显示不同的按钮状态
            if message == "Order has been cancelled":
                keyboard = [[InlineKeyboardButton("Cancelled", callback_data="noop")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_reply_markup(reply_markup=reply_markup)
            elif message == "Order already taken":
                keyboard = [[InlineKeyboardButton("❌Already taken", callback_data="noop")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_reply_markup(reply_markup=reply_markup)
            
            await query.answer(message, show_alert=True)
            return
            
        # 获取订单详情
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if DATABASE_URL.startswith('postgres'):
            cursor.execute("SELECT * FROM orders WHERE id = %s", (oid,))
        else:
            cursor.execute("SELECT * FROM orders WHERE id = ?", (oid,))
            
        order_row = cursor.fetchone()
        columns = [column[0] for column in cursor.description]
        order = {columns[i]: order_row[i] for i in range(len(columns))}
        conn.close()
        
        # 确认回调
        await query.answer("You have successfully accepted the order!", show_alert=True)
        
        # 更新消息
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Mark as Complete", callback_data=f"done_{oid}"),
             InlineKeyboardButton("❌ Mark as Failed", callback_data=f"fail_{oid}")]
        ])
        
        # 获取订单详情以显示
        account = order.get('account', '未知账号')
        password = order.get('password', '未知密码')
        package = order.get('package', '未知套餐')
        
        await query.edit_message_text(
            f"📦 *Order #{oid}*\n\n"
            f"• Account: `{account}`\n"
            f"• Password: `{password}`\n"
            f"• Package: *{PLAN_LABELS_EN.get(package, package)}*\n\n"
            f"*✅ This order has been accepted*\n"
            f"Accepted by: `{order.get('accepted_by_first_name') or order.get('accepted_by_username') or str(user_id)}`",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
        
        # 从处理集合中移除
        if (user_id, query.data) in processing_accepts:
            processing_accepts.remove((user_id, query.data))
        if (user_id, query.data) in processing_accepts_time:
            del processing_accepts_time[(user_id, query.data)]
            
        logger.info(f"订单 {oid} 已被用户 {user_id} 接受")
    except Exception as e:
        logger.error(f"处理订单 {oid} 接单请求时出错: {str(e)}", exc_info=True)
        
        # 从处理集合中移除
        if (user_id, query.data) in processing_accepts:
            processing_accepts.remove((user_id, query.data))
        if (user_id, query.data) in processing_accepts_time:
            del processing_accepts_time[(user_id, query.data)]
            
        await query.answer("Error processing order, please try again later", show_alert=True)

async def on_feedback_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理反馈按钮回调"""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    logger.info(f"收到反馈按钮回调: 用户={user_id}, 数据={data}")
    
    if not is_seller(user_id):
        logger.warning(f"非管理员 {user_id} 尝试提交反馈")
        await query.answer("You are not an admin")
        return
    
    # 先确认回调
    try:    
        await query.answer()
    except Exception as e:
        logger.error(f"确认反馈回调时出错: {str(e)}")
    
    try:
        if data.startswith('done_'):
            oid = int(data.split('_')[1])
            logger.info(f"管理员 {user_id} 标记订单 #{oid} 为已完成")
            
            timestamp = get_china_time()
            execute_query("UPDATE orders SET status=?, completed_at=? WHERE id=? AND accepted_by=?",
                        (STATUS['COMPLETED'], timestamp, oid, str(user_id)))
                        
            try:
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Completed", callback_data="noop")]]))
                logger.info(f"已更新订单 #{oid} 的消息显示为已完成状态")
            except Exception as markup_error:
                logger.error(f"更新已完成标记时出错: {str(markup_error)}")
        
        elif data.startswith('fail_'):
            oid = int(data.split('_')[1])
            logger.info(f"管理员 {user_id} 点击了失败按钮 #{oid}")
            
            # 显示失败原因选项（添加emoji）
            keyboard = [
                [InlineKeyboardButton("🔑 Wrong Password", callback_data=f"reason_wrong_password_{oid}")],
                [InlineKeyboardButton("⏱️ Membership Not Expired", callback_data=f"reason_not_expired_{oid}")],
                [InlineKeyboardButton("❓ Other Reason", callback_data=f"reason_other_{oid}")],
                [InlineKeyboardButton("↩️ Cancel (Clicked by Mistake)", callback_data=f"reason_cancel_{oid}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await query.edit_message_reply_markup(reply_markup=reply_markup)
                # 确保回调被确认
                await query.answer("Please select a reason")
                logger.info(f"已为订单 #{oid} 显示失败原因选项")
            except Exception as markup_error:
                logger.error(f"显示失败原因选项时出错: {str(markup_error)}")
                await query.answer("Error updating options. Please try again.", show_alert=True)
        
        # 处理失败原因选项
        elif data.startswith('reason_'):
            parts = data.split('_')
            # 修复原因类型解析逻辑
            if len(parts) >= 3:
                # 格式为reason_wrong_password_79，需要正确提取原因部分
                reason_type = '_'.join(parts[1:-1])  # 合并中间部分作为原因
                oid = int(parts[-1])  # 订单ID在最后一部分
            else:
                reason_type = "unknown"
                oid = int(parts[-1]) if parts[-1].isdigit() else 0
            
            logger.info(f"管理员 {user_id} 为订单 #{oid} 选择了失败原因: {reason_type}")
            
            # 如果是取消，恢复原始按钮
            if reason_type == "cancel":
                keyboard = [
                    [InlineKeyboardButton("✅ Complete", callback_data=f"done_{oid}"),
                     InlineKeyboardButton("❌ Failed", callback_data=f"fail_{oid}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                try:
                    await query.edit_message_reply_markup(reply_markup=reply_markup)
                    await query.answer("Operation cancelled.")
                    logger.info(f"已取消订单 #{oid} 的失败操作")
                except Exception as cancel_error:
                    logger.error(f"取消失败操作时出错: {str(cancel_error)}")
                return
            
            # 处理其他原因类型
            timestamp = get_china_time()
            
            # 设置失败状态和原因（添加emoji）
            reason_text = ""
            if reason_type == "wrong_password":
                reason_text = "Wrong password"
            elif reason_type == "not_expired":
                reason_text = "Membership not expired"
            elif reason_type == "other":
                reason_text = "Other reason (details pending)"
                # 标记需要额外反馈
                feedback_waiting[user_id] = oid
            else:
                # 处理未知的原因类型
                reason_text = f"Unknown reason: {reason_type}"
            
            # 更新数据库
            execute_query("UPDATE orders SET status=?, completed_at=?, remark=? WHERE id=? AND accepted_by=?",
                        (STATUS['FAILED'], timestamp, reason_text, oid, str(user_id)))
            
            # 执行退款操作
            from modules.database import refund_order
            success, result = refund_order(oid)
            if success:
                logger.info(f"订单退款成功: ID={oid}, 新余额={result}")
            else:
                logger.warning(f"订单退款失败: ID={oid}, 原因={result}")
            
            # 获取原始消息内容
            original_text = query.message.text
            
            # 更新UI - 保留原始消息，仅更改按钮
            try:
                # 初始化keyboard变量，确保在所有情况下都有定义
                keyboard = [[InlineKeyboardButton("❓ Failed", callback_data="noop")]]
                
                if reason_type == "wrong_password":
                    keyboard = [[InlineKeyboardButton("🔑 Failed: Wrong Password", callback_data="noop")]]
                elif reason_type == "not_expired":
                    keyboard = [[InlineKeyboardButton("⏱️ Failed: Membership Not Expired", callback_data="noop")]]
                elif reason_type == "other":
                    keyboard = [[InlineKeyboardButton("❓ Failed: Other Reason", callback_data="noop")]]
                else:
                    keyboard = [[InlineKeyboardButton(f"❓ Failed: {reason_type}", callback_data="noop")]]
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # 保留原始消息文本，只更新按钮
                await query.edit_message_reply_markup(reply_markup=reply_markup)
                
                # 如果是"其他原因"，请求详细反馈
                if reason_type == "other":
                    # 先确认回调，避免"等待中"状态
                    await query.answer("Please provide more details")
                    await query.message.reply_text(
                        "📝 Please provide more details about the failure reason. Your next message will be recorded as feedback."
                    )
                else:
                    # 只显示回调确认，不发送额外消息
                    await query.answer(f"Order marked as failed: {reason_text}")
                
                logger.info(f"已更新订单 #{oid} 的消息显示为失败状态，原因: {reason_text}")
            except Exception as markup_error:
                logger.error(f"更新失败标记时出错: {str(markup_error)}", exc_info=True)
                # 尝试通知用户出错了
                await query.answer("Error updating UI. The order status has been updated.", show_alert=True)
    except ValueError as ve:
        logger.error(f"解析订单ID出错: {str(ve)}")
    except Exception as e:
        logger.error(f"处理反馈按钮回调时出错: {str(e)}", exc_info=True)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文本消息"""
    user_id = update.effective_user.id
    
    # 检查是否等待失败反馈
    if user_id in feedback_waiting:
        oid = feedback_waiting[user_id]
        feedback = update.message.text
        
        execute_query("UPDATE orders SET remark=? WHERE id=?", (feedback, oid))
        del feedback_waiting[user_id]
        
        await update.message.reply_text("Feedback recorded. Thank you.")

async def on_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理统计命令"""
    user_id = update.effective_user.id
    
    if not is_seller(user_id):
        await update.message.reply_text("You are not a seller and cannot use this command.")
        return
    
    # 发送统计选择按钮
    keyboard = [
        [
            InlineKeyboardButton("📅 Today", callback_data="stats_today_personal"),
            InlineKeyboardButton("📅 Yesterday", callback_data="stats_yesterday_personal"),
        ],
        [
            InlineKeyboardButton("📊 This Week", callback_data="stats_week_personal"),
            InlineKeyboardButton("📊 This Month", callback_data="stats_month_personal")
        ]
    ]
    
    # 只有超级管理员（ID: 1878943383）可以查看所有人的统计
    if user_id == 1878943383:
        keyboard.append([
            InlineKeyboardButton("👥 All Sellers", callback_data="stats_all_sellers_menu")
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Please select a time period to view statistics:", reply_markup=reply_markup)

async def on_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理统计回调"""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    if not is_seller(user_id):
        await query.answer("You are not an admin")
        return
    
    await query.answer()
    
    # 处理返回按钮
    if data == "stats_back":
        # 重新显示统计选择按钮
        keyboard = [
            [
                InlineKeyboardButton("📅 Today", callback_data="stats_today_personal"),
                InlineKeyboardButton("📅 Yesterday", callback_data="stats_yesterday_personal"),
            ],
            [
                InlineKeyboardButton("📊 This Week", callback_data="stats_week_personal"),
                InlineKeyboardButton("📊 This Month", callback_data="stats_month_personal")
            ]
        ]
        if user_id == 1878943383:
            keyboard.append([
                InlineKeyboardButton("👥 All Sellers", callback_data="stats_all_sellers_menu")
            ])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Please select a time period to view statistics:", reply_markup=reply_markup)
        return

    # 新增：管理员all sellers日期选择菜单
    if data == "stats_all_sellers_menu":
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        day_before_yesterday = today - timedelta(days=2)
        start_of_week = today - timedelta(days=today.weekday())
        start_of_month = today.replace(day=1)
        keyboard = [
            [
                InlineKeyboardButton(f"{day_before_yesterday.strftime('%Y-%m-%d')}", callback_data=f"stats_all_sellers_{day_before_yesterday}"),
                InlineKeyboardButton(f"{yesterday.strftime('%Y-%m-%d')}", callback_data=f"stats_all_sellers_{yesterday}"),
                InlineKeyboardButton(f"{today.strftime('%Y-%m-%d')}", callback_data=f"stats_all_sellers_{today}")
            ],
            [
                InlineKeyboardButton("本周", callback_data="stats_all_sellers_week"),
                InlineKeyboardButton("本月", callback_data="stats_all_sellers_month")
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data="stats_back")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("请选择要统计的日期：", reply_markup=reply_markup)
        return

    # 新增：管理员all sellers具体日期统计
    if data.startswith("stats_all_sellers_"):
        arg = data[len("stats_all_sellers_"):]
        today = datetime.now().date()
        start_of_week = today - timedelta(days=today.weekday())
        start_of_month = today.replace(day=1)
        if arg == "week":
            await show_all_stats(query, start_of_week.strftime("%Y-%m-%d"), "This Week")
            return
        elif arg == "month":
            await show_all_stats(query, start_of_month.strftime("%Y-%m-%d"), "This Month")
            return
        else:
            # 具体日期
            await show_all_stats(query, arg, arg)
            return
    
    today = datetime.now().date()
    
    if data.startswith('stats_today'):
        date_str = today.strftime("%Y-%m-%d")
        if data.endswith('_all'):
            await show_all_stats(query, date_str, "Today")
        else:
            await show_personal_stats(query, user_id, date_str, "Today")
            
    elif data.startswith('stats_yesterday'):
        yesterday = today - timedelta(days=1)
        date_str = yesterday.strftime("%Y-%m-%d")
        await show_personal_stats(query, user_id, date_str, "Yesterday")
        
    elif data.startswith('stats_week'):
        # 计算本周开始和结束日期
        start_of_week = today - timedelta(days=today.weekday())
        end_of_week = today
        await show_period_stats(query, user_id, start_of_week, end_of_week, "This Week")
        
    elif data.startswith('stats_month'):
        # 计算本月开始和结束日期
        start_of_month = today.replace(day=1)
        end_of_month = today
        
        if data.endswith('_all'):
            await show_all_stats(query, start_of_month.strftime("%Y-%m-%d"), "This Month")
        else:
            await show_period_stats(query, user_id, start_of_month, end_of_month, "This Month")

async def show_personal_stats(query, user_id, date_str, period_text):
    """显示个人统计"""
    # 查询指定日期完成的订单
    completed_orders = execute_query("""
        SELECT package FROM orders 
        WHERE accepted_by = ? AND status = ? AND completed_at LIKE ?
    """, (str(user_id), STATUS['COMPLETED'], f"{date_str}%"), fetch=True)
    
    # 统计各套餐数量
    package_counts = {}
    for order in completed_orders:
        package = order[0]
        package_counts[package] = package_counts.get(package, 0) + 1
    
    # 计算总收入
    total_income = 0
    order_count = 0
    stats_text = []
    
    for package, count in package_counts.items():
        price = TG_PRICES.get(package, 0)
        income = price * count
        stats_text.append(f"{PLAN_LABELS_EN[package]}: {count} x ${price:.2f} = ${income:.2f}")
        total_income += income
        order_count += count
    
    # 发送统计消息
    if stats_text:
        message = (
            f"📊 Your Statistics ({period_text}):\n\n"
            + "\n".join(stats_text) + "\n\n"
            f"Total Orders: {order_count}\n"
            f"Total Earnings: ${total_income:.2f}"
        )
    else:
        message = f"No completed orders found for {period_text}."
    
    # 添加返回按钮
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="stats_back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(message, reply_markup=reply_markup)

async def show_period_stats(query, user_id, start_date, end_date, period_text):
    """显示时间段统计"""
    # 将日期转换为字符串格式
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    
    # 获取该时间段内用户完成的所有订单
    orders = execute_query("""
        SELECT package, completed_at FROM orders 
        WHERE accepted_by = ? AND status = ? 
        AND completed_at >= ? AND completed_at <= ?
    """, (
        str(user_id), STATUS['COMPLETED'], 
        f"{start_str} 00:00:00", f"{end_str} 23:59:59"
    ), fetch=True)
    
    # 按日期和套餐统计
    daily_stats = {}
    package_counts = {}
    
    for package, completed_at in orders:
        # 提取日期部分
        date = completed_at.split()[0]
        
        # 更新每日统计
        if date not in daily_stats:
            daily_stats[date] = {}
        
        if package not in daily_stats[date]:
            daily_stats[date][package] = 0
        
        daily_stats[date][package] += 1
        
        # 更新总计统计
        if package not in package_counts:
            package_counts[package] = 0
        
        package_counts[package] += 1
    
    # 计算总收入和订单数
    total_income = 0
    order_count = 0
    
    # 生成消息
    if daily_stats:
        # 首先按日期排序
        sorted_dates = sorted(daily_stats.keys())
        
        # 生成每日统计
        daily_messages = []
        for date in sorted_dates:
            day_income = 0
            day_count = 0
            day_details = []
            
            for package, count in daily_stats[date].items():
                price = TG_PRICES.get(package, 0)
                income = price * count
                day_income += income
                day_count += count
                day_details.append(f"  {PLAN_LABELS_EN[package]}: {count} x ${price:.2f} = ${income:.2f}")
            
            daily_messages.append(
                f"📅 {date}: {day_count} orders, ${day_income:.2f}\n" +
                "\n".join(day_details)
            )
        
        # 生成总计统计
        summary_lines = []
        for package, count in package_counts.items():
            price = TG_PRICES.get(package, 0)
            income = price * count
            total_income += income
            order_count += count
            summary_lines.append(f"{PLAN_LABELS_EN[package]}: {count} x ${price:.2f} = ${income:.2f}")
        
        # 组合消息
        message = (
            f"📊 {period_text} Statistics ({start_str} to {end_str}):\n\n"
            + "\n\n".join(daily_messages) + "\n\n"
            + "📈 Summary:\n"
            + "\n".join(summary_lines) + "\n\n"
            f"Total Orders: {order_count}\n"
            f"Total Earnings: ${total_income:.2f}"
        )
    else:
        message = f"No completed orders found for {period_text} ({start_str} to {end_str})."
    
    # 消息可能很长，需要检查长度
    if len(message) > 4000:
        message = message[:3950] + "\n...\n(Message truncated due to length limit)"
    
    # 添加返回按钮
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="stats_back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(message, reply_markup=reply_markup)

async def show_all_stats(query, date_str, period_text):
    """显示所有人的统计信息"""
    # 检查是否是超级管理员
    user_id = query.from_user.id
    if user_id != 1878943383:
        await query.answer("You don't have permission to view all sellers' statistics", show_alert=True)
        return
        
    # 查询指定日期所有完成的订单
    if len(date_str) == 10:  # 单日格式 YYYY-MM-DD
        completed_orders = execute_query("""
            SELECT accepted_by, package FROM orders 
            WHERE status = ? AND completed_at LIKE ?
        """, (STATUS['COMPLETED'], f"{date_str}%"), fetch=True)
    else:  # 时间段
        start_str = date_str
        completed_orders = execute_query("""
            SELECT accepted_by, package FROM orders 
            WHERE status = ? AND completed_at >= ?
        """, (STATUS['COMPLETED'], f"{start_str} 00:00:00"), fetch=True)
    
    # 按用户统计
    user_stats = {}
    for accepted_by, package in completed_orders:
        if accepted_by not in user_stats:
            user_stats[accepted_by] = {}
        
        if package not in user_stats[accepted_by]:
            user_stats[accepted_by][package] = 0
            
        user_stats[accepted_by][package] += 1
    
    # 生成消息
    if user_stats:
        all_user_messages = []
        total_all_income = 0
        total_all_orders = 0
        
        for user_id, packages in user_stats.items():
            # 获取用户名
            try:
                user_info = await get_user_info(int(user_id))
                user_name = f"@{user_info['username']}" if user_info['username'] != 'No_Username' else user_info['first_name']
            except:
                user_name = f"User {user_id}"
            
            # 统计该用户的订单
            user_income = 0
            user_orders = 0
            user_details = []
            
            for package, count in packages.items():
                price = TG_PRICES.get(package, 0)
                income = price * count
                user_income += income
                user_orders += count
                user_details.append(f"  {PLAN_LABELS_EN[package]}: {count} x ${price:.2f} = ${income:.2f}")
            
            all_user_messages.append(
                f"👤 {user_name}: {user_orders} orders, ${user_income:.2f}\n" +
                "\n".join(user_details)
            )
            
            total_all_income += user_income
            total_all_orders += user_orders
        
        # 组合消息
        message = (
            f"📊 All Staff Statistics ({period_text}):\n\n"
            + "\n\n".join(all_user_messages) + "\n\n"
            f"Total Staff: {len(user_stats)}\n"
            f"Total Orders: {total_all_orders}\n"
            f"Total Revenue: ${total_all_income:.2f}"
        )
    else:
        message = f"No completed orders found for {period_text}."
    
    # 检查消息长度
    if len(message) > 4000:
        message = message[:3950] + "\n...\n(Message truncated due to length limit)"
    
    # 添加返回按钮
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="stats_back")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(message, reply_markup=reply_markup)

# ===== 推送通知 =====
async def check_and_push_orders():
    """检查并推送新订单"""
    global bot_application
    
    try:
        if not bot_application:
            logger.error("机器人未初始化，无法推送订单")
            return
        
        # 获取未通知的订单
        try:
            unnotified_orders = get_unnotified_orders()
            logger.debug(f"检索到 {len(unnotified_orders) if unnotified_orders else 0} 个未通知的订单")
        except Exception as db_error:
            logger.error(f"获取未通知订单时出错: {str(db_error)}", exc_info=True)
            return
            
        if not unnotified_orders:
            # 没有未通知的订单，直接返回
            return
        
        # 获取活跃卖家
        try:
            seller_ids = get_active_seller_ids()
            logger.debug(f"检索到 {len(seller_ids) if seller_ids else 0} 个活跃卖家")
        except Exception as seller_error:
            logger.error(f"获取活跃卖家时出错: {str(seller_error)}", exc_info=True)
            return
            
        if not seller_ids:
            logger.warning("没有活跃的卖家，无法推送订单")
            return
        
        logger.info(f"找到 {len(seller_ids)} 个活跃卖家")
        
        for order in unnotified_orders:
            try:
                if len(order) < 6:
                    logger.error(f"订单数据格式错误: {order}")
                    continue
                    
                oid, account, password, package, created_at, web_user_id = order
                
                logger.info(f"准备推送订单 #{oid} 给卖家")
                
                # 验证订单是否真实存在
                if not check_order_exists(oid):
                    logger.error(f"订单 #{oid} 不存在于数据库中，但出现在未通知列表中")
                    continue
                
                message = (
                    f"📦 New Order #{oid}\n"
                    f"Account: `{account}`\n"
                    f"Package: {package} month(s)"
                )
                
                # 创建接单按钮 - 确保callback_data格式正确
                callback_data = f'accept_{oid}'
                logger.info(f"创建接单按钮，callback_data: {callback_data}")
                
                keyboard = [[InlineKeyboardButton("Accept", callback_data=callback_data)]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # 向所有卖家发送通知
                success_count = 0
                for seller_id in seller_ids:
                    try:
                        sent_message = await bot_application.bot.send_message(
                            chat_id=seller_id, 
                            text=message, 
                            reply_markup=reply_markup,
                            parse_mode='Markdown'
                        )
                        success_count += 1
                        logger.info(f"成功向卖家 {seller_id} 推送订单 #{oid}, 消息ID: {sent_message.message_id}")
                    except Exception as e:
                        logger.error(f"向卖家 {seller_id} 发送订单 #{oid} 通知失败: {str(e)}", exc_info=True)
                
                if success_count > 0:
                    # 只有成功推送给至少一个卖家时才标记为已通知
                    try:
                        execute_query("UPDATE orders SET notified = 1 WHERE id = ?", (oid,))
                        logger.info(f"订单 #{oid} 已成功推送给 {success_count}/{len(seller_ids)} 个卖家")
                    except Exception as update_error:
                        logger.error(f"更新订单 #{oid} 通知状态时出错: {str(update_error)}", exc_info=True)
                else:
                    logger.error(f"订单 #{oid} 未能成功推送给任何卖家")
            except Exception as e:
                logger.error(f"处理订单通知时出错: {str(e)}", exc_info=True)
    except Exception as e:
        logger.error(f"检查并推送订单时出错: {str(e)}", exc_info=True)

# ===== 通知发送函数 =====
async def send_notification_from_queue(data):
    """根据队列中的数据发送通知"""
    global bot_application
    
    if not bot_application:
        logger.error("机器人应用未初始化，无法发送通知")
        return

    try:
        if data['type'] == 'new_order':
            await send_new_order_notification(data)
        elif data['type'] == 'order_status_change':
            await send_status_change_notification(data)
        elif data['type'] == 'recharge_request':
            await send_recharge_request_notification(data)
        elif data['type'] == 'dispute':
            await send_dispute_notification(data)
        else:
            logger.warning(f"未知的通知类型: {data['type']}")
    except Exception as e:
        logger.error(f"发送通知时出错: {str(e)}", exc_info=True)

# ===== 推送通知函数 =====
def set_order_notified_atomic(oid):
    """原子性地将订单notified字段设为1，只有notified=0时才更新，防止重复推送"""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cursor = conn.cursor()
        placeholder = '%s' if DATABASE_URL.startswith('postgres') else '?'
        cursor.execute(f"UPDATE orders SET notified=1 WHERE id={placeholder} AND notified=0", (oid,))
        affected = cursor.rowcount
        conn.commit()
        return affected > 0
    except Exception as e:
        logger.error(f"原子标记订单 #{oid} 通知状态时出错: {e}", exc_info=True)
        return False
    finally:
        conn.close()

async def send_new_order_notification(data):
    """发送新订单通知到所有卖家"""
    global bot_application
    
    try:
        # 获取新订单详情
        oid = data.get('order_id')
        # 推送前先原子性标记
        if not set_order_notified_atomic(oid):
            logger.info(f"订单 #{oid} 已经被其他进程推送过，跳过")
            return
        account = data.get('account')
        password = data.get('password')
        package = data.get('package')
        
        # 构建消息文本
        message_text = (
            f"📦 New Order #{oid}\n"
            f"Account: `{account}`\n"
            f"Package: {package} month(s)"
        )
        
        # 创建接单按钮
        callback_data = f'accept_{oid}'
        keyboard = [[InlineKeyboardButton("Accept", callback_data=callback_data)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # 向所有卖家发送通知
        seller_ids = get_active_seller_ids()
        if not seller_ids:
            logger.warning("没有活跃的卖家，无法推送订单")
            return
            
        success_count = 0
        for seller_id in seller_ids:
            try:
                sent_message = await bot_application.bot.send_message(
                    chat_id=seller_id, 
                    text=message_text, 
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                success_count += 1
                logger.info(f"成功向卖家 {seller_id} 推送订单 #{oid}, 消息ID: {sent_message.message_id}")
            except Exception as e:
                logger.error(f"向卖家 {seller_id} 发送订单 #{oid} 通知失败: {str(e)}", exc_info=True)
        
        if success_count > 0:
            # 标记订单为已通知
            try:
                execute_query("UPDATE orders SET notified = 1 WHERE id = ?", (oid,))
                logger.info(f"订单 #{oid} 已成功推送给 {success_count}/{len(seller_ids)} 个卖家")
            except Exception as update_error:
                logger.error(f"更新订单 #{oid} 通知状态时出错: {str(update_error)}", exc_info=True)
        else:
            logger.error(f"订单 #{oid} 未能成功推送给任何卖家")
    except Exception as e:
        logger.error(f"发送新订单通知时出错: {str(e)}", exc_info=True)

async def send_status_change_notification(data):
    """发送订单状态变更通知到超级管理员"""
    global bot_application
    
    try:
        # 超级管理员的Telegram ID
        admin_id = 1878943383
        
        # 获取订单状态变更详情
        oid = data.get('order_id')
        status = data.get('status')
        handler_id = data.get('handler_id')
        
        # 构建消息文本
        message_text = (
            f"📢 *Order Status Change Notification* 📢\n\n"
            f"Order #{oid} has been updated to status: {status}\n"
            f"Handler ID: {handler_id}\n"
            f"⏰ 时间: {get_china_time()}\n\n"
            f"Please handle this order accordingly."
        )
        
        # 创建审核按钮
        keyboard = [
            [
                InlineKeyboardButton("✅ 已批准", callback_data=f"approve_status_change:{oid}"),
                InlineKeyboardButton("❌ 已拒绝", callback_data=f"reject_status_change:{oid}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # 发送通知
        await bot_application.bot.send_message(
            chat_id=admin_id,
            text=message_text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

        logger.info(f"已发送订单状态变更 #{oid} 通知到管理员")
    except Exception as e:
        logger.error(f"发送订单状态变更通知时出错: {str(e)}", exc_info=True)

async def send_recharge_request_notification(data):
    """发送充值请求通知到超级管理员"""
    global bot_application
    
    try:
        # 超级管理员的Telegram ID
        admin_id = 1878943383
        
        # 获取充值请求详情
        request_id = data.get('request_id')
        username = data.get('username')
        amount = data.get('amount')
        payment_method = data.get('payment_method')
        proof_image = data.get('proof_image')
        details = data.get('details')
        
        logger.info(f"准备发送充值请求通知: 请求ID={request_id}, 用户={username}, 金额={amount}, 管理员ID={admin_id}")
        
        # 构建消息文本
        message_text = (
            f"📥 <b>新充值请求</b> #{request_id}\n\n"
            f"👤 用户: <code>{username}</code>\n"
            f"💰 金额: <b>{amount} 元</b>\n"
            f"💳 支付方式: {payment_method}\n"
        )

        if details:
            message_text += f"💬 详情: <code>{details}</code>\n"

        message_text += f"⏰ 时间: {get_china_time()}\n\n请审核此充值请求。"
        
        # 创建审核按钮
        keyboard = [
            [
                InlineKeyboardButton("✅ 批准", callback_data=f"approve_recharge:{request_id}"),
                InlineKeyboardButton("❌ 拒绝", callback_data=f"reject_recharge:{request_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # 检查bot是否已初始化
        if not bot_application or not bot_application.bot:
            logger.error(f"无法发送充值请求通知: bot未初始化")
            return
        
        # 发送通知
        try:
            if proof_image:
                # 将URL路径转换为本地文件系统路径
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                relative_path = proof_image.lstrip('/')
                local_image_path = os.path.join(project_root, relative_path)
                
                logger.info(f"尝试从本地路径发送图片: {local_image_path}")
                
                if os.path.exists(local_image_path):
                    try:
                        # 直接发送图片文件
                        with open(local_image_path, 'rb') as photo_file:
                            await bot_application.bot.send_photo(
                                chat_id=admin_id,
                                photo=photo_file,
                                caption=message_text,
                                reply_markup=reply_markup,
                                parse_mode='HTML'
                            )
                        logger.info(f"已成功发送充值请求图片通知到管理员 {admin_id}")
                    except Exception as img_send_error:
                        logger.error(f"发送本地图片失败: {img_send_error}, 回退到纯文本通知", exc_info=True)
                        message_text += f"\n\n⚠️ <i>图片发送失败，请在网页管理界面查看凭证。</i>"
                        await bot_application.bot.send_message(
                            chat_id=admin_id,
                            text=message_text,
                            reply_markup=reply_markup,
                            parse_mode='HTML'
                        )
                else:
                    logger.error(f"图片文件未找到: {local_image_path}, 回退到纯文本通知")
                    message_text += f"\n\n⚠️ <i>图片凭证文件未找到，请在网页管理界面查看。</i>"
                    await bot_application.bot.send_message(
                        chat_id=admin_id,
                        text=message_text,
                        reply_markup=reply_markup,
                        parse_mode='HTML'
                    )
            else:
                # 如果没有支付凭证，只发送文本
                await bot_application.bot.send_message(
                    chat_id=admin_id,
                    text=message_text,
                    reply_markup=reply_markup,
                    parse_mode='HTML'
                )
                logger.info(f"已成功发送无图片充值请求通知到管理员 {admin_id}")
        except Exception as send_error:
            logger.error(f"发送通知到管理员 {admin_id} 失败: {str(send_error)}", exc_info=True)
    except Exception as e:
        logger.error(f"发送充值请求通知时出错: {str(e)}", exc_info=True)

async def send_dispute_notification(data):
    """发送质疑订单通知到卖家"""
    global bot_application
    try:
        seller_id = data.get('seller_id')
        oid = data.get('order_id')
        account = data.get('account')
        password = data.get('password')
        package = data.get('package')
        message_text = (
            f"⚠️ <b>Order Dispute</b> ⚠️\n\n"
            f"Order #{oid} has been disputed by the user.\n"
            f"Account: <code>{account}</code>\n"
            f"Password: <code>{password}</code>\n"
            f"Package: {package} month(s)\n\n"
            f"Please check and handle this order as soon as possible."
        )
        
        # 添加反馈按钮
        keyboard = [
            [
                InlineKeyboardButton("✅ Complete", callback_data=f"done_{oid}"),
                InlineKeyboardButton("❌ Failed", callback_data=f"fail_{oid}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await bot_application.bot.send_message(
            chat_id=seller_id,
            text=message_text,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
        logger.info(f"已向卖家 {seller_id} 发送订单质疑通知 #{oid}")
    except Exception as e:
        logger.error(f"发送订单质疑通知时出错: {str(e)}", exc_info=True)

# ===== 主函数 =====
def run_bot(notification_queue):
    """在一个新事件循环中运行Telegram机器人"""
    global BOT_LOOP
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    BOT_LOOP = loop  # 保存主事件循环
    loop.run_until_complete(bot_main(notification_queue))


async def bot_main(notification_queue):
    """机器人的主异步函数"""
    global bot_application
    
    logger.info("正在启动Telegram机器人...")
    
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
        
        # 添加处理程序
        bot_application.add_handler(CommandHandler("start", on_start))
        bot_application.add_handler(CommandHandler("seller", on_admin_command))
        bot_application.add_handler(CommandHandler("stats", on_stats))
        
        # 添加测试命令处理程序
        bot_application.add_handler(CommandHandler("test", on_test))
        
        # 添加回调处理程序，确保正确处理各种回调
        accept_handler = CallbackQueryHandler(on_accept, pattern="^accept_")
        bot_application.add_handler(accept_handler)
        
        feedback_handler = CallbackQueryHandler(on_feedback_button, pattern="^(done|fail|reason)_")
        bot_application.add_handler(feedback_handler)
        
        stats_handler = CallbackQueryHandler(on_stats_callback, pattern="^stats_")
        bot_application.add_handler(stats_handler)
        
        # 添加充值请求回调处理程序
        recharge_handler = CallbackQueryHandler(on_callback_query)
        bot_application.add_handler(recharge_handler)
        
        # 添加文本消息处理程序
        bot_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        
        logger.info("已添加所有处理程序")
        
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
            await bot_application.bot.set_webhook(
                url=webhook_url,
                allowed_updates=Update.ALL_TYPES
            )
        else:
            logger.warning("无法获取公开URL，未设置webhook。机器人可能无法接收更新。")

        # 启动后台任务
        logger.info("启动后台任务...")
        asyncio.create_task(periodic_order_check())
        asyncio.create_task(process_notification_queue(notification_queue))
        
        logger.info("Telegram机器人主循环已启动，等待更新...")
        
        # 保持此协程运行以使后台任务可以执行
        while True:
            await asyncio.sleep(3600) # 每小时唤醒一次，但主要目的是保持运行

    except Exception as e:
        logger.critical(f"Telegram机器人主函数 `bot_main` 发生严重错误: {str(e)}", exc_info=True)

# 添加错误处理函数
async def error_handler(update, context):
    """处理Telegram机器人的错误"""
    logger.error(f"Telegram机器人发生错误: {context.error}", exc_info=context.error)
    
    # 尝试获取错误来源
    if update:
        if update.effective_message:
            logger.error(f"错误发生在消息: {update.effective_message.text}")
        elif update.callback_query:
            logger.error(f"错误发生在回调查询: {update.callback_query.data}")
    
    # 如果是回调查询错误，尝试回复用户
    try:
        if update and update.callback_query:
            await update.callback_query.answer("An error occurred. Please try again later.", show_alert=True)
    except Exception as e:
        logger.error(f"尝试回复错误通知失败: {str(e)}")

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
            logger.info(f"从队列中获取到通知任务: {data.get('type')}")
            await send_notification_from_queue(data)
            queue.task_done()
        except asyncio.CancelledError:
            logger.info("通知队列处理器被取消。")
            break
        except Exception as e:
            # 捕获并记录所有其他异常
            logger.error(f"处理通知队列任务时发生未知错误: {repr(e)}", exc_info=True)
            # 等待一会避免在持续出错时刷屏
            await asyncio.sleep(5)
    
def restricted(func):
    """限制只有卖家才能访问的装饰器"""
    @wraps(func)
    async def wrapped(update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_seller(user_id):
            logger.warning(f"未经授权的访问: {user_id}")
            await update.message.reply_text("Sorry, you are not authorized to use this bot.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def get_order_by_id(order_id):
    """根据ID获取订单信息（返回字典）"""
    try:
        conn = get_db_connection()
        if not conn:
            logger.error(f"获取订单 {order_id} 信息时无法获取数据库连接")
            return None
        cursor = conn.cursor()
        placeholder = '%s' if DATABASE_URL.startswith('postgres') else '?'
        cursor.execute(f"SELECT * FROM orders WHERE id = {placeholder}", (order_id,))
        order = cursor.fetchone()
        conn.close()
        if order:
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, order))
        return None
    except Exception as e:
        logger.error(f"获取订单 {order_id} 信息时出错: {str(e)}", exc_info=True)
        return None

def check_order_exists(order_id):
    """检查数据库中是否存在指定ID的订单"""
    try:
        placeholder = '%s' if DATABASE_URL.startswith('postgres') else '?'
        result = execute_query(f"SELECT COUNT(*) FROM orders WHERE id = {placeholder}", (order_id,), fetch=True)
        count = result[0][0] if result else 0
        if count == 0:
            logger.warning(f"订单 {order_id} 在数据库中不存在")
        return count > 0
    except Exception as e:
        logger.error(f"检查订单 {order_id} 是否存在时出错: {str(e)}", exc_info=True)
        return False

def update_order_status(order_id, status, handler_id=None):
    """更新订单状态"""
    try:
        conn = get_db_connection()
        if not conn:
            logger.error(f"更新订单 {order_id} 状态时无法获取数据库连接")
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
        return True
    except Exception as e:
        logger.error(f"更新订单 {order_id} 状态时出错: {str(e)}", exc_info=True)
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