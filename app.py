import os
import threading
import logging
import time
from flask import Flask

# 导入自定义模块
from modules.database import init_db
# 导入整个constants模块
import modules.constants as constants
from modules.telegram_bot import run_bot_in_thread
from modules.web_routes import register_routes

# 设置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== 初始化锁 =====
# 在constants模块中设置锁
constants.notified_orders_lock = threading.Lock()

# ===== Flask 应用 =====
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'secret_' + str(time.time()))

# 注册Web路由
register_routes(app)

# ===== 主程序 =====
if __name__ == "__main__":
    # 初始化数据库
    init_db()
    
    # 启动 Bot 线程
    bot_thread = threading.Thread(target=run_bot_in_thread, daemon=True)
    bot_thread.start()
    
    # 启动 Flask
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Flask server on port {port}...")
    app.run(host="0.0.0.0", port=port)