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
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash
import sqlite3
import shutil

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
from modules.database import init_db, execute_query
from modules.telegram_bot import run_bot, process_telegram_update
from modules.web_routes import register_routes
from modules.constants import sync_env_sellers_to_db

# 创建一个线程安全的队列用于在Flask和Telegram机器人之间通信
notification_queue = queue.Queue()

# 锁目录路径
lock_dir = 'bot.lock'

# 清理锁目录的函数
def cleanup_lock_directory():
    """清理SQLite锁目录，防止应用重启时因锁文件残留导致数据库访问错误"""
    try:
        db_path = "orders.db"
        lock_dir = f"{db_path}-journal"
        if os.path.exists(lock_dir):
            if os.path.isdir(lock_dir):
                shutil.rmtree(lock_dir)
                logger.info(f"已清理锁目录: {lock_dir}")
                print(f"INFO: 已清理锁目录: {lock_dir}")
            else:
                os.remove(lock_dir)
                logger.info(f"已删除锁文件: {lock_dir}")
                print(f"INFO: 已删除锁文件: {lock_dir}")
    except Exception as e:
        logger.error(f"清理锁目录时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 清理锁目录时出错: {str(e)}")
        traceback.print_exc()

# 信号处理函数
def signal_handler(sig, frame):
    logger.info(f"收到信号 {sig}，正在清理资源...")
    print(f"INFO: 收到信号 {sig}，正在清理资源...")
    cleanup_lock_directory()
    sys.exit(0)

# 注册信号处理器
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ===== Flask 应用 =====
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'secret_' + str(time.time()))
app.config['DEBUG'] = False
app.config['TEMPLATES_AUTO_RELOAD'] = True

# 注册Web路由，并将队列传递给它
register_routes(app, notification_queue)

# 添加Telegram webhook路由
@app.route('/telegram-webhook', methods=['POST'])
def telegram_webhook():
    """处理来自Telegram的webhook请求"""
    try:
        # 获取更新数据
        update_data = request.get_json()
        logger.info(f"收到Telegram webhook更新: {update_data}")
        print(f"DEBUG: 收到Telegram webhook更新: {update_data}")
        
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

@app.route('/')
def index():
    """首页"""
    try:
        return render_template('index.html')
    except Exception as e:
        logger.error(f"渲染首页时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 渲染首页时出错: {str(e)}")
        traceback.print_exc()
        return "服务器错误，请查看日志", 500

@app.route('/admin')
def admin():
    """管理员页面"""
    try:
        return render_template('admin.html')
    except Exception as e:
        logger.error(f"渲染管理员页面时出错: {str(e)}", exc_info=True)
        print(f"ERROR: 渲染管理员页面时出错: {str(e)}")
        traceback.print_exc()
        return "服务器错误，请查看日志", 500

@app.errorhandler(Exception)
def handle_exception(e):
    """处理所有未捕获的异常"""
    logger.error(f"未捕获的异常: {str(e)}", exc_info=True)
    print(f"ERROR: 未捕获的异常: {str(e)}")
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500

# ===== 主程序 =====
if __name__ == "__main__":
    # 在启动前先尝试清理可能存在的锁目录
    if os.path.exists(lock_dir):
        logger.warning("检测到锁目录已存在，可能是上次异常退出导致。尝试清理...")
        try:
            os.rmdir(lock_dir)
            logger.info("成功清理旧的锁目录")
        except Exception as e:
            logger.error(f"清理锁目录失败: {str(e)}")
            sys.exit(1)
            
    # 使用锁目录确保只有一个实例运行
    try:
        os.mkdir(lock_dir)
        logger.info("成功获取锁，启动主程序。")
        # 注册一个清理函数，在程序正常退出时删除锁目录
        atexit.register(cleanup_lock_directory)
    except FileExistsError:
        logger.error("锁目录已存在，另一个实例可能正在运行。程序退出。")
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
    
    # 启动 Flask
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"正在启动Flask服务器，端口：{port}...")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)