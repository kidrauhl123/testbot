import os
import threading
import logging
import time
import queue
import sys
import atexit
import signal
import json
import traceback
import secrets
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash, send_from_directory, abort
import sqlite3
import shutil
import schedule  # 添加schedule库用于定时任务

# 根据环境变量确定是否为生产环境
is_production = os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('PRODUCTION')

# 日志配置
logging.basicConfig(
    level=logging.INFO if is_production else logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# 导入自定义模块
from modules.database import init_db, execute_query, check_db_connection, delete_old_orders
from modules.telegram_bot import run_bot, process_telegram_update
from modules.web_routes import register_routes, ensure_sellers_columns, ensure_orders_columns
from modules.constants import sync_env_sellers_to_db

# 创建一个线程安全的队列用于在Flask和Telegram机器人之间通信
notification_queue = queue.Queue()

# 锁目录路径
lock_dir = 'bot.lock'

# 定时任务：每天清理旧订单
def run_scheduled_tasks():
    """运行定时任务"""
    # 每天凌晨2点删除3天前的订单
    schedule.every().day.at("02:00").do(delete_old_orders)
    
    logger.info("定时任务已设置")
    
    while True:
        schedule.run_pending()
        time.sleep(60)  # 每分钟检查一次

# 清理锁目录和数据库 journal 文件的函数
def cleanup_resources():
    """清理应用锁目录和数据库 journal 文件。"""
    # 清理应用锁目录
    if os.path.exists(lock_dir):
        try:
            if os.path.isdir(lock_dir):
                os.rmdir(lock_dir)
                logger.info(f"已清理锁目录: {lock_dir}")
            else:
                os.remove(lock_dir) # 如果意外地成了文件
                logger.info(f"已清理锁文件: {lock_dir}")
        except Exception as e:
            logger.error(f"清理锁目录时出错: {str(e)}", exc_info=True)

    # 清理数据库 journal 文件
    try:
        journal_path = "orders.db-journal"
        if os.path.exists(journal_path):
            os.remove(journal_path)
            logger.info(f"已清理残留的 journal 文件: {journal_path}")
    except Exception as e:
        logger.error(f"清理 journal 文件时出错: {str(e)}", exc_info=True)

# 信号处理函数
def signal_handler(sig, frame):
    logger.info(f"收到信号 {sig}，正在清理资源...")
    cleanup_resources()
    sys.exit(0)

# 注册信号处理器和退出钩子
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)
atexit.register(cleanup_resources)

# ===== Flask 应用 =====
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET')
if not app.secret_key:
    logger.warning("未设置 FLASK_SECRET，正在使用临时密钥。生产环境必须设置固定强密钥。")
    app.secret_key = secrets.token_hex(32)
app.config['DEBUG'] = False
app.config['TEMPLATES_AUTO_RELOAD'] = True

# 确保静态文件目录存在
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
uploads_dir = os.path.join(static_dir, 'uploads')
if not os.path.exists(uploads_dir):
    try:
        os.makedirs(uploads_dir)
        logger.info(f"创建上传目录: {uploads_dir}")
    except Exception as e:
        logger.error(f"创建上传目录失败: {str(e)}", exc_info=True)

# 注册Web路由，并将队列传递给它
register_routes(app, notification_queue)

# 添加一个直接访问图片的路由，支持完整路径
@app.route('/<path:filepath>')
def serve_file(filepath):
    """提供直接访问文件的路由，主要用于TG机器人访问图片"""
    if 'static/uploads' in filepath:
        try:
            # 从完整路径中提取相对路径
            parts = filepath.split('static/uploads/')
            if len(parts) > 1:
                filename = parts[1].lstrip('/\\')
                return send_from_directory(uploads_dir, filename)
        except Exception as e:
            logger.error(f"访问文件 {filepath} 时出错: {str(e)}")
    
    # 如果不是上传文件路径，返回404
    return "File not found", 404

# 添加一个专门的图片查看页面
@app.route('/view-image/<path:filepath>')
def view_image(filepath):
    """提供一个专门的图片查看页面"""
    try:
        filename = filepath.split('static/uploads/', 1)[1] if 'static/uploads/' in filepath else filepath
        filename = filename.lstrip('/\\')
        if filename.startswith('..') or os.path.isabs(filename):
            abort(404)
        return send_from_directory(uploads_dir, filename)
    except Exception as e:
        logger.error(f"查看图片 {filepath} 时出错: {str(e)}", exc_info=True)
        abort(404)

# 添加Telegram webhook路由
@app.route('/telegram-webhook', methods=['POST'])
def telegram_webhook():
    """处理来自Telegram的webhook请求"""
    try:
        webhook_secret = os.environ.get('TELEGRAM_WEBHOOK_SECRET')
        if webhook_secret:
            request_secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
            if request_secret != webhook_secret:
                return jsonify({"status": "error", "message": "unauthorized"}), 401

        # 获取更新数据
        update_data = request.get_json()
        update_id = update_data.get('update_id') if isinstance(update_data, dict) else None
        logger.info(f"收到Telegram webhook更新: update_id={update_id}")
        
        # 在单独的线程中处理更新，避免阻塞Flask响应
        threading.Thread(
            target=process_telegram_update,
            args=(update_data, notification_queue),
            daemon=True
        ).start()
        
        # 立即返回响应，避免Telegram超时
        return jsonify({"status": "success"}), 200
    except Exception as e:
        logger.error(f"处理Telegram webhook时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 处理Telegram webhook时出错: {str(e)}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.errorhandler(Exception)
def handle_exception(e):
    """处理所有未捕获的异常"""
    logger.error(f"未捕获的异常: {str(e)}", exc_info=True)
    print(f"ERROR: 未捕获的异常: {str(e)}")
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500

# ===== 主程序 =====
if __name__ == "__main__":
    # 确保数据库列存在
    ensure_sellers_columns()
    ensure_orders_columns()
    
    # 启动时检查数据库连接
    check_db_connection()

    # 在启动前先尝试清理可能存在的锁文件和目录
    cleanup_resources()
            
    # 使用锁目录确保只有一个实例运行
    try:
        os.mkdir(lock_dir)
        logger.info("成功获取锁，启动主程序。")
    except FileExistsError:
        logger.error("锁目录已存在，另一个实例可能正在运行。程序退出。")
        sys.exit(1)
    except Exception as e:
        logger.error(f"创建锁目录时发生未知错误: {e}")
        sys.exit(1)

    # 初始化数据库
    logger.info("正在初始化数据库...")
    init_db()
    logger.info("数据库初始化完成")
    
    # 同步环境变量中的卖家到数据库
    logger.info("同步环境变量卖家到数据库...")
    sync_env_sellers_to_db()
    logger.info("环境变量卖家同步完成")
    
    # 启动 Bot 线程，并将队列传递给它
    logger.info("正在启动Telegram机器人...")
    bot_thread = threading.Thread(target=run_bot, args=(notification_queue,), daemon=True)
    bot_thread.start()
    logger.info("Telegram机器人线程已启动")
    
    # 启动定时任务线程
    logger.info("正在启动定时任务...")
    schedule_thread = threading.Thread(target=run_scheduled_tasks, daemon=True)
    schedule_thread.start()
    logger.info("定时任务线程已启动")
    
    # 启动 Flask
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"正在启动Flask服务器，端口：{port}...")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
