import os
import time
import sqlite3
import asyncio
import threading
import logging
from datetime import datetime
from collections import defaultdict

from flask import Flask, request, render_template, jsonify, session
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters
)

# ✅ 写死变量（优先）
if not os.environ.get('BOT_TOKEN'):
    os.environ['BOT_TOKEN'] = '8120638144:AAHsC9o_juZ0dQB8YVdcN7fAJFTzX0mo_L4'
if not os.environ.get('ADMIN_CHAT_IDS'):
    os.environ['ADMIN_CHAT_IDS'] = '1878943383,7164554273'

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_IDS = [int(x) for x in os.environ.get("ADMIN_CHAT_IDS", "").split(",") if x.strip()]
PRICES = {'1': 1.35, '2': 1.3, '3': 3.2, '6': 5.7, '12': 9.2}

# ===== 状态常量 =====
STATUS = {
    'SUBMITTED': 'submitted',
    'ACCEPTED': 'accepted',
    'COMPLETED': 'completed',
    'FAILED': 'failed',
    'CANCELLED': 'cancelled'
}
STATUS_TEXT_ZH = {
    'submitted': '已提交', 'accepted': '已接单', 'completed': '充值成功',
    'failed': '充值失败', 'cancelled': '已撤销'
}
PLAN_OPTIONS = [('1', '1个月'), ('2', '2个月'), ('3', '3个月'), ('6', '6个月'), ('12', '12个月')]
PLAN_LABELS_ZH = {v: l for v, l in PLAN_OPTIONS}
PLAN_LABELS_EN = {'1': '1 Month', '2': '2 Months', '3': '3 Months', '6': '6 Months', '12': '12 Months'}

# ===== 全局变量 =====
user_languages = defaultdict(lambda: 'en')
feedback_waiting = {}
notified_orders = set()
notified_orders_lock = threading.Lock()  # 添加线程锁

# ===== Flask 应用 =====
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'secret_' + str(time.time()))

# ===== 日志 =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== 全局 Bot 实例 =====
bot_application = None

# ===== 数据库 =====
def init_db():
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            password TEXT NOT NULL,
            package TEXT NOT NULL,
            remark TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            accepted_at TEXT,
            completed_at TEXT,
            accepted_by TEXT,
            notified INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def get_unnotified_orders():
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    c.execute("SELECT id, account, package, remark FROM orders WHERE status=?", (STATUS['SUBMITTED'],))
    orders = []
    for row in c.fetchall():
        with notified_orders_lock:  # 使用线程锁
            if row[0] not in notified_orders:
                orders.append(row)
                notified_orders.add(row[0])
    conn.close()
    return orders

def accept_order_atomic(oid, user_id):
    conn = sqlite3.connect("orders.db")
    try:
        conn.execute("BEGIN IMMEDIATE")
        c = conn.cursor()
        c.execute("SELECT status FROM orders WHERE id=?", (oid,))
        row = c.fetchone()
        if not row or row[0] != STATUS['SUBMITTED']:
            conn.rollback()
            return False, "Order not available", None
        c.execute("SELECT COUNT(*) FROM orders WHERE status=? AND accepted_by=?", (STATUS['ACCEPTED'], str(user_id)))
        active_orders = c.fetchone()[0]
        if active_orders >= 2:
            conn.rollback()
            return False, f"Too many active orders (你已接 {active_orders} 单，最多同时接 2 单)", active_orders
        accepted_at = time.strftime("%Y-%m-%d %H:%M:%S")
        c.execute("UPDATE orders SET status=?, accepted_at=?, accepted_by=? WHERE id=?",
                  (STATUS['ACCEPTED'], accepted_at, str(user_id), oid))
        conn.commit()
        return True, "Success", None
    except Exception as e:
        conn.rollback()
        logger.error(f"accept_order_atomic error: {e}")
        return False, "Database error", None
    finally:
        conn.close()

def get_order_details(oid):
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    c.execute("SELECT account, password, package, remark, status FROM orders WHERE id=?", (oid,))
    row = c.fetchone()
    conn.close()
    return row

