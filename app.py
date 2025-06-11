import os
import time
import sqlite3
import asyncio
import threading
import logging
import json
import base64
import hashlib
from datetime import datetime, timedelta
from collections import defaultdict
import requests
import schedule
from functools import wraps

from flask import Flask, request, render_template, jsonify, session, redirect, url_for
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    filters
)

# ✅ 基础配置
if not os.environ.get('BOT_TOKEN'):
    os.environ['BOT_TOKEN'] = '8120638144:AAHsC9o_juZ0dQB8YVdcN7fAJFTzX0mo_L4'

BOT_TOKEN = os.environ["BOT_TOKEN"]

# GitHub 备份配置
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO = os.environ.get('GITHUB_REPO', '')
GITHUB_BRANCH = os.environ.get('GITHUB_BRANCH', 'main')

# 数据库URL配置（支持Railway PostgreSQL）
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///orders.db')

# ===== 价格系统 =====
WEB_PRICES = {'1': 12, '2': 18, '3': 30, '6': 50, '12': 84}
TG_PRICES = {'1': 1.35, '2': 1.3, '3': 3.2, '6': 5.7, '12': 9.2}

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
notified_orders_lock = threading.Lock()
user_info_cache = {}

# ===== Flask 应用 =====
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'secret_' + str(time.time()))

# ===== 日志 =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== 全局 Bot 实例 =====
bot_application = None
bot_thread = None

# ===== 数据库连接函数 =====
def get_db_connection():
    """获取数据库连接，支持SQLite和PostgreSQL"""
    if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
        # Railway PostgreSQL
        import psycopg2
        import psycopg2.extras
        # 修复Railway的URL格式
        url = DATABASE_URL.replace('postgres://', 'postgresql://')
        return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        # SQLite
        db_path = DATABASE_URL.replace('sqlite:///', '')
        return sqlite3.connect(db_path)

def execute_query(query, params=None, fetch=False, fetchone=False):
    """统一的数据库查询函数"""
    conn = get_db_connection()
    try:
        if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
            # PostgreSQL
            cursor = conn.cursor()
            cursor.execute(query, params or ())
            
            if fetchone:
                result = cursor.fetchone()
                return dict(result) if result else None
            elif fetch:
                results = cursor.fetchall()
                return [dict(row) for row in results]
            else:
                conn.commit()
                return cursor.lastrowid if hasattr(cursor, 'lastrowid') else None
        else:
            # SQLite
            cursor = conn.cursor()
            cursor.execute(query, params or ())
            
            if fetchone:
                return cursor.fetchone()
            elif fetch:
                return cursor.fetchall()
            else:
                conn.commit()
                return cursor.lastrowid
    finally:
        conn.close()

# ===== 数据库初始化 =====
def init_db():
    """初始化数据库，支持SQLite和PostgreSQL"""
    if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
        # PostgreSQL
        queries = [
            """
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                account TEXT NOT NULL,
                password TEXT NOT NULL,
                package TEXT NOT NULL,
                remark TEXT,
                status TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                accepted_at TIMESTAMP,
                completed_at TIMESTAMP,
                accepted_by TEXT,
                accepted_by_username TEXT,
                notified INTEGER DEFAULT 0,
                web_user_id TEXT,
                user_id INTEGER REFERENCES users(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS telegram_admins (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                added_by_user_id INTEGER REFERENCES users(id)
            )
            """
        ]
    else:
        # SQLite
        queries = [
            """
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
                accepted_by_username TEXT,
                notified INTEGER DEFAULT 0,
                web_user_id TEXT,
                user_id INTEGER,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                last_login TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS telegram_admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                is_active INTEGER DEFAULT 1,
                added_at TEXT NOT NULL,
                added_by_user_id INTEGER,
                FOREIGN KEY (added_by_user_id) REFERENCES users (id)
            )
            """
        ]
    
    # 执行创建表的查询
    for query in queries:
        execute_query(query)
    
    # 创建超级管理员账号（如果不存在）
    admin_hash = hashlib.sha256("755439".encode()).hexdigest()
    existing_admin = execute_query("SELECT id FROM users WHERE username = ?", ("755439",), fetchone=True)
    if not existing_admin:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        execute_query("""
            INSERT INTO users (username, password_hash, is_admin, created_at) 
            VALUES (?, ?, 1, ?)
        """, ("755439", admin_hash, timestamp))
    
    # 如果有环境变量中的管理员ID，迁移到数据库
    env_admin_ids = os.environ.get("ADMIN_CHAT_IDS", "").split(",")
    for admin_id in env_admin_ids:
        if admin_id.strip().isdigit():
            existing = execute_query("SELECT id FROM telegram_admins WHERE telegram_id = ?", 
                                   (int(admin_id.strip()),), fetchone=True)
            if not existing:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                execute_query("""
                    INSERT INTO telegram_admins (telegram_id, is_active, added_at) 
                    VALUES (?, ?, ?)
                """, (int(admin_id.strip()), 1, timestamp))

