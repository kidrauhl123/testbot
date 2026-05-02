import logging
from functools import wraps
from datetime import datetime

from flask import request, render_template, session, redirect, url_for

from modules.database import execute_query, hash_password, get_china_time

logger = logging.getLogger(__name__)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def register_auth_routes(app):
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')

            if not username or not password:
                return render_template('login.html', error='请填写用户名和密码')

            # 验证用户
            hashed_password = hash_password(password)
            user = execute_query("SELECT id, username, is_admin FROM users WHERE username=%s AND password_hash=%s",
                            (username, hashed_password), fetch=True)

            if user:
                user_id, username, is_admin = user[0]
                session['user_id'] = user_id
                session['username'] = username
                session['is_admin'] = is_admin

                # 更新最后登录时间
                execute_query("UPDATE users SET last_login=%s WHERE id=%s",
                            (get_china_time(), user_id))

                logger.info(f"用户 {username} 登录成功")

                # 检查是否有待处理的激活码
                if 'pending_activation_code' in session:
                    code = session.pop('pending_activation_code')

                    # 如果同时有账号密码，直接跳转到激活码页面
                    if 'pending_account' in session and 'pending_password' in session:
                        session.pop('pending_account')
                        session.pop('pending_password')
                        return redirect(url_for('redeem_page', code=code))

                    return redirect(url_for('redeem_page', code=code))

                return redirect(url_for('index'))
            else:
                logger.warning(f"用户 {username} 登录失败 - 密码错误")
                return render_template('login.html', error='用户名或密码错误')

        # 检查是否有激活码参数
        code = request.args.get('code')
        if code:
            session['pending_activation_code'] = code

        return render_template('login.html')

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            confirm_password = request.form.get('password_confirm')  # 修正字段名称

            # 验证输入
            if not username or not password or not confirm_password:
                return render_template('register.html', error='请填写所有字段')

            if password != confirm_password:
                return render_template('register.html', error='两次密码输入不一致')

            # 检查用户名是否已存在
            existing_user = execute_query("SELECT id FROM users WHERE username=%s", (username,), fetch=True)
            if existing_user:
                return render_template('register.html', error='用户名已存在')

            # 创建用户
            hashed_password = hash_password(password)
            execute_query("""
                INSERT INTO users (username, password_hash, is_admin, created_at)
                VALUES (%s, %s, 0, %s)
            """, (username, hashed_password, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

            return redirect(url_for('login'))

        return render_template('register.html')

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))
