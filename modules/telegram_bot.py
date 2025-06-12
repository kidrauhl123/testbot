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
    STATUS_TEXT_ZH, TG_PRICES, WEB_PRICES, SELLER_CHAT_IDS
)
from modules.database import (
    get_order_details, accept_order_atomic, execute_query, 
    get_unnotified_orders, get_active_seller_ids
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

# ===== 全局 Bot 实例 =====
bot_application = None

# 跟踪等待额外反馈的订单
feedback_waiting = {}

# 用户信息缓存
user_info_cache = {}

# ===== TG 辅助函数 =====
def is_seller(chat_id):
    """检查用户是否为已授权的卖家"""
    # 只从数据库中获取卖家信息，因为环境变量中的卖家已经同步到数据库
    return chat_id in get_active_seller_ids()

async def get_user_info(user_id):
    """获取Telegram用户信息并缓存"""
    global bot_application, user_info_cache
    
    if not bot_application:
        return {"id": user_id, "username": "Unknown", "first_name": "Unknown", "last_name": ""}
    
    # 检查缓存
    if user_id in user_info_cache:
        return user_info_cache[user_id]
    
    try:
        user = await bot_application.bot.get_chat(user_id)
        user_info = {
            "id": user_id,
            "username": user.username or "No_Username",
            "first_name": user.first_name or "Unknown",
            "last_name": user.last_name or ""
        }
        user_info_cache[user_id] = user_info
        return user_info
    except Exception as e:
        logger.error(f"Failed to get user info for {user_id}: {e}")
        default_info = {"id": user_id, "username": "Unknown", "first_name": "Unknown", "last_name": ""}
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
    
    for key, start_time in list(processing_accepts_time.items()):
        # 如果请求处理时间超过30秒，认为超时
        if current_time - start_time > 30:
            timeout_keys.append(key)
    
    # 从集合中移除超时的请求
    for key in timeout_keys:
        if key in processing_accepts:
            processing_accepts.remove(key)
        if key in processing_accepts_time:
            del processing_accepts_time[key]
        logger.warning(f"清理超时的接单请求: {key}")

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
    if active_orders_count >= 2:
        status_icon = "🔴"
        status_message = f"{status_icon} *Seller Status:* {active_orders_count}/2 active orders\n⚠️ *Maximum limit reached.* Please complete existing orders first."
    else:
        status_icon = "🟢" 
        status_message = f"{status_icon} *Seller Status:* {active_orders_count}/2 active orders\n✅ *You can accept new orders.*"
    
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
async def on_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理接单回调"""
    global processing_accepts, processing_accepts_time
    
    try:
        query = update.callback_query
        user_id = query.from_user.id
        data = query.data
        
        # 添加更详细的日志
        logger.info(f"收到接单回调: 用户ID={user_id}, 回调数据={data}, 消息ID={query.message.message_id}")
        print(f"DEBUG: 收到接单回调: 用户ID={user_id}, 回调数据={data}")
        
        # 立即确认回调，避免Telegram显示等待状态
        try:
            await query.answer("Processing your request...")
            logger.info("已确认回调请求")
        except Exception as e:
            logger.error(f"确认回调时出错: {str(e)}", exc_info=True)
            print(f"ERROR: 确认回调时出错: {str(e)}")
        
        # 清理超时的处理中请求
        await cleanup_processing_accepts()
        
        # 检查用户是否为卖家
        if not is_seller(user_id):
            logger.warning(f"非卖家 {user_id} 尝试接单")
            try:
                await query.answer("You are not a seller and cannot accept orders", show_alert=True)
            except Exception as e:
                logger.error(f"回复非卖家时出错: {str(e)}")
            return
        
        # 检查是否是接单回调
        if not data.startswith('accept_'):
            logger.warning(f"无效的接单回调数据: {data}")
            return
        
        # 简化流程，使用更直接的方式处理接单
        try:
            # 解析订单ID
            oid = int(data.split('_')[1])
            logger.info(f"解析订单ID: {oid}")
            print(f"DEBUG: 解析订单ID: {oid}")
            
            # 尝试接单
            logger.info(f"卖家 {user_id} 尝试接单 #{oid}")
            print(f"DEBUG: 卖家 {user_id} 尝试接单 #{oid}")
            success, message = accept_order_atomic(oid, user_id)
            logger.info(f"接单结果: 成功={success}, 消息={message}")
            print(f"DEBUG: 接单结果: 成功={success}, 消息={message}")
            
            if success:
                # 接单成功
                logger.info(f"卖家 {user_id} 成功接单 #{oid}")
                print(f"DEBUG: 卖家 {user_id} 成功接单 #{oid}")
                
                # 获取订单详情
                order = get_order_details(oid)
                if not order or len(order) == 0:
                    logger.error(f"找不到订单 #{oid} 的详情")
                    print(f"ERROR: 找不到订单 #{oid} 的详情")
                    await query.edit_message_text(f"Error: Order #{oid} details not found")
                    return
                
                # 发送成功提示
                await query.answer("Order accepted successfully!", show_alert=True)
                
                # 更新消息
                order = order[0]
                account, password, package = order[1], order[2], order[3]
                
                keyboard = [
                    [InlineKeyboardButton("✅ Complete", callback_data=f"done_{oid}"),
                     InlineKeyboardButton("❌ Failed", callback_data=f"fail_{oid}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    f"🎉 Order #{oid} - You've accepted this order\n\n"
                    f"👤 Account: `{account}`\n"
                    f"🔑 Password: `{password}`\n"
                    f"📦 Package: {package} month(s)\n"
                    f"💰 Payment: ${TG_PRICES[package]}",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                logger.info(f"已更新订单 #{oid} 的消息显示为已接单状态")
                print(f"DEBUG: 已更新订单 #{oid} 的消息显示为已接单状态")
            else:
                # 接单失败
                logger.warning(f"订单 #{oid} 接单失败: {message}")
                print(f"DEBUG: 订单 #{oid} 接单失败: {message}")
                await query.answer(message, show_alert=True)
                
                if "already taken" in message or "not found" in message:
                    await query.edit_message_text(f"⚠️ Order #{oid} has already been taken by someone else or does not exist.")
        except ValueError as ve:
            logger.error(f"解析订单ID出错: {str(ve)}", exc_info=True)
            print(f"ERROR: 解析订单ID出错: {str(ve)}")
            await query.answer("Invalid order ID", show_alert=True)
        except Exception as e:
            logger.error(f"处理接单时发生未知错误: {str(e)}", exc_info=True)
            print(f"ERROR: 处理接单时发生未知错误: {str(e)}")
            await query.answer("An error occurred. Please try again.", show_alert=True)
    except Exception as outer_error:
        logger.critical(f"接单回调函数外层出错: {str(outer_error)}", exc_info=True)
        print(f"CRITICAL: 接单回调函数外层出错: {str(outer_error)}")

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
    
    # 如果是总管理员，添加查看所有人统计的选项
    if user_id in get_active_seller_ids() and get_active_seller_ids().index(user_id) == 0:
        keyboard.append([
            InlineKeyboardButton("👥 All Sellers Today", callback_data="stats_today_all"),
            InlineKeyboardButton("👥 All Sellers This Month", callback_data="stats_month_all")
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
    
    await query.edit_message_text(message)

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
    
    await query.edit_message_text(message)

async def show_all_stats(query, date_str, period_text):
    """显示所有人的统计信息"""
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
    
    await query.edit_message_text(message)

# ===== 推送通知 =====
async def check_and_push_orders():
    """检查并推送新订单"""
    global bot_application
    
    try:
        if not bot_application:
            logger.error("机器人未初始化，无法推送订单")
            print("ERROR: 机器人未初始化，无法推送订单")
            return
        
        # 获取未通知的订单
        unnotified_orders = get_unnotified_orders()
        if not unnotified_orders:
            # 没有未通知的订单，直接返回
            return
        
        # 获取活跃卖家
        seller_ids = get_active_seller_ids()
        if not seller_ids:
            logger.warning("没有活跃的卖家，无法推送订单")
            print("WARNING: 没有活跃的卖家，无法推送订单")
            return
        
        logger.info(f"找到 {len(seller_ids)} 个活跃卖家")
        print(f"DEBUG: 找到 {len(seller_ids)} 个活跃卖家: {seller_ids}")
        
        for order in unnotified_orders:
            try:
                oid, account, password, package, created_at, web_user_id = order
                
                logger.info(f"准备推送订单 #{oid} 给卖家")
                print(f"DEBUG: 准备推送订单 #{oid} 给卖家")
                
                user_info = f" from web user: {web_user_id}" if web_user_id else ""
                
                message = (
                    f"📢 New Order #{oid}{user_info}\n"
                    f"Account: `{account}`\n"
                    f"Password: `********` (hidden until accepted)\n"
                    f"Package: {package} month(s)"
                )
                
                # 创建接单按钮
                keyboard = [[InlineKeyboardButton("Accept", callback_data=f'accept_{oid}')]]
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
                        print(f"DEBUG: 成功向卖家 {seller_id} 推送订单 #{oid}, 消息ID: {sent_message.message_id}")
                    except Exception as e:
                        logger.error(f"向卖家 {seller_id} 发送订单 #{oid} 通知失败: {str(e)}", exc_info=True)
                        print(f"ERROR: 向卖家 {seller_id} 发送订单 #{oid} 通知失败: {str(e)}")
                
                if success_count > 0:
                    # 只有成功推送给至少一个卖家时才标记为已通知
                    execute_query("UPDATE orders SET notified = 1 WHERE id = ?", (oid,))
                    logger.info(f"订单 #{oid} 已成功推送给 {success_count}/{len(seller_ids)} 个卖家")
                    print(f"DEBUG: 订单 #{oid} 已成功推送给 {success_count}/{len(seller_ids)} 个卖家")
                else:
                    logger.error(f"订单 #{oid} 未能成功推送给任何卖家")
                    print(f"ERROR: 订单 #{oid} 未能成功推送给任何卖家")
            except Exception as e:
                logger.error(f"处理订单通知时出错: {str(e)}", exc_info=True)
                print(f"ERROR: 处理订单通知时出错: {str(e)}")
    except Exception as e:
        logger.error(f"检查并推送订单时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 检查并推送订单时出错: {str(e)}")

# ===== 通知发送函数 =====
async def send_notification_from_queue(data):
    """根据队列中的数据发送通知"""
    global bot_application
    if not bot_application:
        logger.error("机器人未初始化，无法发送通知")
        return

    try:
        notification_type = data.get('type')
        seller_id = data.get('seller_id')
        oid = data.get('order_id')
        account = data.get('account')
        password = data.get('password')
        package = data.get('package')

        message = ""
        if notification_type == 'dispute':
            message = (
                f"⚠️ *Order Dispute Notification* ⚠️\n\n"
                f"Order #{oid} has been disputed by the buyer.\n"
                f"Account: `{account}`\n"
                f"Password: `{password}`\n"
                f"Package: {package} month(s)\n\n"
                f"Please handle this issue and update the status."
            )
        elif notification_type == 'urge':
            accepted_at = data.get('accepted_at')
            message = (
                f"🔔 *Order Urge Notification* 🔔\n\n"
                f"The buyer is urging for the completion of order #{oid}.\n"
                f"Account: `{account}`\n"
                f"Password: `{password}`\n"
                f"Package: {package} month(s)\n"
                f"Accepted at: {accepted_at}\n\n"
                f"Please process this order quickly."
            )
        else:
            logger.warning(f"未知的通知类型: {notification_type}")
            return
        
        keyboard = [
            [InlineKeyboardButton("✅ Mark as Complete", callback_data=f"done_{oid}"),
             InlineKeyboardButton("❌ Mark as Failed", callback_data=f"fail_{oid}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await bot_application.bot.send_message(
            chat_id=seller_id,
            text=message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
        logger.info(f"成功向 {seller_id} 发送了 {notification_type} 通知 (订单 #{oid})")

    except Exception as e:
        logger.error(f"从队列发送通知时出错: {e}", exc_info=True)


# ===== 主函数 =====
def run_bot(notification_queue):
    """在一个新事件循环中运行Telegram机器人"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot_main(notification_queue))


async def bot_main(notification_queue):
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
        
        # 添加处理程序
        bot_application.add_handler(CommandHandler("start", on_start))
        bot_application.add_handler(CommandHandler("seller", on_admin_command))
        bot_application.add_handler(CommandHandler("stats", on_stats))
        
        # 添加回调处理程序，确保正确处理各种回调
        bot_application.add_handler(CallbackQueryHandler(on_accept, pattern="^accept_"))
        bot_application.add_handler(CallbackQueryHandler(on_feedback_button, pattern="^(done|fail|reason)_"))
        bot_application.add_handler(CallbackQueryHandler(on_stats_callback, pattern="^stats_"))
        
        # 添加文本消息处理程序
        bot_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        
        logger.info("已添加所有处理程序")
        print("DEBUG: 已添加所有处理程序")
        
        # 添加错误处理程序
        bot_application.add_error_handler(error_handler)
        
        # 启动机器人
        logger.info("初始化机器人...")
        print("DEBUG: 初始化机器人...")
        await bot_application.initialize()
        
        logger.info("启动机器人轮询...")
        print("DEBUG: 启动机器人轮询...")
        await bot_application.updater.start_polling(allowed_updates=["message", "callback_query"])
        
        logger.info("Telegram机器人启动成功")
        print("DEBUG: Telegram机器人启动成功")
        
        # 启动后台任务
        order_check_task = asyncio.create_task(periodic_order_check())
        notification_task = asyncio.create_task(process_notification_queue(notification_queue))
        
        logger.info("进入主循环保持运行")
        print("DEBUG: 进入主循环保持运行")
        
        # 等待任务完成
        await asyncio.gather(order_check_task, notification_task)
    except Exception as e:
        logger.critical(f"Telegram机器人启动失败: {str(e)}", exc_info=True)
        print(f"CRITICAL: Telegram机器人启动失败: {str(e)}")

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
    return wrapped 