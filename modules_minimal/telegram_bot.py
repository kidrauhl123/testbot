import os
import sys
import logging
import asyncio
import functools
import threading
import queue
import traceback
from datetime import datetime
import pytz
import sqlite3

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler, filters,
                         CallbackQueryHandler, ConversationHandler, ApplicationBuilder)

from modules_minimal.constants import BOT_TOKEN, STATUS, STATUS_TEXT_ZH
from modules_minimal.database import (get_unnotified_orders, execute_query, 
                                     get_active_seller_ids, get_china_time)

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

# 定义bot_command_handler装饰器，用于处理命令
def bot_command_handler(func):
    """命令处理器的装饰器，用于注册命令处理函数"""
    @functools.wraps(func)
    async def wrapper(update: Update, context):
        try:
            return await func(update, context)
        except Exception as e:
            logger.error(f"命令 {func.__name__} 处理出错: {str(e)}", exc_info=True)
            await update.message.reply_text("处理命令时出错，请稍后重试")
    return wrapper

# 错误处理装饰器
def callback_error_handler(func):
    """装饰器：捕获并处理回调函数中的异常"""
    @functools.wraps(func)
    async def wrapper(update: Update, context):
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
notification_queue = None  # 将在run_bot函数中初始化

# ===== TG 辅助函数 =====
def is_seller(chat_id):
    """检查用户是否为已授权的卖家"""
    return chat_id in get_active_seller_ids()

# 添加处理 Telegram webhook 更新的函数
def process_telegram_update(update_data, queue):
    """处理来自Telegram webhook的更新（同步包装器）"""
    global BOT_LOOP
    
    try:
        if not BOT_LOOP:
            logger.error("机器人事件循环未初始化，无法处理webhook更新")
            return
        
        # 在机器人的事件循环中运行异步处理函数
        asyncio.run_coroutine_threadsafe(
            process_telegram_update_async(update_data, queue),
            BOT_LOOP
        )
        
        logger.info("已将webhook更新提交到机器人事件循环处理")
    
    except Exception as e:
        logger.error(f"提交webhook更新到事件循环时出错: {str(e)}", exc_info=True)

async def process_telegram_update_async(update_data, queue):
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

# ===== 命令处理函数 =====
async def on_start(update: Update, context):
    """开始命令处理"""
    user_id = update.effective_user.id
    
    if is_seller(user_id):
        await update.message.reply_text(
            "👋 欢迎使用二维码转发机器人！\n\n"
            "您是授权卖家，可以接收二维码转发通知。"
        )
    else:
        await update.message.reply_text(
            "⚠️ 访问受限 ⚠️\n\n"
            "此机器人仅对授权卖家开放。\n"
            "如需账号查询，请联系管理员。"
        )

async def on_help(update: Update, context):
    """帮助命令处理"""
    await update.message.reply_text(
        "📋 机器人使用帮助\n\n"
        "此机器人用于接收二维码转发通知。\n\n"
        "可用命令：\n"
        "/start - 开始使用机器人\n"
        "/help - 显示帮助信息"
    )

