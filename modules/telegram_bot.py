import asyncio
import threading
import logging
from datetime import datetime
import time
import os
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
    STATUS_TEXT_ZH, SELLER_CHAT_IDS, DATABASE_URL
)
from modules.database import (
    get_order_details, execute_query, 
    get_unnotified_orders, get_active_seller_ids, 
    update_seller_last_active
)

# 设置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
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
                    await update.callback_query.answer("操作失败，请稍后重试", show_alert=True)
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

# ===== TG 命令处理 =====
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理/start命令"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    if is_seller(chat_id):
        await update.message.reply_text(
            f"欢迎回来，{user.first_name}！您是YouTube会员充值卖家。\n"
            f"您可以接收新的充值订单并处理它们。"
        )
    else:
        await update.message.reply_text(
            f"您好，{user.first_name}！这是YouTube会员充值机器人。\n"
            f"您不是授权卖家，无法处理订单。"
        )

# ===== TG 回调处理 =====
@callback_error_handler
async def on_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理接单回调"""
    chat_id = update.effective_chat.id
    user = update.effective_user
    callback_query = update.callback_query
    
    # 检查是否是卖家
    if not is_seller(chat_id):
        await callback_query.answer("您不是授权卖家，无法接单", show_alert=True)
        return
    
    # 更新卖家最后活跃时间
    update_seller_last_active(chat_id)
    
    try:
        # 解析回调数据，格式为: accept_订单ID
        data = callback_query.data
        if data.startswith('accept_'):
            order_id = int(data.split('_')[1])
            
            # 获取订单信息
            order_data = get_order_details(order_id)
            if not order_data:
                await callback_query.answer("找不到订单信息", show_alert=True)
                return
                
            # 接单处理
            seller_name = user.first_name
            if user.username:
                seller_name += f" (@{user.username})"
                
            success, message = accept_order(order_id, seller_name, chat_id)
            
            if success:
                # 修改消息，移除接单按钮
                account = order_data[1] if len(order_data) > 1 else "未知"
                package_type = order_data[2] if len(order_data) > 2 else "未知"
                
                updated_text = (
                    f"📋 订单 #{order_id}\n"
                    f"📱 账号: {account}\n"
                    f"📦 套餐: {PLAN_LABELS_EN.get(package_type, package_type)}\n"
                    f"👤 已被 {seller_name} 接单\n\n"
                    f"⏱ 接单时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                
                reply_markup = None  # 移除按钮
                
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=callback_query.message.message_id,
                    text=updated_text,
                    reply_markup=reply_markup
                )
                
                # 发送订单详情作为回复，便于卖家查看
                qr_path = account if account and not account.startswith("uploads/") else None
                if qr_path and os.path.exists(f"static/{qr_path}"):
                    # 发送二维码图片
                    try:
                        with open(f"static/{qr_path}", 'rb') as img:
                            await context.bot.send_photo(
                                chat_id=chat_id,
                                photo=img,
                                caption=f"订单 #{order_id} 的二维码"
                            )
                    except Exception as img_err:
                        logger.error(f"发送二维码图片失败: {str(img_err)}")
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"无法发送二维码图片: {str(img_err)}"
                        )
                
                # 通知接单成功
                await callback_query.answer("接单成功！", show_alert=True)
                
            else:
                # 通知接单失败
                await callback_query.answer(f"接单失败: {message}", show_alert=True)
        else:
            await callback_query.answer("无效的操作", show_alert=True)
    
    except Exception as e:
        logger.error(f"处理接单时出错: {str(e)}", exc_info=True)
        await callback_query.answer("处理接单时出错，请重试", show_alert=True)

@callback_error_handler
async def on_feedback_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理失败反馈按钮"""
    query = update.callback_query
    user_id = query.from_user.id
    
    logger.info(f"收到失败反馈回调: 用户ID={user_id}, data={repr(query.data)}")
    
    if not is_seller(user_id):
        await query.answer("您不是授权卖家", show_alert=True)
        return
    
    try:
        parts = query.data.split('_')
        if len(parts) < 3 or parts[0] != 'feedback':
            await query.answer("无效的回调数据", show_alert=True)
            return
            
        oid = int(parts[1])
        reason_type = parts[2]
        
        # 确认回调
        await query.answer()
        
        # 记录失败原因
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
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
        
        # 更新消息
        await query.edit_message_text(
            f"📦 Order #{oid}\n\n"
            f"❌ Order marked as FAILED\n"
            f"Reason: {reason_text}\n"
            f"Time: {timestamp}",
            parse_mode='Markdown'
        )
        
        # 如果是其他原因，等待用户输入详细信息
        if reason_type == "other":
            await context.bot.send_message(
                chat_id=user_id,
                text=f"请输入订单 #{oid} 失败的具体原因："
            )
        
        logger.info(f"订单 {oid} 已被标记为失败，原因: {reason_text}")
    except Exception as e:
        logger.error(f"处理失败反馈时出错: {str(e)}", exc_info=True)
        await query.answer("处理失败反馈时出错", show_alert=True)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文本消息"""
    user_id = update.effective_user.id
    text = update.message.text
    
    # 检查是否在等待失败订单的详细原因
    if user_id in feedback_waiting:
        oid = feedback_waiting[user_id]
        # 更新失败原因
        execute_query("UPDATE orders SET remark=? WHERE id=? AND accepted_by=?",
                     (f"Other reason: {text}", oid, str(user_id)))
        
        await update.message.reply_text(f"订单 #{oid} 的失败原因已更新为: {text}")
        
        # 从等待列表中移除
        del feedback_waiting[user_id]
        return

    # 其他文本消息处理
    if is_seller(user_id):
        await update.message.reply_text("请使用按钮操作订单")
    else:
        await update.message.reply_text("您好，这是YouTube会员充值机器人。您不是授权卖家，无法使用此机器人。")

async def check_and_push_orders():
    """定期检查并推送新订单"""
    try:
        # 获取未通知的订单
        unnotified_orders = get_unnotified_orders()
        if not unnotified_orders:
            return
            
        logger.info(f"发现 {len(unnotified_orders)} 个未通知的订单")
        
        for order in unnotified_orders:
            try:
                if len(order) < 6:
                    logger.error(f"订单数据格式错误: {order}")
                    continue
                    
                oid, account, password, package, created_at, web_user_id = order
                
                logger.info(f"准备推送订单 #{oid} 给卖家")
                
                message = (
                    f"📦 New Order #{oid}\n"
                    f"• Package: 1 Year Premium (YouTube)\n"
                    f"• Price: 20 USDT\n"
                    f"• Status: Pending"
                )
                
                # 创建接单按钮
                callback_data = f'accept_{oid}'
                logger.info(f"创建接单按钮，callback_data: {callback_data}")
                
                keyboard = [[InlineKeyboardButton("Accept", callback_data=callback_data)]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # 向所有卖家发送通知
                success_count = 0
                for seller_id in get_active_seller_ids():
                    try:
                        # 检查是否有二维码图片
                        has_qr_code = account and os.path.exists(account)
                        
                        if has_qr_code:
                            # 如果有二维码，先发送二维码图片
                            with open(account, 'rb') as photo:
                                await bot_application.bot.send_photo(
                                    chat_id=seller_id,
                                    photo=photo,
                                    caption=f"YouTube QR Code for Order #{oid}"
                                )
                        
                        # 然后发送订单信息
                        await bot_application.bot.send_message(
                            chat_id=seller_id,
                            text=message,
                            reply_markup=reply_markup
                        )
                        success_count += 1
                    except Exception as seller_e:
                        logger.error(f"向卖家 {seller_id} 发送通知失败: {str(seller_e)}")
                
                if success_count > 0:
                    # 标记订单为已通知
                    execute_query("UPDATE orders SET notified=1 WHERE id=?", (oid,))
                    logger.info(f"订单 #{oid} 已成功通知 {success_count} 位卖家")
                else:
                    logger.warning(f"订单 #{oid} 未能成功通知任何卖家")
            except Exception as order_e:
                logger.error(f"处理订单 #{oid} 通知时出错: {str(order_e)}")
    except Exception as e:
        logger.error(f"检查并推送订单时出错: {str(e)}", exc_info=True)

@callback_error_handler
async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理回调查询"""
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    
    logger.info(f"收到回调查询: {data} 来自用户 {user_id}")
    
    # 处理不同类型的回调
    if data.startswith("accept_"):
        await on_accept(update, context)
    elif data.startswith("feedback:"):
        await on_feedback_button(update, context)
    elif data.startswith("done_"):
        oid = int(data.split('_')[1])
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        execute_query("UPDATE orders SET status=?, completed_at=? WHERE id=? AND accepted_by=?",
                    (STATUS['COMPLETED'], timestamp, oid, str(user_id)))
        
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Completed", callback_data="noop")]])
        await query.edit_message_text(
            f"📦 Order #{oid}\n\n"
            f"✅ Successfully completed\n"
            f"Time: {timestamp}",
            reply_markup=keyboard
        )
        await query.answer("订单已标记为完成", show_alert=True)
    elif data.startswith("fail_"):
        oid = int(data.split('_')[1])
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("密码错误", callback_data=f"feedback_{oid}_wrong_password")],
            [InlineKeyboardButton("会员未到期", callback_data=f"feedback_{oid}_not_expired")],
            [InlineKeyboardButton("其他原因", callback_data=f"feedback_{oid}_other")]
        ])
        await query.edit_message_reply_markup(reply_markup=keyboard)
        await query.answer("请选择失败原因", show_alert=True)
    else:
        await query.answer("未知命令")

