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

# ✅ 写死变量（优先）
if not os.environ.get('BOT_TOKEN'):
    os.environ['BOT_TOKEN'] = '8120638144:AAHsC9o_juZ0dQB8YVdcN7fAJFTzX0mo_L4'

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_IDS = [int(x) for x in os.environ.get("ADMIN_CHAT_IDS", "").split(",") if x.strip()]

# GitHub 备份配置 - 需要在Railway环境变量中设置
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN', '')  # GitHub Personal Access Token
GITHUB_REPO = os.environ.get('GITHUB_REPO', '')    # 格式: username/repo-name
GITHUB_BRANCH = os.environ.get('GITHUB_BRANCH', 'main')  # 分支名

# ===== 价格系统 =====
# 网页端价格（人民币）
WEB_PRICES = {'1': 12, '2': 18, '3': 30, '6': 50, '12': 84}
# Telegram端管理员薪资（美元）
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
user_info_cache = {}  # 缓存用户信息

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
    
    # 订单表
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
            accepted_by_username TEXT,
            notified INTEGER DEFAULT 0,
            web_user_id TEXT,
            user_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    
    # 用户表
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_login TEXT
        )
    """)
    
    # 检查是否需要添加新列
    c.execute("PRAGMA table_info(orders)")
    columns = [column[1] for column in c.fetchall()]
    if 'user_id' not in columns:
        c.execute("ALTER TABLE orders ADD COLUMN user_id INTEGER")
    
    # 创建超级管理员账号（如果不存在）
    admin_hash = hashlib.sha256("755439".encode()).hexdigest()
    c.execute("SELECT id FROM users WHERE username = ?", ("755439",))
    if not c.fetchone():
        c.execute("""
            INSERT INTO users (username, password_hash, is_admin, created_at) 
            VALUES (?, ?, 1, ?)
        """, ("755439", admin_hash, time.strftime("%Y-%m-%d %H:%M:%S")))
    
    conn.commit()
    conn.close()

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

# ===== GitHub 备份功能 =====
async def get_user_info(user_id):
    """获取Telegram用户信息并缓存"""
    global bot_application, user_info_cache
    
    if not bot_application:
        return {"id": user_id, "username": "Unknown", "first_name": "Unknown"}
    
    # 检查缓存
    if user_id in user_info_cache:
        return user_info_cache[user_id]
    
    try:
        user = await bot_application.bot.get_chat(user_id)
        user_info = {
            "id": user_id,
            "username": user.username or "No_Username",
            "first_name": user.first_name or "Unknown",
            "last_name": user.last_name or ""
        }
        user_info_cache[user_id] = user_info
        return user_info
    except Exception as e:
        logger.error(f"Failed to get user info for {user_id}: {e}")
        default_info = {"id": user_id, "username": "Unknown", "first_name": "Unknown"}
        user_info_cache[user_id] = default_info
        return default_info

def backup_to_github(data, filename):
    """将数据备份到GitHub"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logger.warning("GitHub备份未配置：缺少GITHUB_TOKEN或GITHUB_REPO环境变量")
        return False
    
    try:
        # GitHub API URL
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/backups/{filename}"
        
        # 准备数据
        content = json.dumps(data, ensure_ascii=False, indent=2)
        content_encoded = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        
        # 检查文件是否已存在（获取SHA）
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        
        get_response = requests.get(api_url, headers=headers)
        sha = None
        if get_response.status_code == 200:
            sha = get_response.json().get('sha')
        
        # 准备提交数据
        commit_data = {
            'message': f'自动备份订单数据 - {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}',
            'content': content_encoded,
            'branch': GITHUB_BRANCH
        }
        
        if sha:
            commit_data['sha'] = sha
        
        # 提交到GitHub
        response = requests.put(api_url, headers=headers, json=commit_data)
        
        if response.status_code in [200, 201]:
            logger.info(f"成功备份到GitHub: {filename}")
            return True
        else:
            logger.error(f"GitHub备份失败: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"GitHub备份异常: {e}")
        return False

async def create_daily_backup():
    """创建当日订单备份 - 修复统计逻辑"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"开始创建 {today} 的订单备份...")
        
        conn = sqlite3.connect("orders.db")
        c = conn.cursor()
        
        # 获取当日所有订单
        c.execute("""
            SELECT id, account, password, package, remark, status, 
                   created_at, accepted_at, completed_at, accepted_by, 
                   accepted_by_username, web_user_id
            FROM orders 
            WHERE date(created_at) = ?
            ORDER BY created_at DESC
        """, (today,))
        
        orders = c.fetchall()
        conn.close()
        
        if not orders:
            logger.info(f"{today} 没有订单数据需要备份")
            return
        
        # 处理订单数据，获取用户信息
        backup_data = {
            "backup_date": today,
            "backup_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_orders": len(orders),
            "orders": []
        }
        
        stats = {
            "total_amount_cny": 0,  # 网页端总收入（人民币）- 只统计完成的
            "total_salary_usd": 0,  # TG端总薪资（美元）- 只统计完成的
            "status_count": defaultdict(int),
            "package_stats": defaultdict(int)
        }
        
        for order in orders:
            order_id, account, password, package, remark, status, created_at, accepted_at, completed_at, accepted_by, accepted_by_username, web_user_id = order
            
            # 获取接单者信息
            accepter_info = None
            if accepted_by:
                try:
                    accepter_info = await get_user_info(int(accepted_by))
                except:
                    accepter_info = {"id": accepted_by, "username": "Unknown", "first_name": "Unknown"}
            
            # 构建订单数据
            order_data = {
                "id": order_id,
                "account": account,
                "password": password,
                "package": package,
                "package_name": PLAN_LABELS_ZH.get(package, package),
                "remark": remark,
                "status": status,
                "status_text": STATUS_TEXT_ZH.get(status, status),
                "created_at": created_at,
                "accepted_at": accepted_at,
                "completed_at": completed_at,
                "web_user_id": web_user_id,
                "accepter": accepter_info
            }
            
            backup_data["orders"].append(order_data)
            
            # 统计数据
            stats["status_count"][status] += 1
            stats["package_stats"][package] += 1
            
            # 只有完成的订单才计入金额统计
            if status == STATUS['COMPLETED']:
                stats["total_amount_cny"] += WEB_PRICES.get(package, 0)
                stats["total_salary_usd"] += TG_PRICES.get(package, 0)
        
        # 添加统计信息
        backup_data["statistics"] = {
            "total_revenue_cny": stats["total_amount_cny"],  # 只包含完成订单的收入
            "total_salary_usd": stats["total_salary_usd"],   # 只包含完成订单的薪资
            "status_breakdown": dict(stats["status_count"]),
            "package_breakdown": dict(stats["package_stats"]),
            "status_breakdown_text": {k: STATUS_TEXT_ZH.get(k, k) for k in stats["status_count"].keys()},
            "package_breakdown_text": {k: PLAN_LABELS_ZH.get(k, k) for k in stats["package_stats"].keys()},
            # 添加详细的收入统计说明
            "revenue_note": "金额统计仅包含已完成(completed)状态的订单"
        }
        
        # 备份到GitHub
        filename = f"orders_{today}.json"
        success = backup_to_github(backup_data, filename)
        
        if success:
            logger.info(f"成功备份 {len(orders)} 条订单到GitHub")
            
            # 发送备份通知给管理员 - 修复通知内容
            if bot_application and ADMIN_CHAT_IDS:
                completed_orders = sum(1 for _, _, _, _, _, status, _, _, _, _, _, _ in orders if status == STATUS['COMPLETED'])
                message = f"📦 **每日订单备份完成**\n\n"
                message += f"📅 日期：{today}\n"
                message += f"📊 订单总数：{len(orders)}\n"
                message += f"✅ 完成订单：{completed_orders}\n"
                message += f"💰 实际收入：¥{stats['total_amount_cny']}\n"
                message += f"💵 实际薪资：${stats['total_salary_usd']:.2f}\n"
                message += f"📝 备注：金额仅统计已完成订单\n"
                message += f"✅ 已自动备份到GitHub"
                
                for admin_id in ADMIN_CHAT_IDS:
                    try:
                        await bot_application.bot.send_message(
                            chat_id=admin_id, 
                            text=message, 
                            parse_mode='Markdown'
                        )
                    except Exception as e:
                        logger.error(f"发送备份通知失败 {admin_id}: {e}")
        else:
            logger.error("GitHub备份失败")
            
    except Exception as e:
        logger.error(f"创建备份失败: {e}")

def schedule_daily_backup():
    """安排每日备份任务"""
    schedule.every().day.at("00:00").do(lambda: asyncio.create_task(create_daily_backup()))
    
    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(60)  # 每分钟检查一次
    
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    logger.info("每日备份调度器已启动 - 每天00:00自动备份")

# ===== 原有功能（保持不变）=====
def get_unnotified_orders():
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    c.execute("SELECT id, account, package, remark FROM orders WHERE status=?", (STATUS['SUBMITTED'],))
    orders = []
    for row in c.fetchall():
        with notified_orders_lock:
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

# ===== Bot 推送功能 =====
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

# 登录页面
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        if not username or not password:
            return render_template('login.html', error='请填写用户名和密码')
        
        conn = sqlite3.connect("orders.db")
        c = conn.cursor()
        c.execute("SELECT id, password_hash, is_admin FROM users WHERE username = ?", (username,))
        user = c.fetchone()
        
        if user and user[1] == hash_password(password):
            # 更新最后登录时间
            c.execute("UPDATE users SET last_login = ? WHERE id = ?", 
                     (time.strftime("%Y-%m-%d %H:%M:%S"), user[0]))
            conn.commit()
            conn.close()
            
            # 设置session
            session['user_id'] = user[0]
            session['username'] = username
            session['is_admin'] = user[2] == 1
            
            return redirect(url_for('index'))
        else:
            conn.close()
            return render_template('login.html', error='用户名或密码错误')
    
    return render_template('login.html')

# 注册页面
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        password_confirm = request.form.get('password_confirm', '').strip()
        
        if not username or not password:
            return render_template('register.html', error='请填写用户名和密码')
        
        if password != password_confirm:
            return render_template('register.html', error='两次输入的密码不一致')
        
        if len(username) < 3:
            return render_template('register.html', error='用户名至少3个字符')
        
        if len(password) < 6:
            return render_template('register.html', error='密码至少6个字符')
        
        conn = sqlite3.connect("orders.db")
        c = conn.cursor()
        
        # 检查用户名是否已存在
        c.execute("SELECT id FROM users WHERE username = ?", (username,))
        if c.fetchone():
            conn.close()
            return render_template('register.html', error='用户名已存在')
        
        # 创建新用户
        c.execute("""
            INSERT INTO users (username, password_hash, is_admin, created_at) 
            VALUES (?, ?, 0, ?)
        """, (username, hash_password(password), time.strftime("%Y-%m-%d %H:%M:%S")))
        
        user_id = c.lastrowid
        conn.commit()
        conn.close()
        
        # 自动登录
        session['user_id'] = user_id
        session['username'] = username
        session['is_admin'] = False
        
        return redirect(url_for('index'))
    
    return render_template('register.html')

# 登出
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# 主页（需要登录）
@app.route('/', methods=['GET'])
@login_required
def index():
    return render_template("index.html", 
                         PLAN_OPTIONS=PLAN_OPTIONS, 
                         WEB_PRICES=WEB_PRICES,
                         username=session.get('username'),
                         is_admin=session.get('is_admin', False))

@app.route('/', methods=['POST'])
@login_required
def create_order():
    account = request.form.get("account", "").strip()
    password = request.form.get("password", "").strip()
    package = request.form.get("package", "").strip()
    remark = request.form.get("remark", "").strip()
    
    if not all([account, password, package]):
        return jsonify({"error": "Missing fields"}), 400
    
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    c.execute("""
        INSERT INTO orders (account, password, package, remark, status, created_at, user_id) 
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (account, password, package, remark, STATUS['SUBMITTED'], 
          time.strftime("%Y-%m-%d %H:%M:%S"), session['user_id']))
    oid = c.lastrowid
    conn.commit()
    conn.close()
    
    return '', 204

