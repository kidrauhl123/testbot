import logging

from flask import jsonify, redirect, render_template, request, session, url_for

from modules.constants import STATUS, STATUS_TEXT_ZH
from modules.database import (
    execute_query,
    get_activation_code,
    get_china_time,
    get_postgres_connection,
    get_user_balance,
)

logger = logging.getLogger(__name__)


def register_redeem_routes(app):
    @app.route('/redeem', methods=['GET'])
    def redeem_page():
        """激活码兑换页面"""
        # 从URL获取激活码参数
        code = request.args.get('code', '')
        
        try:
            orders = execute_query("SELECT id, account, package, status, created_at FROM orders ORDER BY id DESC LIMIT 5", fetch=True)
            
            # 如果有激活码参数，检查是否已被使用，并获取相关订单信息
            order_info = None
            code_info = None
            
            # 1. 检查URL中的激活码
            if code:
                code_info = get_activation_code(code)
                if code_info:
                    # 查找使用此激活码创建的订单
                    order_query = execute_query(
                        "SELECT id, account, package, status, created_at, completed_at, remark FROM orders WHERE remark LIKE %s ORDER BY id DESC LIMIT 1", 
                        (f"%通过激活码兑换: {code}%",), 
                        fetch=True
                    )
                    if order_query and len(order_query) > 0:
                        order = order_query[0]
                        order_info = {
                            "id": order[0],
                            "account": order[1],
                            "package": order[2],
                            "status": order[3],
                            "status_text": STATUS_TEXT_ZH.get(order[3], order[3]),
                            "created_at": order[4],
                            "completed_at": order[5] or "",
                            "remark": order[6]
                        }
            
            # 2. 如果URL中没有激活码或没找到订单，检查session中的上次兑换记录
            if not order_info and 'last_redeemed_code' in session and 'last_order_id' in session:
                last_code = session.get('last_redeemed_code')
                last_order_id = session.get('last_order_id')
                
                # 查询订单详情
                order_query = execute_query(
                    "SELECT id, account, package, status, created_at, completed_at, remark FROM orders WHERE id = %s", 
                    (last_order_id,), 
                    fetch=True
                )
                
                if order_query and len(order_query) > 0:
                    order = order_query[0]
                    order_info = {
                        "id": order[0],
                        "account": order[1],
                        "package": order[2],
                        "status": order[3],
                        "status_text": STATUS_TEXT_ZH.get(order[3], order[3]),
                        "created_at": order[4],
                        "completed_at": order[5] or "",
                        "remark": order[6]
                    }
                    
                    # 如果URL没有激活码，但session有，使用session中的激活码
                    if not code:
                        code = last_code
                        code_info = get_activation_code(code)
            
            return render_template('redeem.html', 
                                   code=code,
                                   orders=orders, 
                                   status_text=STATUS_TEXT_ZH,
                                   username=session.get('username'),
                                   is_admin=session.get('is_admin'),
                                   balance=get_user_balance(session.get('user_id', 0)),
                                   order_info=order_info,
                                   code_info=code_info)
        except Exception as e:
            logger.error(f"加载兑换页面失败: {str(e)}", exc_info=True)
            return render_template('redeem.html', 
                                   code=code,
                                   error='加载数据失败', 
                                   username=session.get('username'),
                                   is_admin=session.get('is_admin'))

    @app.route('/redeem/<code>', methods=['GET'])
    def redeem_with_code(code):
        """带激活码的兑换链接"""
        return redirect(url_for('redeem_page', code=code))

    @app.route('/api/verify-code', methods=['POST'])
    def verify_activation_code():
        """验证激活码"""
        try:
            code = request.json.get('code', '')
            
            if not code:
                return jsonify({"success": False, "message": "请输入激活码"}), 400
            
            # 获取激活码信息
            code_info = get_activation_code(code)
            
            # 检查激活码是否存在
            if not code_info:
                logger.warning(f"无效的激活码: {code}")
                return jsonify({"success": False, "message": "无效的激活码"}), 400
            
            # 检查激活码是否已使用
            if code_info['is_used']:
                # 查找使用此激活码创建的订单
                order_query = execute_query(
                    "SELECT id, status FROM orders WHERE remark LIKE %s ORDER BY id DESC LIMIT 1", 
                    (f"%通过激活码兑换: {code}%",), 
                    fetch=True
                )
                
                if order_query and len(order_query) > 0:
                    order_id = order_query[0][0]
                    order_status = order_query[0][1]
                    status_text = STATUS_TEXT_ZH.get(order_status, order_status)
                    logger.warning(f"激活码已被使用: {code}, 关联订单 #{order_id}, 状态: {status_text}")
                    return jsonify({
                        "success": False, 
                        "message": f"此激活码已被使用，关联订单 #{order_id}，状态: {status_text}"
                    }), 400
                else:
                    logger.warning(f"激活码已被使用: {code}, 但未找到关联订单")
                    return jsonify({"success": False, "message": "此激活码已被使用"}), 400
            
            # 返回成功和套餐信息
            return jsonify({
                "success": True, 
                "package": code_info['package'],
                "message": "有效的激活码"
            })
                
        except Exception as e:
            logger.error(f"验证激活码失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "message": "验证失败，请稍后再试"}), 500

    @app.route('/redeem', methods=['POST'])
    def process_redeem():
        """处理激活码兑换请求"""
        try:
            # 从JSON获取数据
            data = request.json
            code = data.get('code', '')
            account = data.get('account', '')
            password = data.get('password', '')
            remark = data.get('remark', '')
            
            if not code:
                return jsonify({"success": False, "error": "请输入激活码"}), 400
            
            if not account or not password:
                return jsonify({"success": False, "error": "请输入账号和密码"}), 400
            
            # 获取激活码信息
            code_info = get_activation_code(code)
            
            # 检查激活码是否存在
            if not code_info:
                logger.warning(f"无效的激活码: {code}")
                return jsonify({"success": False, "error": "无效的激活码"}), 400
            
            # 检查激活码是否已使用 - 使用数据库事务确保原子性
            if code_info['is_used']:
                # 查找使用此激活码创建的订单
                order_query = execute_query(
                    "SELECT id, status FROM orders WHERE remark LIKE %s ORDER BY id DESC LIMIT 1", 
                    (f"%通过激活码兑换: {code}%",), 
                    fetch=True
                )
                
                if order_query and len(order_query) > 0:
                    order_id = order_query[0][0]
                    order_status = order_query[0][1]
                    status_text = STATUS_TEXT_ZH.get(order_status, order_status)
                    logger.warning(f"激活码已被使用: {code}, 关联订单 #{order_id}, 状态: {status_text}")
                    return jsonify({
                        "success": False, 
                        "error": f"此激活码已被使用，关联订单 #{order_id}，状态: {status_text}"
                    }), 400
                else:
                    logger.warning(f"激活码已被使用: {code}, 但未找到关联订单")
                    return jsonify({"success": False, "error": "此激活码已被使用"}), 400
            
            # 用户ID和用户名 - 如果已登录则使用登录信息，否则使用临时值
            user_id = session.get('user_id', 0)  # 未登录用户使用0作为ID
            username = session.get('username', '未登录用户')
            
            # 创建订单记录（状态为已提交，而非已完成）
            now = get_china_time()
            order_id = None
            
            # 使用数据库事务确保原子性操作
            conn = None
            cursor = None
            try:
                conn = get_postgres_connection()
                cursor = conn.cursor()
                conn.autocommit = False

                # 1. 先检查激活码是否仍然可用
                cursor.execute(
                    "SELECT id, is_used FROM activation_codes WHERE code = %s FOR UPDATE",
                    (code,)
                )
                code_check = cursor.fetchone()
                if not code_check or code_check[1] == 1:
                    conn.rollback()
                    return jsonify({"success": False, "error": "此激活码已被使用或不存在"}), 400

                # 2. 创建订单
                cursor.execute("""
                    INSERT INTO orders (account, password, package, remark, status, created_at, user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    account,
                    password,
                    code_info['package'],
                    f"通过激活码兑换: {code}",
                    STATUS['SUBMITTED'],
                    now,
                    user_id
                ))
                order_id = cursor.fetchone()[0]

                # 3. 标记激活码为已使用
                cursor.execute("""
                    UPDATE activation_codes
                    SET is_used = 1, used_at = %s, used_by = %s
                    WHERE id = %s
                """, (now, user_id if user_id > 0 else None, code_info['id']))

                conn.commit()

                # 记录成功日志
                logger.info(f"用户 {username} 成功兑换激活码 {code}, 套餐: {code_info['package']}, 订单ID: {order_id}")

                # 将激活码和订单ID保存到session，以便刷新页面后仍能显示
                session['last_redeemed_code'] = code
                session['last_order_id'] = order_id

            except Exception as e:
                if conn:
                    conn.rollback()
                logger.error(f"激活码兑换事务失败: {str(e)}", exc_info=True)
                return jsonify({"success": False, "error": f"处理激活码兑换失败: {str(e)}"}), 500
            finally:
                if cursor:
                    cursor.close()
                if conn:
                    conn.close()
            
            # 获取完整的订单信息
            order = {
                "id": order_id,
                "account": account,
                "password": password,
                "package": code_info['package'],
                "status": STATUS['SUBMITTED'],
                "status_text": STATUS_TEXT_ZH.get(STATUS['SUBMITTED'], STATUS['SUBMITTED']),
                "created_at": now,
                "completed_at": None,  # 未完成
                "remark": f"通过激活码兑换: {code}",
                "creator": username,
                "accepted_by": "",
                "can_cancel": True  # 已提交的订单可以取消
            }
            
            # 如果用户已登录并成功完成兑换，可以选择重定向到仪表板
            redirect_url = url_for('dashboard') if 'user_id' in session else None
            
            # 返回成功消息和订单数据
            return jsonify({
                "success": True, 
                "message": f"激活码兑换成功，订单已提交，等待处理!",
                "orders": [order],  # 返回包含单个订单的数组
                "redirect": redirect_url,
                "redirect_delay": 3000  # 延迟3秒后重定向，给用户足够时间查看结果
            })
                
        except Exception as e:
            logger.error(f"处理激活码兑换请求失败: {str(e)}", exc_info=True)
            return jsonify({"success": False, "error": "处理请求失败，请稍后再试"}), 500