def run_bot(queue):
    """启动Telegram机器人（在主线程中）"""
    global notification_queue
    notification_queue = queue
    
    # 创建并启动异步任务
    threading.Thread(target=lambda: asyncio.run(bot_main(queue)), daemon=True).start()

async def bot_main(queue):
    """机器人主函数（异步）"""
    global bot_application, BOT_LOOP, notification_queue
    
    try:
        # 获取当前事件循环
        BOT_LOOP = asyncio.get_event_loop()
        notification_queue = queue
        
        # 创建bot应用
        bot_application = ApplicationBuilder().token(BOT_TOKEN).build()
        
        # 注册命令处理器
        bot_application.add_handler(CommandHandler("start", on_start))
        
        # 注册消息处理器
        bot_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
        
        # 注册回调查询处理器
        bot_application.add_handler(CallbackQueryHandler(on_callback_query))
        
        # 注册错误处理器
        bot_application.add_error_handler(error_handler)
        
        # 启动定期任务 - 检查并推送订单
        check_task = asyncio.create_task(periodic_order_check())
        
        # 启动通知队列处理
        notification_task = asyncio.create_task(process_notification_queue(queue))
        
        logger.info("机器人启动完成，开始轮询更新...")
        
        # 启动轮询
        await bot_application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        logger.error(f"机器人主函数出错: {str(e)}", exc_info=True)