@app.route('/orders/stats/web/<user_id>')
@login_required
def web_user_stats(user_id):
    """获取网页端用户的订单统计"""
    # 权限检查：只能查看自己的统计，除非是管理员
    if not session.get('is_admin') and str(session['user_id']) != user_id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    date_str = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    
    # 使用新的user_id字段查询
    c.execute("""SELECT package, 
                        COUNT(*) as total_count,
                        SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as completed_count
                 FROM orders 
                 WHERE user_id=? AND date(created_at)=?
                 GROUP BY package""",
              (STATUS['COMPLETED'], int(user_id), date_str))
    
    stats = []
    total_orders = 0
    total_amount = 0
    
    for pkg, total_count, completed_count in c.fetchall():
        amount = completed_count * WEB_PRICES.get(pkg, 0)
        total_orders += total_count
        total_amount += amount
        stats.append({
            'package': PLAN_LABELS_ZH.get(pkg, pkg),
            'count': total_count,
            'completed_count': completed_count,
            'amount': amount
        })
    
    conn.close()
    
    return jsonify({
        'date': date_str,
        'total_orders': total_orders,
        'total_amount': total_amount,
        'details': stats
    })

@app.route('/orders/recent')
@login_required
def orders_recent():
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    
    # 如果是管理员，显示所有订单；否则只显示该用户的订单
    if session.get('is_admin'):
        c.execute("""SELECT o.id, o.account, o.package, o.remark, o.status, 
                           o.created_at, o.accepted_at, o.completed_at, u.username, o.accepted_by_username
                     FROM orders o
                     LEFT JOIN users u ON o.user_id = u.id
                     ORDER BY o.created_at DESC LIMIT 100""")
    else:
        c.execute("""SELECT id, account, package, remark, status, 
                           created_at, accepted_at, completed_at, NULL
                     FROM orders 
                     WHERE user_id = ?
                     ORDER BY created_at DESC LIMIT 100""", (session['user_id'],))
    
    rows = c.fetchall()
    conn.close()
    
    orders = []
    for row in rows:
        order = {
            "id": row[0], 
            "account": row[1],
            "package": PLAN_LABELS_ZH.get(row[2], row[2]),
            "remark": row[3], 
            "status": row[4],
            "status_text": STATUS_TEXT_ZH.get(row[4], row[4]),
            "created_at": row[5], 
            "accepted_at": row[6], 
            "completed_at": row[7],
            "can_cancel": row[4] == STATUS['SUBMITTED'] and not session.get('is_admin')
        }
        
        # 如果是管理员，显示订单创建者
        if session.get('is_admin') and row[8]:
            order["creator"] = row[8]
        if session.get('is_admin') and row[9]:
            order["accepted_by"] = row[9]

        orders.append(order)
    
    return jsonify(orders)