# ===== 管理员管理函数 =====
def get_active_admin_ids():
    """获取所有活跃的Telegram管理员ID"""
    admins = execute_query("SELECT telegram_id FROM telegram_admins WHERE is_active = ?", (1,), fetch=True)
    if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
        return [admin['telegram_id'] for admin in admins]
    else:
        return [admin[0] for admin in admins]

def add_telegram_admin(telegram_id, username=None, first_name=None, added_by_user_id=None):
    """添加Telegram管理员"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        execute_query("""
            INSERT INTO telegram_admins (telegram_id, username, first_name, is_active, added_at, added_by_user_id) 
            VALUES (?, ?, ?, ?, ?, ?)
        """, (telegram_id, username, first_name, 1, timestamp, added_by_user_id))
        return True
    except:
        return False

def remove_telegram_admin(telegram_id):
    """移除Telegram管理员（设为非活跃）"""
    execute_query("UPDATE telegram_admins SET is_active = ? WHERE telegram_id = ?", (0, telegram_id))

def is_telegram_admin(telegram_id):
    """检查是否为活跃管理员"""
    result = execute_query("SELECT id FROM telegram_admins WHERE telegram_id = ? AND is_active = ?", 
                          (telegram_id, 1), fetchone=True)
    return result is not None

# ===== 密码加密 =====
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# ===== 登录装饰器 =====
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ===== 管理员装饰器 =====
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or not session.get('is_admin'):
            return jsonify({'error': 'Unauthorized'}), 403
        return f(*args, **kwargs)
    return decorated_function

# ===== Telegram管理员管理API =====
@app.route('/admin')
@login_required
@admin_required
def admin():
    return render_template('admin.html')

@app.route('/admin/telegram-admins', methods=['GET'])
@admin_required
def get_telegram_admins():
    """获取所有Telegram管理员列表"""
    admins = execute_query("""
        SELECT ta.telegram_id, ta.username, ta.first_name, ta.is_active, 
               ta.added_at, u.username as added_by
        FROM telegram_admins ta
        LEFT JOIN users u ON ta.added_by_user_id = u.id
        ORDER BY ta.added_at DESC
    """, fetch=True)
    
    if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
        # PostgreSQL返回字典
        result = []
        for admin in admins:
            result.append({
                'telegram_id': admin['telegram_id'],
                'username': admin['username'],
                'first_name': admin['first_name'],
                'is_active': admin['is_active'],
                'added_at': admin['added_at'],
                'added_by': admin['added_by']
            })
        return jsonify(result)
    else:
        # SQLite返回元组
        result = []
        for admin in admins:
            result.append({
                'telegram_id': admin[0],
                'username': admin[1],
                'first_name': admin[2],
                'is_active': admin[3],
                'added_at': admin[4],
                'added_by': admin[5]
            })
        return jsonify(result)

@app.route('/admin/telegram-admins', methods=['POST'])
@admin_required
def add_telegram_admin_api():
    """添加Telegram管理员"""
    data = request.get_json()
    telegram_id = data.get('telegram_id')
    
    if not telegram_id or not str(telegram_id).isdigit():
        return jsonify({'error': 'Invalid Telegram ID'}), 400
    
    telegram_id = int(telegram_id)
    
    # 检查是否已存在
    existing = execute_query("SELECT id FROM telegram_admins WHERE telegram_id = ?", 
                           (telegram_id,), fetchone=True)
    if existing:
        return jsonify({'error': 'Admin already exists'}), 400
    
    # 尝试获取用户信息
    username = data.get('username')
    first_name = data.get('first_name')
    
    success = add_telegram_admin(telegram_id, username, first_name, session['user_id'])
    if success:
        return jsonify({'success': True, 'message': 'Admin added successfully'})
    else:
        return jsonify({'error': 'Failed to add admin'}), 500

@app.route('/admin/telegram-admins/<int:telegram_id>', methods=['DELETE'])
@admin_required
def remove_telegram_admin_api(telegram_id):
    """移除Telegram管理员"""
    remove_telegram_admin(telegram_id)
    return jsonify({'success': True, 'message': 'Admin removed successfully'})

@app.route('/admin/telegram-admins/<int:telegram_id>/toggle', methods=['POST'])
@admin_required
def toggle_telegram_admin(telegram_id):
    """切换管理员状态"""
    current = execute_query("SELECT is_active FROM telegram_admins WHERE telegram_id = ?", 
                          (telegram_id,), fetchone=True)
    if current:
        if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
            new_status = 0 if current['is_active'] else 1
        else:
            new_status = 0 if current[0] else 1
        execute_query("UPDATE telegram_admins SET is_active = ? WHERE telegram_id = ?", 
                     (new_status, telegram_id))
        return jsonify({'success': True, 'is_active': bool(new_status)})
    return jsonify({'error': 'Admin not found'}), 404

# ===== 其他现有函数保持不变，但需要更新获取管理员ID的方式 =====

def get_unnotified_orders():
    orders = execute_query("SELECT id, account, package, remark FROM orders WHERE status=?", 
                          (STATUS['SUBMITTED'],), fetch=True)
    result_orders = []
    for row in orders:
        if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
            order_id = row['id']
        else:
            order_id = row[0]
        
        with notified_orders_lock:
            if order_id not in notified_orders:
                result_orders.append(row)
                notified_orders.add(order_id)
    return result_orders

def accept_order_atomic(oid, user_id):
    conn = get_db_connection()
    try:
        if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            cursor.execute("SELECT status FROM orders WHERE id=%s", (oid,))
            row = cursor.fetchone()
            if not row or row['status'] != STATUS['SUBMITTED']:
                cursor.execute("ROLLBACK")
                return False, "Order not available", None
            
            cursor.execute("SELECT COUNT(*) FROM orders WHERE status=%s AND accepted_by=%s", 
                         (STATUS['ACCEPTED'], str(user_id)))
            active_orders = cursor.fetchone()['count']
            
            if active_orders >= 2:
                cursor.execute("ROLLBACK")
                return False, f"Too many active orders (你已接 {active_orders} 单，最多同时接 2 单)", active_orders
            
            accepted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute("UPDATE orders SET status=%s, accepted_at=%s, accepted_by=%s WHERE id=%s",
                         (STATUS['ACCEPTED'], accepted_at, str(user_id), oid))
            cursor.execute("COMMIT")
            return True, "Success", None
        else:
            # SQLite逻辑保持不变
            conn.execute("BEGIN IMMEDIATE")
            c = conn.cursor()
            c.execute("SELECT status FROM orders WHERE id=?", (oid,))
            row = c.fetchone()
            if not row or row[0] != STATUS['SUBMITTED']:
                conn.rollback()
                return False, "Order not available", None
            
            c.execute("SELECT COUNT(*) FROM orders WHERE status=? AND accepted_by=?", 
                     (STATUS['ACCEPTED'], str(user_id)))
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
        conn.rollback() if hasattr(conn, 'rollback') else None
        logger.error(f"accept_order_atomic error: {e}")
        return False, "Database error", None
    finally:
        conn.close()

def get_order_details(oid):
    result = execute_query("SELECT account, password, package, remark, status FROM orders WHERE id=?", 
                          (oid,), fetchone=True)
    if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
        if result:
            return (result['account'], result['password'], result['package'], 
                   result['remark'], result['status'])
    return result

# ===== Bot 推送功能（更新管理员获取方式）=====
async def check_and_push_orders():
    """定期检查并推送新订单"""
    global bot_application
    while True:
        try:
            if bot_application is None:
                await asyncio.sleep(5)
                continue
                
            orders = get_unnotified_orders()
            admin_ids = get_active_admin_ids()  # 从数据库获取活跃管理员
            
            for order_data in orders:
                if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
                    oid, account, package, remark = order_data['id'], order_data['account'], order_data['package'], order_data['remark']
                else:
                    oid, account, package, remark = order_data
                
                text = f"📦 New Order #{oid}\nAccount: {account}\nPackage: {PLAN_LABELS_EN.get(package)}"
                if remark:
                    text += f"\nRemark: {remark}"
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Accept", callback_data=f"accept_{oid}")]])
                
                for admin_id in admin_ids:
                    try:
                        await bot_application.bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard)
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.error(f"Failed to send message to admin {admin_id}: {e}")
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Push check failed: {e}")
            await asyncio.sleep(5)

# ===== 更新Bot命令处理，添加管理员权限检查 =====
async def on_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    username = query.from_user.username or "No_Username"
    
    # 检查是否为活跃管理员
    if not is_telegram_admin(user_id):
        await query.answer("You are not authorized to accept orders.", show_alert=True)
        return
    
    oid = int(query.data.split('_')[1])
    
    success, msg, count = accept_order_atomic(oid, user_id)
    if not success:
        await query.answer(msg, show_alert=True)
        if msg == "Order not available":
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Already taken", callback_data="noop")]]))
        return
    
    # 更新接单者用户名
    execute_query("UPDATE orders SET accepted_by_username=? WHERE id=?", (username, oid))
    
    detail = get_order_details(oid)
    text = f"✅ Order #{oid} accepted!\nAccount: {detail[0]}\nPassword: {detail[1]}\nPackage: {PLAN_LABELS_EN.get(detail[2])}"
    if detail[3]:
        text += f"\nRemark: {detail[3]}"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Done", callback_data=f"done_{oid}"),
         InlineKeyboardButton("❌ Fail", callback_data=f"fail_{oid}")]
    ])
    await query.edit_message_text(text=text, reply_markup=keyboard)

# ===== 添加管理员管理命令 =====
async def on_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员管理命令"""
    user_id = update.effective_user.id
    
    # 只有超级管理员（数据库中的is_admin=1用户对应的telegram_id）才能管理
    # 这里简化处理，只允许已存在的管理员添加新管理员
    if not is_telegram_admin(user_id):
        await update.message.reply_text("❌ You are not authorized to manage admins.")
        return
    
    args = context.args
    if not args:
        # 显示管理员列表
        admins = execute_query("""
            SELECT telegram_id, username, first_name, is_active 
            FROM telegram_admins 
            ORDER BY added_at DESC
        """, fetch=True)
        
        if not admins:
            await update.message.reply_text("📋 No admins found.")
            return
        
        text = "📋 **Current Telegram Admins:**\n\n"
        for admin in admins:
            if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
                telegram_id, username, first_name, is_active = admin['telegram_id'], admin['username'], admin['first_name'], admin['is_active']
            else:
                telegram_id, username, first_name, is_active = admin
            
            status = "✅ Active" if is_active else "❌ Inactive"
            name = first_name or "Unknown"
            username_text = f"@{username}" if username else "No username"
            text += f"• **{name}** ({username_text})\n  ID: `{telegram_id}` - {status}\n\n"
        
        text += "**Commands:**\n"
        text += "`/admin add <telegram_id>` - Add admin\n"
        text += "`/admin remove <telegram_id>` - Remove admin\n"
        text += "`/admin toggle <telegram_id>` - Toggle status"
        
        await update.message.reply_text(text, parse_mode='Markdown')
        return
    
    command = args[0].lower()
    
    if command == "add" and len(args) >= 2:
        try:
            new_admin_id = int(args[1])
            success = add_telegram_admin(new_admin_id)
            if success:
                await update.message.reply_text(f"✅ Added admin: {new_admin_id}")
            else:
                await update.message.reply_text(f"❌ Failed to add admin (may already exist): {new_admin_id}")
        except ValueError:
            await update.message.reply_text("❌ Invalid Telegram ID")
    
    elif command == "remove" and len(args) >= 2:
        try:
            admin_id = int(args[1])
            remove_telegram_admin(admin_id)
            await update.message.reply_text(f"✅ Removed admin: {admin_id}")
        except ValueError:
            await update.message.reply_text("❌ Invalid Telegram ID")
    
    elif command == "toggle" and len(args) >= 2:
        try:
            admin_id = int(args[1])
            current = execute_query("SELECT is_active FROM telegram_admins WHERE telegram_id = ?", 
                                  (admin_id,), fetchone=True)
            if current:
                if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
                    new_status = 0 if current['is_active'] else 1
                else:
                    new_status = 0 if current[0] else 1
                execute_query("UPDATE telegram_admins SET is_active = ? WHERE telegram_id = ?", 
                             (new_status, admin_id))
                status_text = "activated" if new_status else "deactivated"
                await update.message.reply_text(f"✅ Admin {admin_id} {status_text}")
            else:
                await update.message.reply_text(f"❌ Admin {admin_id} not found")
        except ValueError:
            await update.message.reply_text("❌ Invalid Telegram ID")
    
    else:
        await update.message.reply_text("❌ Invalid command. Use `/admin` to see available commands.")

