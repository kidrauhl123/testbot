import os
import threading
import logging
import time
from flask import Flask

# 设置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 导入自定义模块
from modules.database import init_db
from modules.telegram_bot import run_bot_in_thread
from modules.web_routes import register_routes

# ===== Flask 应用 =====
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'secret_' + str(time.time()))
app.config['DEBUG'] = True
app.config['TEMPLATES_AUTO_RELOAD'] = True

# 注册Web路由
register_routes(app)

# ===== 主程序 =====
if __name__ == "__main__":
    # 初始化数据库
    logger.info("正在初始化数据库...")
    init_db()
    logger.info("数据库初始化完成")
    
    # 启动 Bot 线程
    logger.info("正在启动Telegram机器人...")
    bot_thread = threading.Thread(target=run_bot_in_thread, daemon=True)
    bot_thread.start()
    logger.info("Telegram机器人线程已启动")
    
    # 启动 Flask
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"正在启动Flask服务器，端口：{port}...")
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)