@app.route('/orders/cancel/<int:oid>', methods=['POST'])
@login_required
def cancel_order(oid):
    conn = sqlite3.connect("orders.db")
    c = conn.cursor()
    
    # 检查订单是否属于当前用户且状态为已提交
    c.execute("SELECT status, user_id FROM orders WHERE id=?", (oid,))
    row = c.fetchone()
    
    if row and row[0] == STATUS['SUBMITTED'] and row[1] == session['user_id']:
        c.execute("UPDATE orders SET status=? WHERE id=?", (STATUS['CANCELLED'], oid))
        conn.commit()
    
    conn.close()
    return '', 204

# 添加手动备份API
@app.route('/backup/manual', methods=['POST'])
@login_required
def manual_backup():
    """手动触发备份（仅管理员）"""
    if not session.get('is_admin'):
        return jsonify({"error": "Unauthorized"}), 403
    
    try:
        asyncio.create_task(create_daily_backup())
        return jsonify({"success": True, "message": "备份任务已启动"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ===== Bot 命令处理函数 =====
async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Welcome! Use /stats to see reports.")

async def on_feedback_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    if data.startswith('done_'):
        oid = int(data.split('_')[1])
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        execute_query("UPDATE orders SET status=?, completed_at=? WHERE id=? AND accepted_by=?",
                     (STATUS['COMPLETED'], timestamp, oid, str(user_id)))
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Completed", callback_data="noop")]]))
    elif data.startswith('fail_'):
        oid = int(data.split('_')[1])
        feedback_waiting[user_id] = oid
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Waiting for reason...", callback_data="noop")]]))
        await context.bot.send_message(chat_id=user_id, text="Please send the reason for failure:")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in feedback_waiting:
        oid = feedback_waiting.pop(user_id)
        reason = update.message.text[:200]
        execute_query("UPDATE orders SET status=?, remark=? WHERE id=? AND accepted_by=?",
                     (STATUS['FAILED'], reason, oid, str(user_id)))
        
        await update.message.reply_text(f"❌ Order #{oid} marked as failed.\nReason: {reason}")

# ===== 统计功能 =====
async def on_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示统计菜单"""
    user_id = update.effective_user.id
    
    # 创建日期选择按钮
    keyboard = [
        [InlineKeyboardButton("📊 Today", callback_data=f"stats_{user_id}_today"),
         InlineKeyboardButton("📊 Yesterday", callback_data=f"stats_{user_id}_yesterday")],
        [InlineKeyboardButton("📊 This Week", callback_data=f"stats_{user_id}_week"),
         InlineKeyboardButton("📊 This Month", callback_data=f"stats_{user_id}_month")],
        [InlineKeyboardButton("📊 Custom Date", callback_data=f"stats_{user_id}_custom")]
    ]
    
    # 如果是卖家，添加查看所有人统计的选项
    if is_telegram_admin(user_id):
        keyboard.append([InlineKeyboardButton("👥 All Users Stats", callback_data="stats_all_today")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Please select the time period for statistics:", reply_markup=reply_markup)

async def on_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理统计按钮回调"""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    # 处理返回菜单
    if data == "stats_menu":
        # 重新显示统计菜单
        keyboard = [
            [InlineKeyboardButton("📊 Today", callback_data=f"stats_{user_id}_today"),
             InlineKeyboardButton("📊 Yesterday", callback_data=f"stats_{user_id}_yesterday")],
            [InlineKeyboardButton("📊 This Week", callback_data=f"stats_{user_id}_week"),
             InlineKeyboardButton("📊 This Month", callback_data=f"stats_{user_id}_month")],
            [InlineKeyboardButton("📊 Custom Date", callback_data=f"stats_{user_id}_custom")]
        ]
        
        if is_telegram_admin(user_id):
            keyboard.append([InlineKeyboardButton("👥 All Users Stats", callback_data="stats_all_today")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Please select the time period for statistics:", reply_markup=reply_markup)
        return
    
    # 解析回调数据
    parts = data.split('_')
    
    if len(parts) >= 3 and parts[0] == 'stats':
        target_user = parts[1]
        period = parts[2]
        
        # 计算日期
        today = datetime.now()
        if period == 'today':
            date_str = today.strftime("%Y-%m-%d")
            period_text = "Today"
        elif period == 'yesterday':
            date_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")
            period_text = "Yesterday"
        elif period == 'week':
            # 本周统计
            start_date = today - timedelta(days=today.weekday())
            await show_period_stats(query, target_user, start_date, today, "This Week")
            return
        elif period == 'month':
            # 本月统计
            start_date = today.replace(day=1)
            await show_period_stats(query, target_user, start_date, today, "This Month")
            return
        elif period == 'custom':
            await query.answer("Please use command /stats YYYY-MM-DD to view specific date", show_alert=True)
            return
        
        # 查询单日统计
        if target_user == 'all':
            await show_all_stats(query, date_str, period_text)
        else:
            await show_personal_stats(query, int(target_user), date_str, period_text)

async def show_personal_stats(query, user_id, date_str, period_text):
    """显示个人统计 - 只统计完成的订单薪资"""
    # 修改查询：只统计已完成的订单来计算薪资
    orders = execute_query("""SELECT package, 
                        COUNT(*) as total_accepted,
                        SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as completed_count
                 FROM orders
                 WHERE date(accepted_at)=? AND accepted_by=? AND status IN (?, ?, ?)
                 GROUP BY package""",
              (STATUS['COMPLETED'], date_str, str(user_id), STATUS['ACCEPTED'], STATUS['COMPLETED'], STATUS['FAILED']), fetch=True)
    
    if not orders:
        await query.edit_message_text(f"📊 No orders accepted on {period_text.lower()}")
        return
    
    # 计算统计 - 只有完成的订单才计入薪资
    text = f"📊 **Personal Statistics for {period_text}**\n"
    text += f"📅 Date: {date_str}\n\n"
    
    total_accepted = 0
    total_completed = 0
    total_amount = 0  # 只统计完成订单的薪资
    
    for order in orders:
        if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
            pkg, accepted_count, completed_count = order['package'], order['total_accepted'], order['completed_count']
        else:
            pkg, accepted_count, completed_count = order
        
        # 只有完成的订单才计入薪资
        pkg_amount = completed_count * TG_PRICES.get(pkg, 0)
        total_accepted += accepted_count
        total_completed += completed_count
        total_amount += pkg_amount
        
        text += f"• {PLAN_LABELS_EN.get(pkg, pkg)}: {accepted_count} accepted, {completed_count} completed (${pkg_amount:.2f})\n"
    
    text += f"\n**Total: {total_accepted} accepted, {total_completed} completed**\n"
    text += f"**Salary: ${total_amount:.2f}** (only completed orders)"
    
    # 添加返回按钮
    keyboard = [[InlineKeyboardButton("\U0001F519 Back", callback_data="stats_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_period_stats(query, user_id, start_date, end_date, period_text):
    """显示时间段统计 - 只统计完成的订单薪资"""
    # 查询时间段数据，区分接单总数和完成数
    if user_id == 'all':
        orders = execute_query("""SELECT accepted_by, package, 
                            COUNT(*) as total_accepted,
                            SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as completed_count
                     FROM orders
                     WHERE date(accepted_at) BETWEEN ? AND ? AND status IN (?, ?, ?)
                     GROUP BY accepted_by, package""",
                  (STATUS['COMPLETED'], start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"),
                   STATUS['ACCEPTED'], STATUS['COMPLETED'], STATUS['FAILED']), fetch=True)
    else:
        orders = execute_query("""SELECT package, 
                            COUNT(*) as total_accepted,
                            SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as completed_count
                     FROM orders
                     WHERE date(accepted_at) BETWEEN ? AND ? AND accepted_by=? AND status IN (?, ?, ?)
                     GROUP BY package""",
                  (STATUS['COMPLETED'], start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"),
                   str(user_id), STATUS['ACCEPTED'], STATUS['COMPLETED'], STATUS['FAILED']), fetch=True)
    
    if not orders:
        await query.edit_message_text(f"📊 No orders accepted during {period_text.lower()}")
        return
    
    # 生成统计文本 - 只统计完成订单的薪资
    if user_id == 'all':
        text = f"📊 **All Users Statistics for {period_text}**\n"
        text += f"📅 {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}\n\n"
        
        grouped = defaultdict(lambda: {'accepted': defaultdict(int), 'completed': defaultdict(int)})
        for order in orders:
            if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
                uid, pkg, accepted_count, completed_count = order['accepted_by'], order['package'], order['total_accepted'], order['completed_count']
            else:
                uid, pkg, accepted_count, completed_count = order
            grouped[uid]['accepted'][pkg] += accepted_count
            grouped[uid]['completed'][pkg] += completed_count
        
        grand_total = 0
        for uid, data in grouped.items():
            # 只计算完成订单的薪资
            user_total = sum(count * TG_PRICES.get(pkg, 0) for pkg, count in data['completed'].items())
            total_accepted = sum(data['accepted'].values())
            total_completed = sum(data['completed'].values())
            grand_total += user_total
            text += f"**User {uid}: {total_accepted} accepted, {total_completed} completed - ${user_total:.2f}**\n"
        
        text += f"\n**Grand Total Salary: ${grand_total:.2f}** (only completed orders)"
    else:
        text = f"📊 **Personal Statistics for {period_text}**\n"
        text += f"📅 {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}\n\n"
        
        total_accepted = 0
        total_completed = 0
        total_amount = 0
        
        for order in orders:
            if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
                pkg, accepted_count, completed_count = order['package'], order['total_accepted'], order['completed_count']
            else:
                pkg, accepted_count, completed_count = order
            pkg_amount = completed_count * TG_PRICES.get(pkg, 0)
            total_accepted += accepted_count
            total_completed += completed_count
            total_amount += pkg_amount
            text += f"• {PLAN_LABELS_EN.get(pkg, pkg)}: {accepted_count} accepted, {completed_count} completed (${pkg_amount:.2f})\n"
        
        text += f"\n**Total: {total_accepted} accepted, {total_completed} completed**\n"
        text += f"**Salary: ${total_amount:.2f}** (only completed orders)"
    
    # 添加返回按钮
    keyboard = [[InlineKeyboardButton("\U0001F519 Back", callback_data="stats_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_all_stats(query, date_str, period_text):
    """显示所有人统计 - 只统计完成的订单薪资"""
    orders = execute_query("""SELECT accepted_by, package, 
                        COUNT(*) as total_accepted,
                        SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) as completed_count
                 FROM orders
                 WHERE date(accepted_at)=? AND status IN (?, ?, ?)
                 GROUP BY accepted_by, package""",
              (STATUS['COMPLETED'], date_str, STATUS['ACCEPTED'], STATUS['COMPLETED'], STATUS['FAILED']), fetch=True)
    
    if not orders:
        await query.edit_message_text(f"📊 No orders accepted on {period_text.lower()}")
        return
    
    text = f"📊 **All Users Statistics for {period_text}**\n"
    text += f"📅 Date: {date_str}\n\n"
    
    grouped = defaultdict(lambda: {'accepted': defaultdict(int), 'completed': defaultdict(int)})
    for order in orders:
        if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
            uid, pkg, accepted_count, completed_count = order['accepted_by'], order['package'], order['total_accepted'], order['completed_count']
        else:
            uid, pkg, accepted_count, completed_count = order
        grouped[uid]['accepted'][pkg] += accepted_count
        grouped[uid]['completed'][pkg] += completed_count
    
    grand_total = 0
    for uid, data in grouped.items():
        # 只计算完成订单的薪资
        total_salary = sum(count * TG_PRICES.get(pkg, 0) for pkg, count in data['completed'].items())
        total_accepted = sum(data['accepted'].values())
        total_completed = sum(data['completed'].values())
        grand_total += total_salary
        
        text += f"**User {uid}: {total_accepted} accepted, {total_completed} completed - ${total_salary:.2f}**\n"
        for pkg, accepted_count in data['accepted'].items():
            completed_count = data['completed'][pkg]
            text += f"  • {PLAN_LABELS_EN.get(pkg, pkg)}: {accepted_count} accepted, {completed_count} completed\n"
        text += "\n"
    
    text += f"**Grand Total Salary: ${grand_total:.2f}** (only completed orders)"
    
    # 添加返回按钮
    keyboard = [[InlineKeyboardButton("\U0001F519 Back", callback_data="stats_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')

# ===== 启动函数 =====
async def run_bot():
    """运行 Telegram Bot"""
    global bot_application
    
    # 创建 Bot 应用
    bot_application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # 注册处理器
    bot_application.add_handler(CommandHandler("start", on_start))
    bot_application.add_handler(CommandHandler("seller", on_admin_command))  # 卖家管理命令
    bot_application.add_handler(CommandHandler("stats", on_stats))
    bot_application.add_handler(CallbackQueryHandler(on_accept, pattern=r"^accept_\d+$"))
    bot_application.add_handler(CallbackQueryHandler(on_feedback_button, pattern=r"^(done|fail)_\d+$"))
    bot_application.add_handler(CallbackQueryHandler(on_stats_callback, pattern=r"^stats_"))
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
    
    # 启动每日备份调度器
    schedule_daily_backup()
    
    # 启动 Flask
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Flask server on port {port}...")
    app.run(host="0.0.0.0", port=port)