# ===== 回调查询处理 =====
@callback_error_handler
async def on_callback_query(update: Update, context):
    """处理回调查询"""
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    
    logger.info(f"收到回调查询: {data} 来自用户 {user_id}")
    
    # 处理不同类型的回调
    if data.startswith("accept:"):
        # 接单逻辑
        try:
            # 解析订单ID
            oid = int(data.split(':')[1])
            
            # 获取用户信息
            username = update.effective_user.username or ""
            first_name = update.effective_user.first_name or ""
            
            # 标记订单为已接单
            timestamp = get_china_time()
            
            # 检查订单状态
            order_status = execute_query("SELECT status FROM orders WHERE id = ?", (oid,), fetch=True)
            
            if not order_status:
                await query.answer("订单不存在", show_alert=True)
                return
            
            # 如果订单已被接单，则拒绝
            if order_status[0][0] != STATUS['SUBMITTED']:
                await query.answer("该订单已被接单", show_alert=True)
                return
            
            # 更新订单状态
            execute_query(
                """UPDATE orders SET status=?, accepted_by=?, accepted_at=? WHERE id=?""",
                (STATUS['ACCEPTED'], str(user_id), timestamp, oid)
            )
            
            # 更新按钮
            keyboard = [
                [
                    InlineKeyboardButton("✅ 完成", callback_data=f"done_{oid}"),
                    InlineKeyboardButton("❓ 问题", callback_data=f"problem_{oid}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=reply_markup)
            await query.answer("订单已接单", show_alert=True)
            logger.info(f"用户 {user_id} 已接单: {oid}")
        except Exception as e:
            logger.error(f"接单时出错: {str(e)}", exc_info=True)
            await query.answer("接单失败，请稍后重试", show_alert=True)
    
    elif data.startswith("done_"):
        # 完成订单逻辑
        oid = int(data.split('_')[1])
        
        try:
            timestamp = get_china_time()
            
            # 更新订单状态为已完成
            execute_query(
                "UPDATE orders SET status=?, completed_at=? WHERE id=?",
                (STATUS['COMPLETED'], timestamp, oid)
            )
            
            # 更新按钮显示
            keyboard = [[InlineKeyboardButton("✅ 已完成", callback_data="noop")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=reply_markup)
            await query.answer("订单已标记为完成", show_alert=True)
            logger.info(f"用户 {user_id} 已完成订单: {oid}")
        except Exception as e:
            logger.error(f"标记订单完成时出错: {str(e)}", exc_info=True)
            await query.answer("操作失败，请稍后重试", show_alert=True)
    
    elif data.startswith("problem_"):
        # 问题订单逻辑
        oid = int(data.split('_')[1])
        
        try:
            timestamp = get_china_time()
            
            # 更新订单状态为失败
            execute_query(
                "UPDATE orders SET status=?, completed_at=? WHERE id=?",
                (STATUS['FAILED'], timestamp, oid)
            )
            
            # 更新按钮显示
            keyboard = [[InlineKeyboardButton("❌ 处理失败", callback_data="noop")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=reply_markup)
            await query.answer("订单已标记为处理失败", show_alert=True)
            logger.info(f"用户 {user_id} 标记订单 {oid} 为处理失败")
        except Exception as e:
            logger.error(f"标记订单问题时出错: {str(e)}", exc_info=True)
            await query.answer("操作失败，请稍后重试", show_alert=True)

# ===== 主函数 =====
def run_bot(queue):
    """在单独的线程中运行机器人"""
    global BOT_LOOP
    global bot_application
    global notification_queue
    
    # 设置全局变量
    notification_queue = queue
    
    try:
        # 创建事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        BOT_LOOP = loop
        
        # 运行机器人
        loop.run_until_complete(bot_main(queue))
    except Exception as e:
        logger.critical(f"运行机器人时发生严重错误: {str(e)}", exc_info=True)

async def bot_main(queue):
    """机器人的主异步函数"""
    global bot_application
    
    logger.info("正在启动Telegram机器人...")
    
    try:
        # 初始化
        bot_application = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .connection_pool_size(8)
            .connect_timeout(30.0)
            .read_timeout(30.0)
            .write_timeout(30.0)
            .build()
        )
        
        logger.info("Telegram机器人应用已构建")
        
        # 添加处理程序
        bot_application.add_handler(CommandHandler("start", on_start))
        bot_application.add_handler(CommandHandler("help", on_help))
        bot_application.add_handler(CallbackQueryHandler(on_callback_query))
        
        # 启动机器人
        await bot_application.initialize()
        
        # 启动后台任务
        asyncio.create_task(process_notification_queue(queue))
        asyncio.create_task(periodic_order_check())
        
        logger.info("Telegram机器人已启动")
        
        # 保持机器人运行
        await bot_application.updater.start_polling()
        await asyncio.Future()  # 永远运行
    except Exception as e:
        logger.error(f"启动Telegram机器人时出错: {str(e)}", exc_info=True)

# ===== 后台任务 =====
async def periodic_order_check():
    """定期检查未通知的订单"""
    while True:
        try:
            await asyncio.sleep(10)  # 每10秒检查一次
            
            # 获取未通知的订单
            unnotified_orders = get_unnotified_orders()
            
            if unnotified_orders:
                logger.info(f"发现 {len(unnotified_orders)} 个未通知的订单")
                
                # 立即标记这些订单为已通知，防止其他进程重复处理
                order_ids = [order[0] for order in unnotified_orders]
                
                # SQLite需要逐个更新
                for order_id in order_ids:
                    execute_query("UPDATE orders SET notified = 1 WHERE id = ?", (order_id,))
                
                logger.info(f"已将订单 {order_ids} 标记为已通知")
                
                # 现在安全地处理这些订单
                for order in unnotified_orders:
                    # 注意：order是一个元组，不是字典
                    # 根据查询，元素顺序为: id, account, created_at
                    order_id = order[0]
                    account = order[1]  # 图片路径
                    
                    # 使用全局通知队列
                    global notification_queue
                    if notification_queue:
                        # 添加到通知队列
                        notification_queue.put({
                            'type': 'new_order',
                            'order_id': order_id,
                            'account': account,
                            'preferred_seller': None
                        })
                        logger.info(f"已将订单 #{order_id} 添加到通知队列")
                    else:
                        logger.error("通知队列未初始化")
            else:
                logger.debug("没有发现未通知的订单")
        except Exception as e:
            logger.error(f"检查未通知订单时出错: {str(e)}", exc_info=True)
            await asyncio.sleep(30)  # 出错后等待30秒再重试

async def process_notification_queue(queue):
    """处理通知队列"""
    while True:
        try:
            # 从队列中获取通知数据
            data = queue.get(block=False)
            
            # 处理通知
            await send_notification_from_queue(data)
            
            # 标记任务完成
            queue.task_done()
        except queue.Empty:
            # 队列为空，等待一段时间
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"处理通知队列时出错: {str(e)}", exc_info=True)
            await asyncio.sleep(5)  # 出错后等待5秒再重试

async def send_notification_from_queue(data):
    """发送来自队列的通知"""
    try:
        if data['type'] == 'new_order':
            # 处理新订单通知
            order_id = data['order_id']
            account = data['account']
            
            # 获取所有活跃卖家
            seller_ids = get_active_seller_ids()
            
            if not seller_ids:
                logger.warning(f"没有活跃卖家可接收订单 #{order_id} 的通知")
                return
            
            # 构建消息文本
            message = f"📣 *新订单通知*\n\n"
            message += f"订单ID: `{order_id}`\n"
            message += f"创建时间: {get_china_time()}\n\n"
            
            # 构建接单按钮
            keyboard = [
                [InlineKeyboardButton("👍 接单", callback_data=f"accept:{order_id}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # 获取图片URL的完整路径
            image_path = account
            if not image_path.startswith('/'):
                image_path = '/' + image_path
            
            # 向所有活跃卖家发送通知
            for seller_id in seller_ids:
                try:
                    # 尝试发送图片消息
                    try:
                        await bot_application.bot.send_photo(
                            chat_id=seller_id,
                            photo=open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), account), 'rb'),
                            caption=message,
                            reply_markup=reply_markup,
                            parse_mode='Markdown'
                        )
                        logger.info(f"向卖家 {seller_id} 发送订单 #{order_id} 通知成功")
                    except Exception as photo_error:
                        # 如果发送图片失败，尝试发送纯文本消息
                        logger.error(f"发送图片消息失败: {str(photo_error)}")
                        await bot_application.bot.send_message(
                            chat_id=seller_id,
                            text=f"{message}\n\n[图片无法显示，请查看网站]",
                            reply_markup=reply_markup,
                            parse_mode='Markdown'
                        )
                        logger.info(f"向卖家 {seller_id} 发送订单 #{order_id} 纯文本通知成功")
                except Exception as seller_error:
                    logger.error(f"向卖家 {seller_id} 发送订单通知失败: {str(seller_error)}")
    except Exception as e:
        logger.error(f"发送通知失败: {str(e)}", exc_info=True) 