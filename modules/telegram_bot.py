import asyncio
import threading
import logging
from datetime import datetime, timedelta
from collections import defaultdict
import time

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
    BOT_TOKEN, ADMIN_CHAT_IDS, STATUS, PLAN_LABELS_EN, 
    TG_PRICES, DATABASE_URL, user_info_cache, user_languages,
    feedback_waiting, notified_orders
)
import modules.constants as constants
from modules.database import execute_query, accept_order_atomic, get_order_details

# 设置日志
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# ===== 全局 Bot 实例 =====
bot_application = None

# ===== TG 辅助函数 =====
def is_telegram_admin(chat_id):
    """检查是否为管理员"""
    return chat_id in ADMIN_CHAT_IDS

async def get_user_info(user_id):
    """获取Telegram用户信息并缓存"""
    global bot_application, user_info_cache
    
    if not bot_application:
        return {"id": user_id, "username": "Unknown", "first_name": "Unknown"}
    
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
        default_info = {"id": user_id, "username": "Unknown", "first_name": "Unknown"}
        user_info_cache[user_id] = default_info
        return default_info

# ===== TG 命令处理 =====
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开始命令处理"""
    user_id = update.effective_user.id
    
    if is_telegram_admin(user_id):
        await update.message.reply_text(
            "Welcome back, Admin! Use the following commands:\n"
            "/seller - Show seller specific commands\n"
            "/stats - View statistics"
        )
    else:
        await update.message.reply_text(
            "Welcome! You are not an admin and cannot use this bot's features."
        )

async def on_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理管理员命令"""
    user_id = update.effective_user.id
    
    if not is_telegram_admin(user_id):
        await update.message.reply_text("You are not an admin and cannot use this command.")
        return
    
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
        await update.message.reply_text("📋 Pending Orders:")
        for order in new_orders:
            oid, account, password, package, created_at = order
            keyboard = [[InlineKeyboardButton("Accept", callback_data=f"accept_{oid}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"Order #{oid} - {created_at}\n"
                f"Account: `{account}`\n"
                f"Password: `{password}`\n"
                f"Package: {package} month(s)",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
    else:
        await update.message.reply_text("No pending orders at the moment.")
    
    # 发送我的订单
    if my_orders:
        await update.message.reply_text("🔄 My Active Orders:")
        for order in my_orders:
            oid, account, password, package, status = order
            
            if status == STATUS['ACCEPTED']:
                keyboard = [
                    [InlineKeyboardButton("✅ Complete", callback_data=f"done_{oid}"),
                     InlineKeyboardButton("❌ Failed", callback_data=f"fail_{oid}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(
                    f"Order #{oid}\n"
                    f"Account: `{account}`\n"
                    f"Password: `{password}`\n"
                    f"Package: {package} month(s)",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )

# ===== TG 回调处理 =====
async def on_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理接单回调"""
    query = update.callback_query
    user_id = query.from_user.id
    
    logger.info(f"收到接单回调: 用户={user_id}, 数据={query.data}")
    
    if not is_telegram_admin(user_id):
        logger.warning(f"非管理员 {user_id} 尝试接单")
        await query.answer("You are not an admin and cannot accept orders")
        return
    
    # 先确认回调
    try:
        await query.answer()
    except Exception as e:
        logger.error(f"确认回调时出错: {str(e)}")
    
    data = query.data
    if data.startswith('accept_'):
        try:
            oid = int(data.split('_')[1])
            logger.info(f"管理员 {user_id} 尝试接单 #{oid}")
            
            # 尝试接单
            success, message = accept_order_atomic(oid, user_id)
            
            if success:
                logger.info(f"管理员 {user_id} 成功接单 #{oid}")
                
                # 更新消息展示
                try:
                    order = get_order_details(oid)
                    if not order:
                        logger.error(f"找不到订单 #{oid} 的详情")
                        await query.edit_message_text(f"Error: Order #{oid} details not found")
                        return
                        
                    order = order[0]
                    account, password, package = order[1], order[2], order[3]
                    
                    keyboard = [
                        [InlineKeyboardButton("✅ Complete", callback_data=f"done_{oid}"),
                         InlineKeyboardButton("❌ Failed", callback_data=f"fail_{oid}")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    await query.edit_message_text(
                        f"Order #{oid} - You've accepted this order\n"
                        f"Account: `{account}`\n"
                        f"Password: `{password}`\n"
                        f"Package: {package} month(s)",
                        reply_markup=reply_markup,
                        parse_mode='Markdown'
                    )
                    logger.info(f"已更新订单 #{oid} 的消息显示为已接单状态")
                except Exception as update_error:
                    logger.error(f"更新接单消息时出错: {str(update_error)}", exc_info=True)
                    try:
                        await query.edit_message_text(
                            f"Order #{oid} accepted, but there was an error updating the message. The order is still assigned to you."
                        )
                    except:
                        pass
            else:
                logger.warning(f"订单 #{oid} 接单失败: {message}")
                try:
                    # 根据不同的失败原因显示不同的消息
                    if "2 active orders" in message:
                        await query.edit_message_reply_markup(
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ " + message, callback_data="noop")]])
                        )
                        # 同时发送一条单独的消息提醒
                        await query.message.reply_text(message)
                    else:
                        await query.edit_message_reply_markup(
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Already taken by another admin", callback_data="noop")]])
                        )
                except Exception as markup_error:
                    logger.error(f"更新失败标记时出错: {str(markup_error)}")
        except ValueError as ve:
            logger.error(f"解析订单ID出错: {str(ve)}")
        except Exception as e:
            logger.error(f"接单处理出错: {str(e)}", exc_info=True)
async def on_feedback_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理完成/失败回调"""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    logger.info(f"收到反馈按钮回调: 用户={user_id}, 数据={data}")
    
    if not is_telegram_admin(user_id):
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
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
            logger.info(f"管理员 {user_id} 标记订单 #{oid} 为失败")
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            execute_query("UPDATE orders SET status=?, completed_at=? WHERE id=? AND accepted_by=?",
                        (STATUS['FAILED'], timestamp, oid, str(user_id)))
            
            # 获取原始订单信息并请求反馈
            order = get_order_details(oid)
            if order:
                feedback_waiting[user_id] = oid
                
                try:
                    await query.edit_message_reply_markup(
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Failed", callback_data="noop")]])
                    )
                    await query.message.reply_text(
                        "Please provide a reason for the failure. Your next message will be recorded as feedback."
                    )
                    logger.info(f"已请求管理员 {user_id} 为失败订单 #{oid} 提供反馈")
                except Exception as reply_error:
                    logger.error(f"请求反馈时出错: {str(reply_error)}")
            else:
                logger.error(f"找不到订单 #{oid} 的详情，无法请求反馈")
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
    
    if not is_telegram_admin(user_id):
        await update.message.reply_text("You are not an admin and cannot use this command.")
        return
    
    # 发送统计选择按钮
    keyboard = [
        [
            InlineKeyboardButton("Today", callback_data="stats_today_personal"),
            InlineKeyboardButton("Yesterday", callback_data="stats_yesterday_personal"),
        ],
        [
            InlineKeyboardButton("This Week", callback_data="stats_week_personal"),
            InlineKeyboardButton("This Month", callback_data="stats_month_personal")
        ]
    ]
    
    # 如果是总管理员，添加查看所有人统计的选项
    if user_id in ADMIN_CHAT_IDS and ADMIN_CHAT_IDS.index(user_id) == 0:
        keyboard.append([
            InlineKeyboardButton("All Staff Today", callback_data="stats_today_all"),
            InlineKeyboardButton("All Staff This Month", callback_data="stats_month_all")
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Please select a time period to view statistics:", reply_markup=reply_markup)

async def on_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理统计回调"""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    if not is_telegram_admin(user_id):
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
    """检查新订单并推送给管理员"""
    global notified_orders, bot_application
    
    logger.info("开始检查新订单...")
    logger.info(f"当前管理员列表: {ADMIN_CHAT_IDS}")
    
    # 检查机器人实例是否初始化
    if bot_application is None or not hasattr(bot_application, 'bot'):
        logger.error("Telegram机器人未初始化或实例不完整")
        return
    
    # 检查是否有有效的request对象
    if not hasattr(bot_application.bot, 'request') or bot_application.bot.request is None:
        logger.error("Telegram机器人request对象不可用，尝试重新连接")
        try:
            await bot_application.bot.initialize()
            logger.info("重新初始化机器人成功")
        except Exception as e:
            logger.error(f"重新初始化机器人失败: {str(e)}", exc_info=True)
            return
    
    try:
        # 使用数据库级别的锁来防止并发问题
        with constants.notified_orders_lock:
            # 直接在数据库中查询并更新，使用事务确保原子性
            if DATABASE_URL.startswith('postgres'):
                # PostgreSQL版本
                from urllib.parse import urlparse
                import psycopg2
                
                url = urlparse(DATABASE_URL)
                conn = psycopg2.connect(
                    dbname=url.path[1:],
                    user=url.username,
                    password=url.password,
                    host=url.hostname,
                    port=url.port
                )
                cursor = conn.cursor()
                
                try:
                    # 开始事务
                    cursor.execute("BEGIN")
                    
                    # 查询未通知的订单并立即标记为已通知（原子操作）
                    cursor.execute("""
                        UPDATE orders 
                        SET notified = 1 
                        WHERE status = %s AND notified = 0
                        RETURNING id, account, password, package, created_at, web_user_id
                    """, (STATUS['SUBMITTED'],))
                    
                    new_orders = cursor.fetchall()
                    
                    # 提交事务
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    raise e
                finally:
                    cursor.close()
                    conn.close()
            else:
                # SQLite版本 - 使用两步操作但加强检查
                import sqlite3
                conn = sqlite3.connect("orders.db")
                cursor = conn.cursor()
                
                try:
                    # 开启事务
                    cursor.execute("BEGIN EXCLUSIVE")
                    
                    # 查询未通知的订单
                    cursor.execute("""
                        SELECT id, account, password, package, created_at, web_user_id 
                        FROM orders 
                        WHERE status = ? AND notified = 0
                    """, (STATUS['SUBMITTED'],))
                    
                    new_orders = cursor.fetchall()
                    
                    # 立即更新这些订单为已通知
                    if new_orders:
                        order_ids = [order[0] for order in new_orders]
                        placeholders = ','.join('?' * len(order_ids))
                        cursor.execute(f"""
                            UPDATE orders 
                            SET notified = 1 
                            WHERE id IN ({placeholders})
                        """, order_ids)
                    
                    # 提交事务
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    raise e
                finally:
                    cursor.close()
                    conn.close()
            
            if not new_orders:
                logger.debug("没有新订单需要通知")
                return
                
            logger.info(f"发现 {len(new_orders)} 个新订单需要通知")
            
            # 更新内存缓存
            for order in new_orders:
                notified_orders.add(order[0])
        
        # 推送通知给所有管理员（在锁外执行，避免长时间持锁）
        if not ADMIN_CHAT_IDS:
            logger.error("管理员列表为空，无法发送通知")
            return
        
        # 记录发送失败的订单，以便后续重试
        failed_notifications = []
        
        for admin_id in ADMIN_CHAT_IDS:
            try:
                logger.info(f"向管理员 {admin_id} 推送新订单通知")
                for order in new_orders:
                    oid, account, password, package, created_at, web_user = order
                    
                    # 创建接单按钮
                    keyboard = [[InlineKeyboardButton("Accept", callback_data=f"accept_{oid}")]]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    
                    # 发送消息
                    try:
                        await bot_application.bot.send_message(
                            chat_id=admin_id,
                            text=f"🆕 New Order #{oid} - {created_at}\n"
                                 f"From: {web_user or 'Unknown'}\n"
                                 f"Account: `{account}`\n"
                                 f"Password: `{password}`\n"
                                 f"Package: {package} month(s)",
                            reply_markup=reply_markup,
                            parse_mode='Markdown'
                        )
                        logger.info(f"已向管理员 {admin_id} 发送订单 #{oid} 的通知")
                    except Exception as msg_error:
                        logger.error(f"向管理员 {admin_id} 发送订单 #{oid} 通知失败: {str(msg_error)}")
                        if (oid, admin_id) not in failed_notifications:
                            failed_notifications.append((oid, admin_id))
            except Exception as e:
                logger.error(f"向管理员 {admin_id} 发送通知失败: {str(e)}", exc_info=True)
        
        # 如果有发送失败的通知，可以考虑回滚或记录
        if failed_notifications:
            logger.warning(f"有 {len(failed_notifications)} 个通知发送失败")
            # 这里可以选择是否要回滚这些订单的notified状态
            # 但通常不建议回滚，因为可能会导致重复通知
            
    except Exception as e:
        logger.error(f"检查和推送订单时出错: {str(e)}", exc_info=True)
# ===== 主函数 =====
async def run_bot():
    """运行Telegram机器人"""
    global bot_application
    
    logger.info(f"正在启动Telegram机器人，管理员ID: {ADMIN_CHAT_IDS}")
    
    # 主循环，确保即使出错也会尝试重启
    restart_count = 0
    max_restarts = 5
    
    while restart_count < max_restarts:
        try:
            # 如果不是第一次启动，等待一段时间再重试
            if restart_count > 0:
                wait_time = min(30, restart_count * 5)  # 最多等待30秒
                logger.info(f"正在尝试第 {restart_count} 次重新启动，等待 {wait_time} 秒...")
                await asyncio.sleep(wait_time)
            
            # 确保之前的实例已清理
            if bot_application is not None:
                try:
                    logger.info("尝试清理之前的机器人实例")
                    if hasattr(bot_application, 'updater') and bot_application.updater:
                        await bot_application.updater.stop()
                    if hasattr(bot_application, 'stop'):
                        await bot_application.stop()
                    if hasattr(bot_application, 'shutdown'):
                        await bot_application.shutdown()
                except Exception as cleanup_error:
                    logger.error(f"清理之前的机器人实例时出错: {str(cleanup_error)}")
                bot_application = None
            
            # 创建应用
            logger.info("创建新的机器人应用实例")
            bot_application = ApplicationBuilder().token(BOT_TOKEN).build()
            
            # 添加处理程序
            bot_application.add_handler(CommandHandler("start", on_start))
            bot_application.add_handler(CommandHandler("seller", on_admin_command))
            bot_application.add_handler(CommandHandler("stats", on_stats))
            bot_application.add_handler(CallbackQueryHandler(on_accept, pattern="^accept_"))
            bot_application.add_handler(CallbackQueryHandler(on_feedback_button, pattern="^(done|fail)_"))
            bot_application.add_handler(CallbackQueryHandler(on_stats_callback, pattern="^stats_"))
            bot_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
            
            # 启动机器人
            logger.info("初始化机器人...")
            await bot_application.initialize()
            logger.info("启动机器人...")
            await bot_application.start()
            logger.info("启动轮询...")
            await bot_application.updater.start_polling()
            
            logger.info("Telegram机器人启动成功")
            
            # 启动订单检查任务
            logger.info("启动订单检查任务")
            
            async def order_check_job():
                """定期检查新订单的任务"""
                check_count = 0
                last_check_time = 0  # 上次检查的时间
                min_check_interval = 5  # 最小检查间隔（秒）
                
                while True:
                    check_count += 1
                    current_time = time.time()
                    
                    # 确保两次检查之间至少间隔 min_check_interval 秒
                    time_since_last_check = current_time - last_check_time
                    if time_since_last_check < min_check_interval:
                        await asyncio.sleep(min_check_interval - time_since_last_check)
                        current_time = time.time()
                    
                    try:
                        logger.debug(f"执行第 {check_count} 次订单检查")
                        await check_and_push_orders()
                        last_check_time = current_time
                    except Exception as e:
                        logger.error(f"订单检查任务出错: {str(e)}", exc_info=True)
                        # 出错后等待更长时间再重试
                        await asyncio.sleep(10)
                        continue
                    
                    # 每隔30次检查（约2.5分钟），检查机器人是否仍在运行
                    if check_count % 30 == 0:
                        try:
                            if bot_application and hasattr(bot_application, 'bot'):
                                test_response = await bot_application.bot.get_me()
                                logger.debug(f"机器人状态检查: @{test_response.username if test_response else 'Unknown'}")
                            else:
                                logger.error("机器人实例不可用")
                                return
                        except Exception as check_error:
                            logger.error(f"机器人状态检查失败: {str(check_error)}")
                            return
                    
                    # 正常情况下每5秒检查一次
                    await asyncio.sleep(5)
            # 启动任务并保存引用，以便后续可以取消
            order_check_task = asyncio.create_task(order_check_job())
            
            logger.info("进入主循环保持运行")
            
            # 保持运行，不要停止
            while True:
                await asyncio.sleep(60)  # 每分钟检查一次
                logger.debug("Telegram机器人仍在运行中")
                
                # 检查订单检查任务是否仍在运行
                if order_check_task.done():
                    exception = order_check_task.exception()
                    if exception:
                        logger.error(f"订单检查任务异常退出: {str(exception)}")
                    else:
                        logger.error("订单检查任务已退出但没有异常")
                    # 退出内部循环，让外部循环重启机器人
                    break
            
        except Exception as e:
            restart_count += 1
            logger.error(f"Telegram机器人运行出错 (尝试 {restart_count}/{max_restarts}): {str(e)}", exc_info=True)
            # 不立即退出，让外层循环尝试重启
            continue
        
        # 如果正常退出内部循环，也增加重启计数
        restart_count += 1
    
    logger.error(f"达到最大重启次数 ({max_restarts})，停止Telegram机器人")
    return

def run_bot_in_thread():
    """在线程中运行机器人"""
    try:
        logger.info("在单独的线程中启动Telegram机器人")
        asyncio.run(run_bot())
    except Exception as e:
        logger.error(f"Telegram机器人线程出错: {str(e)}", exc_info=True) 