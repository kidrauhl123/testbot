import os
import threading
import logging
import time
from flask import Flask, jsonify

# 设置日志
logging.basicConfig(
    level=logging.DEBUG,  # 更改为DEBUG级别以获取更多信息
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 打印环境变量，用于调试
logger.info("环境变量:")
logger.info(f"BOT_TOKEN: {os.environ.get('BOT_TOKEN', '未设置')[:10]}...")
logger.info(f"ADMIN_CHAT_IDS: {os.environ.get('ADMIN_CHAT_IDS', '未设置')}")
logger.info(f"DATABASE_URL: {os.environ.get('DATABASE_URL', '未设置')[:15]}...")
logger.info(f"FLASK_SECRET: {os.environ.get('FLASK_SECRET', '未设置')[:5]}...")
logger.info(f"PORT: {os.environ.get('PORT', '未设置')}")

# 设置环境变量（如果尚未设置）
if not os.environ.get('ADMIN_CHAT_IDS'):
    os.environ['ADMIN_CHAT_IDS'] = '1878943383'  # 替换为您的Telegram ID
    logger.info(f"设置默认ADMIN_CHAT_IDS: {os.environ['ADMIN_CHAT_IDS']}")

# 导入自定义模块
from modules.database import init_db
# 导入整个constants模块
import modules.constants as constants
from modules.telegram_bot import run_bot_in_thread
from modules.web_routes import register_routes

# ===== 初始化锁 =====
# 在constants模块中设置锁
constants.notified_orders_lock = threading.Lock()

# ===== Flask 应用 =====
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'secret_' + str(time.time()))
app.config['DEBUG'] = True  # 启用调试模式
app.config['TEMPLATES_AUTO_RELOAD'] = True  # 自动重新加载模板
app.config['EXPLAIN_TEMPLATE_LOADING'] = True  # 解释模板加载过程

# 添加一个测试路由
@app.route('/test')
def test_route():
    logger.info("访问测试路由")
    return jsonify({
        'status': 'ok',
        'message': '服务器正常运行',
        'time': time.strftime("%Y-%m-%d %H:%M:%S"),
        'admin_ids': constants.ADMIN_CHAT_IDS
    })

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
    app.run(host="0.0.0.0", port=port, debug=True, use_reloader=True)