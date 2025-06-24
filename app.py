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
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, flash, send_file
import sqlite3
import shutil
import argparse
try:
    from flask_session import Session
except ImportError:
    # 如果没有flask_session，使用Flask自带的session
    print("Warning: flask_session not found, using default Flask session")
    Session = None
from modules.web_routes import register_routes
from modules.telegram_bot import run_bot_in_thread, process_telegram_update
from modules.init import initialize_app
from modules.database import init_db, execute_query
from modules.constants import sync_env_sellers_to_db

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# 根据环境变量确定是否为生产环境
is_production = os.environ.get('ENVIRONMENT') == 'production'
logger.info(f"环境: {'生产环境' if is_production else '开发环境'}")

# 创建通知队列
notification_queue = queue.Queue()

# 清理资源的函数
def cleanup_resources():
    logger.info("正在清理资源...")
    try:
        # 删除bot.lock文件
        if os.path.exists('bot.lock'):
            os.rmdir('bot.lock')
    except Exception as e:
        logger.error(f"清理资源时出错: {str(e)}")

# 注册退出时清理资源
atexit.register(cleanup_resources)

# 处理信号
def signal_handler(sig, frame):
    logger.info("接收到终止信号，正在退出...")
    cleanup_resources()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def main():
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='启动YouTube充值系统')
    parser.add_argument('--debug', action='store_true', help='启用调试模式')
    args = parser.parse_args()
    
    # 初始化应用
    try:
        initialize_app()
    except Exception as e:
        logger.error(f"初始化应用失败: {str(e)}")

    # 同步环境变量中的卖家到数据库
    try:
        sync_env_sellers_to_db()
        logger.info("环境变量卖家同步完成")
    except Exception as e:
        logger.error(f"同步卖家失败: {str(e)}")
    
    # 创建Flask应用
    app = Flask(__name__)
    
    # 配置会话
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev_key_for_youtube_bot")
        
    # 尝试使用Flask-Session如果可用
    if Session is not None:
        app.config["SESSION_TYPE"] = "filesystem"
        app.config["SESSION_PERMANENT"] = True
        app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 30  # 30天
        Session(app)
    else:
        # 使用默认的Flask session（基于cookie）
        app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 30  # 30天
    
    # 注册Web路由
    register_routes(app, notification_queue)
    
    # 启动Telegram机器人线程
    logger.info("正在启动Telegram机器人...")
    telegram_thread = threading.Thread(
        target=run_bot_in_thread,
        args=(notification_queue,),
        daemon=True
    )
    telegram_thread.start()
    logger.info("Telegram机器人线程已启动")
    
    # 运行Web服务器
    port = int(os.environ.get("PORT", 5050))
    logger.info(f"正在启动Flask服务器，端口：{port}...")
    app.run(host='0.0.0.0', port=port, debug=args.debug, use_reloader=False)

if __name__ == "__main__":
    main()