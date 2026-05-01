import sqlite3
import os

# 连接数据库
db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.db")
print(f"数据库路径: {db_path}")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# 检查激活码表结构
cursor.execute("SELECT * FROM sqlite_master WHERE type='table' AND name='activation_codes'")
table_info = cursor.fetchone()
if table_info:
    print("\n激活码表结构:")
    print(table_info[4])  # 表创建SQL
else:
    print("\n激活码表不存在!")

# 检查激活码表内容
try:
    cursor.execute("SELECT id, code, package, is_used, used_at, used_by FROM activation_codes LIMIT 10")
    codes = cursor.fetchall()
    print("\n激活码表内容(前10条):")
    for code in codes:
        print(code)
except Exception as e:
    print(f"\n查询激活码表出错: {e}")

# 检查订单表中的激活码相关订单
try:
    cursor.execute("SELECT id, account, package, status, remark FROM orders WHERE remark LIKE '%通过激活码兑换%' LIMIT 10")
    orders = cursor.fetchall()
    print("\n激活码相关订单(前10条):")
    for order in orders:
        print(order)
except Exception as e:
    print(f"\n查询订单表出错: {e}")

# 关闭连接
conn.close() 