# ===== Bot 推送功能（使用全局 bot_application）=====
async def check_and_push_orders():
    """定期检查并推送新订单"""
    global bot_application
    while True:
        try:
            if bot_application is None:
                await asyncio.sleep(5)
                continue
                
            orders = get_unnotified_orders()
            for oid, account, package, remark in orders:
                text = f"📦 New Order #{oid}\nAccount: {account}\nPackage: {PLAN_LABELS_EN.get(package)}"
                if remark:
                    text += f"\nRemark: {remark}"
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Accept", callback_data=f"accept_{oid}")]])
                for admin_id in ADMIN_CHAT_IDS:
                    try:
                        await bot_application.bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard)
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.error(f"Failed to send message to admin {admin_id}: {e}")
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Push check failed: {e}")
            await asyncio.sleep(5)

# ===== Flask 路由 =====
@app.route('/', methods=['GET'])
def index():
    return render_template("index.html", PLAN_OPTIONS=PLAN_OPTIONS)

@app.route('/', methods=['POST'])
def create_order():
    account = request.form.get("account", "").strip()
    password = request.form.get("password", "").strip()
    package = request.form.get("package", "").strip()
    remark = request.form.get("remark", "").strip()
    if not all([account, password, package]):
        return jsonify({"error": "Missing fields"}), 400
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    c.execute("INSERT INTO orders (account, password, package, remark, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (account, password, package, remark, STATUS['SUBMITTED'], time.strftime("%Y-%m-%d %H:%M:%S")))
    oid = c.lastrowid
    conn.commit()
    conn.close()
    session.setdefault("orders", []).append(oid)
    return '', 204

@app.route('/orders/recent')
def orders_recent():
    # 获取整个系统的最近订单（不限于当前session）
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    # 获取最新的100个订单
    c.execute("""SELECT id, account, package, remark, status, created_at, accepted_at, completed_at 
                 FROM orders ORDER BY created_at DESC LIMIT 100""")
    rows = c.fetchall()
    conn.close()
    
    # 检查用户session中的订单，用于判断是否可以撤销
    user_order_ids = session.get("orders", [])
    
    return jsonify([{
        "id": row[0], 
        "account": row[1],
        "package": PLAN_LABELS_ZH.get(row[2], row[2]),
        "remark": row[3], 
        "status": row[4],
        "status_text": STATUS_TEXT_ZH.get(row[4], row[4]),
        "created_at": row[5], 
        "accepted_at": row[6], 
        "completed_at": row[7],
        "can_cancel": row[0] in user_order_ids  # 标记用户是否可以撤销此订单
    } for row in rows])

@app.route('/orders/my')
def orders_my():
    # 只返回当前用户的订单（保留原有功能）
    ids = session.get("orders", [])[-100:]
    if not ids:
        return jsonify([])
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    placeholders = ','.join('?' for _ in ids)
    c.execute(f"""SELECT id, account, package, remark, status, created_at, accepted_at, completed_at 
                  FROM orders WHERE id IN ({placeholders}) ORDER BY created_at DESC""", ids)
    rows = c.fetchall()
    conn.close()
    return jsonify([{
        "id": row[0], "account": row[1],
        "package": PLAN_LABELS_ZH.get(row[2], row[2]),
        "remark": row[3], "status": row[4],
        "status_text": STATUS_TEXT_ZH.get(row[4], row[4]),
        "created_at": row[5], "accepted_at": row[6], "completed_at": row[7]
    } for row in rows])

@app.route('/orders/cancel/<int:oid>', methods=['POST'])
def cancel_order(oid):
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    c.execute("SELECT status FROM orders WHERE id=?", (oid,))
    row = c.fetchone()
    if row and row[0] == STATUS['SUBMITTED']:
        if oid in session.get("orders", []):
            c.execute("UPDATE orders SET status=? WHERE id=?", (STATUS['CANCELLED'], oid))
            conn.commit()
    conn.close()
    return '', 204

# ===== Bot 回调处理 =====
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Welcome! Use /stats [date] to see reports.")

async def on_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    oid = int(query.data.split('_')[1])
    success, msg, count = accept_order_atomic(oid, user_id)
    if not success:
        await query.answer(msg, show_alert=True)
        if msg == "Order not available":
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Already taken", callback_data="noop")]]))
        return
    detail = get_order_details(oid)
    text = f"✅ Order #{oid} accepted!\nAccount: {detail[0]}\nPassword: {detail[1]}\nPackage: {PLAN_LABELS_EN.get(detail[2])}"
    if detail[3]:
        text += f"\nRemark: {detail[3]}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Done", callback_data=f"done_{oid}"),
         InlineKeyboardButton("❌ Fail", callback_data=f"fail_{oid}")]
    ])
    await query.edit_message_text(text=text, reply_markup=keyboard)