async def error_handler(update, context):
    """处理机器人错误"""
    logger.error(f"Update {update} caused error: {context.error}")
    
    # 尝试获取错误的完整堆栈跟踪
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = ''.join(tb_list)
    logger.error(f"完整错误跟踪:\n{tb_string}")

async def periodic_order_check():
    """定期检查订单的异步任务"""
    while True:
        try:
            await check_and_push_orders()
        except Exception as e:
            logger.error(f"定期检查订单时出错: {str(e)}", exc_info=True)
        
        # 等待60秒
        await asyncio.sleep(60)

async def process_notification_queue(queue):
    """处理通知队列的异步任务"""
    while True:
        try:
            # 非阻塞方式获取通知
            try:
                item = queue.get_nowait()
                logger.info(f"从队列获取到通知: {item.get('type', 'unknown')}")
                
                # 处理通知
                if item.get('type') == 'new_order':
                    await send_new_order_notification(item)
                
                # 标记任务完成
                queue.task_done()
            except:
                # 队列为空，继续
                pass
        except Exception as e:
            logger.error(f"处理通知队列时出错: {str(e)}", exc_info=True)
        
        # 短暂休息
        await asyncio.sleep(1)

async def send_new_order_notification(data):
    """发送新订单通知给所有活跃卖家"""
    order_id = data.get('order_id')
    package = data.get('package', '12')  # 默认为1年会员
    qr_code_path = data.get('qr_code_path', '')
    username = data.get('username', '未知用户')
    timestamp = data.get('time', get_china_time())
    
    if not order_id:
        logger.error("无法发送订单通知: 缺少订单ID")
        return
        
    logger.info(f"准备发送订单 #{order_id} 通知给卖家")
    
    # 获取所有活跃卖家
    seller_ids = get_active_seller_ids()
    if not seller_ids:
        logger.warning("没有活跃卖家可以接收通知")
        return
    
    # 构建消息和按钮
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 接单", callback_data=f"accept_{order_id}")]
    ])
    
    message_text = (
        f"🆕 *新订单 #{order_id}*\n\n"
        f"• 套餐: *{PLAN_LABELS_EN.get(package, package)}*\n"
        f"• 创建时间: {timestamp}\n"
        f"• 创建者: {username}\n\n"
        f"请点击下方按钮接单处理"
    )
    
    sent_messages = []
    
    # 发送消息给所有卖家
    for seller_id in seller_ids:
        try:
            # 发送文本消息
            message = await bot_application.bot.send_message(
                chat_id=seller_id,
                text=message_text,
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
            
            # 如果有二维码图片，发送图片
            if qr_code_path and os.path.exists(f"static/{qr_code_path}"):
                try:
                    with open(f"static/{qr_code_path}", 'rb') as img:
                        await bot_application.bot.send_photo(
                            chat_id=seller_id,
                            photo=img,
                            caption=f"订单 #{order_id} 的二维码"
                        )
                except Exception as img_err:
                    logger.error(f"发送图片失败: {str(img_err)}")
            
            sent_messages.append({
                'seller_id': seller_id,
                'message_id': message.message_id
            })
            
            logger.info(f"成功向卖家 {seller_id} 发送订单 #{order_id} 通知")
            
        except Exception as e:
            logger.error(f"向卖家 {seller_id} 发送通知失败: {str(e)}")
    
    # 记录已通知状态
    if sent_messages:
        try:
            # 记录通知状态到数据库
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for msg in sent_messages:
                execute_query(
                    "INSERT INTO order_notifications (order_id, telegram_message_id, notified_at) VALUES (?, ?, ?)",
                    (order_id, f"{msg['seller_id']}:{msg['message_id']}", timestamp)
                )
            
            logger.info(f"订单 #{order_id} 通知状态已记录到数据库")
            
        except Exception as e:
            logger.error(f"记录通知状态到数据库失败: {str(e)}")
    
    return sent_messages