# ===== 其他现有函数保持不变，但需要更新数据库查询方式 =====
# [这里包含其他现有的路由和函数，需要根据新的数据库函数进行相应更新]

# ===== Bot 命令处理函数 =====
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    user = update.effective_user
    user_id = user.id
    username = user.username
    first_name = user.first_name
    
    # 检查是否是管理员
    is_admin = is_telegram_admin(user_id)
    
    # 构建欢迎消息
    welcome_text = f"👋 你好 {first_name}！\n\n"
    if is_admin:
        welcome_text += "✅ 你是系统管理员，可以使用以下命令：\n"
        welcome_text += "/admin - 查看管理员列表\n"
        welcome_text += "/stats - 查看统计数据\n"
    else:
        welcome_text += "❌ 你不是系统管理员，无法使用管理命令。"
    
    await update.message.reply_text(welcome_text)

async def on_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /stats 命令"""
    user_id = update.effective_user.id
    
    # 检查是否是管理员
    if not is_telegram_admin(user_id):
        await update.message.reply_text("❌ 你不是系统管理员，无法使用此命令。")
        return
    
    # 获取统计数据
    stats = execute_query("""
        SELECT 
            COUNT(*) as total_orders,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed_orders,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_orders,
            SUM(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END) as pending_orders
        FROM orders
    """, fetchone=True)
    
    if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
        total = stats['total_orders'] or 0
        completed = stats['completed_orders'] or 0
        failed = stats['failed_orders'] or 0
        pending = stats['pending_orders'] or 0
    else:
        total = stats[0] or 0
        completed = stats[1] or 0
        failed = stats[2] or 0
        pending = stats[3] or 0
    
    text = "📊 **订单统计**\n\n"
    text += f"总订单数：{total}\n"
    text += f"✅ 已完成：{completed}\n"
    text += f"❌ 失败：{failed}\n"
    text += f"⏳ 待处理：{pending}"
    
    await update.message.reply_text(text, parse_mode='Markdown')

async def on_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理统计数据的回调查询"""
    query = update.callback_query
    await query.answer()
    
    # 这里可以添加更多统计数据的处理逻辑
    await query.edit_message_text("统计功能开发中...")