async def on_feedback_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    if data.startswith('done_'):
        oid = int(data.split('_')[1])
        conn = sqlite3.connect("orders.db")
        c = conn.cursor()
        c.execute("UPDATE orders SET status=?, completed_at=? WHERE id=? AND accepted_by=?",
                  (STATUS['COMPLETED'], time.strftime("%Y-%m-%d %H:%M:%S"), oid, str(user_id)))
        conn.commit()
        conn.close()
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Completed", callback_data="noop")]]))
    elif data.startswith('fail_'):
        feedback_waiting[user_id] = int(data.split('_')[1])
        await context.bot.send_message(chat_id=user_id, text="Please send the reason for failure:")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in feedback_waiting:
        oid = feedback_waiting.pop(user_id)
        reason = update.message.text[:200]
        conn = sqlite3.connect("orders.db")
        c = conn.cursor()
        c.execute("UPDATE orders SET status=?, remark=? WHERE id=? AND accepted_by=?",
                  (STATUS['FAILED'], reason, oid, str(user_id)))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"❌ Order #{oid} marked as failed.\nReason: {reason}")

async def on_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date_str = context.args[0] if context.args else datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    c.execute("""SELECT accepted_by, package, COUNT(*) FROM orders
                 WHERE date(accepted_at)=? AND status IN (?, ?, ?)
                 GROUP BY accepted_by, package""",
              (date_str, STATUS['ACCEPTED'], STATUS['COMPLETED'], STATUS['FAILED']))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No data.")
        return
    text = f"📊 Statistics for {date_str}\n"
    grouped = defaultdict(lambda: defaultdict(int))
    for uid, pkg, count in rows:
        grouped[uid][pkg] += count
    for uid, items in grouped.items():
        total = sum(count * PRICES.get(pkg, 0) for pkg, count in items.items())
        text += f"\n👤 User {uid}:\n"
        for pkg, count in items.items():
            text += f"  {PLAN_LABELS_EN.get(pkg)}: {count} (${PRICES.get(pkg, 0)*count:.2f})\n"
        text += f"  ➤ Total: ${total:.2f}\n"
    await update.message.reply_text(text)

# ===== 启动函数 =====
async def run_bot():
    """运行 Telegram Bot"""
    global bot_application
    
    # 创建 Bot 应用
    bot_application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # 注册处理器
    bot_application.add_handler(CommandHandler("start", on_start))
    bot_application.add_handler(CommandHandler("stats", on_stats))
    bot_application.add_handler(CallbackQueryHandler(on_accept, pattern=r"^accept_\d+$"))
    bot_application.add_handler(CallbackQueryHandler(on_feedback_button, pattern=r"^(done|fail)_\d+$"))
    bot_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    
    # 删除 webhook（如果存在）
    await bot_application.bot.delete_webhook(drop_pending_updates=True)
    
    # 启动 Bot
    await bot_application.initialize()
    await bot_application.start()
    await bot_application.updater.start_polling()
    
    # 启动推送任务
    asyncio.create_task(check_and_push_orders())
    
    # 保持运行
    await asyncio.Event().wait()

def run_bot_in_thread():
    """在独立线程中运行 Bot"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_bot())

# ===== 主程序 =====
if __name__ == "__main__":
    init_db()
    
    # 启动 Bot 线程
    bot_thread = threading.Thread(target=run_bot_in_thread, daemon=True)
    bot_thread.start()
    
    # 启动 Flask
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Flask server on port {port}...")
    app.run(host="0.0.0.0", port=port)
