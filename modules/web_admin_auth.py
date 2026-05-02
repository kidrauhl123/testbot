from functools import wraps

from flask import jsonify, session


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({"error": "管理员权限不足"}), 403
        return f(*args, **kwargs)
    return decorated_function
