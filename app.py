import os
from dotenv import load_dotenv
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

# 在所有其他导入之前加载环境变量
load_dotenv()

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
from modules.database import init_db
from modules.telegram_bot import run_bot, process_telegram_update
from modules.web_routes import register_routes
from modules.constants import sync_env_sellers_to_db

# 创建一个线程安全的队列用于在Flask和Telegram机器人之间通信
notification_queue = queue.Queue()

# ===== Flask 应用 =====
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'secret_' + str(time.time()))
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

# 添加一个空的 favicon 路由，防止 404 错误刷屏
@app.route('/favicon.ico')
def favicon():
    return '', 204

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

@app.errorhandler(Exception)
def handle_exception(e):
    """处理所有未捕获的异常"""
    logger.error(f"未捕获的异常: {str(e)}", exc_info=True)
    print(f"ERROR: 未捕获的异常: {str(e)}")
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500

# ===== 应用初始化 =====
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

# ===== 主程序入口（仅用于本地开发） =====
if __name__ == "__main__":
    # 启动 Flask
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"正在启动Flask服务器，端口：{port}...")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)