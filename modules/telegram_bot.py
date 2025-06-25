import logging
import os
import sys
import functools
import psycopg2
from urllib.parse import urlparse
import sqlite3
from datetime import datetime
import pytz
import asyncio
import threading

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    CommandHandler
)

from modules.constants import (
    BOT_TOKEN, STATUS, DATABASE_URL
)
from modules.database import execute_query

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

# 全局变量
bot_application = None
notification_queue = None
BOT_LOOP = None

# 获取中国时间的函数
def get_china_time():
    """获取当前中国时间（UTC+8）"""
    utc_now = datetime.now(pytz.utc)
    china_now = utc_now.astimezone(CN_TIMEZONE)
    return china_now.strftime("%Y-%m-%d %H:%M:%S")

# 获取数据库连接
def get_db_connection():
    """获取PostgreSQL数据库连接"""
    try:
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

# 简单的启动命令处理函数
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理/start命令"""
    await update.message.reply_text("Bot is running")

# 发送新订单通知（只包含YouTube二维码功能）
async def send_new_order_notification(data):
    """发送新订单通知到所有卖家"""
    global bot_application
    
    try:
        # 获取新订单详情
        oid = data.get('order_id')
        account = data.get('account')
        
        # 构建消息文本
        message_text = (
            f"📦 New Order #{oid}\n"
            f"• Package: 1 Year Premium (YouTube)\n"
            f"• Price: 20 USDT\n"
            f"• Status: Pending"
        )
        
        # 检查是否有二维码图片
        has_qr_code = account and os.path.exists(account)
        logger.info(f"订单 #{oid} 二维码路径: {account}")
        logger.info(f"二维码文件是否存在: {has_qr_code}")
        
        # 创建完成和失败按钮
        keyboard = [[
            InlineKeyboardButton("✅ Mark as Complete", callback_data=f'complete_{oid}'),
            InlineKeyboardButton("❌ Mark as Failed", callback_data=f'fail_{oid}')
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # 获取卖家ID（简化为固定值）
        seller_id = 1878943383  # 示例固定值，实际使用时应该从配置或数据库获取
        
        try:
            if account and os.path.exists(account):
                with open(account, 'rb') as photo_file:
                    await bot_application.bot.send_photo(
                        chat_id=seller_id,
                        photo=photo_file,
                        caption=message_text,
                        reply_markup=reply_markup,
                        parse_mode='Markdown'
                    )
            else:
                await bot_application.bot.send_message(
                    chat_id=seller_id,
                    text=message_text,
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
            logger.info(f"成功向卖家 {seller_id} 推送订单 #{oid}")
        except Exception as e:
            logger.error(f"向卖家 {seller_id} 发送订单 #{oid} 通知失败: {str(e)}", exc_info=True)
    except Exception as e:
        logger.error(f"发送新订单通知时出错: {str(e)}", exc_info=True)

# 更新订单状态函数
def update_order_status(order_id, status, handler_id=None):
    """更新订单状态"""
    try:
        # 将字符串状态转换为常量状态值
        from modules.constants import STATUS
        
        # 如果传入的是字符串状态，转换为对应的数字状态
        if isinstance(status, str) and status.upper() in STATUS:
            numeric_status = STATUS[status.upper()]
            logger.info(f"将字符串状态 '{status}' 转换为数字状态 {numeric_status}")
            status = numeric_status
        
        conn = get_db_connection()
        if not conn:
            logger.error(f"更新订单 {order_id} 状态时无法获取数据库连接")
            print(f"ERROR: 更新订单 {order_id} 状态时无法获取数据库连接")
            return False
            
        cursor = conn.cursor()
        
        # PostgreSQL查询
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
        
        conn.commit()
        conn.close()
        
        logger.info(f"已更新订单 {order_id} 状态为 {status}")
        print(f"INFO: 已更新订单 {order_id} 状态为 {status}")
        return True
    except Exception as e:
        logger.error(f"更新订单 {order_id} 状态时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 更新订单 {order_id} 状态时出错: {str(e)}")
        return False 

# 处理回调查询（只保留complete和fail功能）
@callback_error_handler
async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理回调查询"""
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    
    logger.info(f"收到回调查询: {data} 来自用户 {user_id}")
    
    # 只保留complete和fail功能
    if data.startswith("complete_"):
        oid = int(data.split('_')[1])
        update_order_status(oid, STATUS['COMPLETED'], user_id)
        keyboard = [[InlineKeyboardButton("✅ Completed", callback_data="noop")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_reply_markup(reply_markup=reply_markup)
        await query.answer("Order marked as completed.", show_alert=True)
        return
    elif data.startswith("fail_"):
        oid = int(data.split('_')[1])
        update_order_status(oid, STATUS['FAILED'], user_id)
        keyboard = [[InlineKeyboardButton("❌ Failed", callback_data="noop")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_reply_markup(reply_markup=reply_markup)
        await query.answer("Order marked as failed.", show_alert=True)
        return
    else:
        await query.answer("Unknown command")

# webhook处理函数
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

# 简化的通知队列处理函数
async def process_notification_queue(queue):
    """处理通知队列中的消息"""
    while True:
        try:
            # 获取队列中的消息
            if not queue.empty():
                data = queue.get()
                logger.info(f"从队列获取到消息: {data.get('type')}")
                
                # 处理不同类型的通知
                if data.get('type') == 'new_order':
                    await send_new_order_notification(data)
                else:
                    logger.warning(f"未知的通知类型: {data.get('type')}")
                
                queue.task_done()
            
            # 等待一段时间后再检查队列
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"处理通知队列时出错: {str(e)}", exc_info=True)
            await asyncio.sleep(5)  # 出错后稍等长一点时间

# 机器人主函数
async def bot_main(queue):
    """机器人主函数"""
    global bot_application, notification_queue
    
    try:
        # 保存队列引用
        notification_queue = queue
        
        # 创建应用
        bot_application = ApplicationBuilder().token(BOT_TOKEN).build()
        
        # 添加处理器
        bot_application.add_handler(CommandHandler("start", on_start))
        bot_application.add_handler(CallbackQueryHandler(on_callback_query))
        
        # 启动通知队列处理任务
        asyncio.create_task(process_notification_queue(queue))
        
        # 启动轮询
        await bot_application.initialize()
        await bot_application.start()
        await bot_application.updater.start_polling()
        
        logger.info("Telegram机器人已启动")
        
        # 保持运行
        await bot_application.updater.start_polling()
    except Exception as e:
        logger.error(f"启动Telegram机器人时出错: {str(e)}", exc_info=True)

# 启动机器人的函数
def run_bot(queue):
    """运行Telegram机器人"""
    global BOT_LOOP
    
    try:
        logger.info("正在启动Telegram机器人...")
        
        # 创建事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        BOT_LOOP = loop
        
        # 运行机器人主函数
        loop.run_until_complete(bot_main(queue))
    except Exception as e:
        logger.error(f"运行Telegram机器人时出错: {str(e)}", exc_info=True)

# 在独立线程中启动机器人
def run_bot_in_thread():
    """在独立线程中启动机器人"""
    import queue
    
    # 创建队列
    q = queue.Queue()
    
    # 创建并启动线程
    bot_thread = threading.Thread(target=run_bot, args=(q,), daemon=True)
    bot_thread.start()
    
    logger.info("Telegram机器人线程已启动")
    
    return q 