async def on_feedback_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理订单反馈按钮"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # 检查是否是管理员
    if not is_telegram_admin(user_id):
        await query.answer("你不是系统管理员，无法使用此功能。", show_alert=True)
        return
    
    action, oid = query.data.split('_')
    oid = int(oid)
    
    # 更新订单状态
    status = 'completed' if action == 'done' else 'failed'
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    execute_query("""
        UPDATE orders 
        SET status = ?, completed_at = ? 
        WHERE id = ?
    """, (status, timestamp, oid))
    
    # 更新消息
    status_text = "✅ 已完成" if action == 'done' else "❌ 失败"
    await query.edit_message_text(
        f"{query.message.text}\n\n{status_text}",
        reply_markup=None
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理文本消息"""
    # 这里可以添加文本消息的处理逻辑
    pass

# ===== Flask 路由 =====
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if not username or not password:
            return render_template('login.html', error='请输入用户名和密码')
        
        user = execute_query(
            "SELECT * FROM users WHERE username = ?", 
            (username,), 
            fetchone=True
        )
        
        if not user:
            return render_template('login.html', error='用户不存在')
        
        if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
            password_hash = user['password_hash']
        else:
            password_hash = user[2]
        
        if hash_password(password) != password_hash:
            return render_template('login.html', error='密码错误')
        
        session['user_id'] = user[0] if not DATABASE_URL.startswith('postgresql://') else user['id']
        session['username'] = username
        
        # 更新最后登录时间
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        execute_query(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (timestamp, user[0] if not DATABASE_URL.startswith('postgresql://') else user['id'])
        )
        
        return redirect(url_for('admin'))
    
    return render_template('login.html')

# ===== 主程序 =====
if __name__ == '__main__':
    import sys
    if 'bot' in sys.argv:
        import asyncio
        asyncio.run(run_bot())
    else:
        app.run(host='0.0.0.0', port=8080)