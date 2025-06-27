import sqlite3
import os

# 连接数据库
db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.db")
print(f"数据库路径: {db_path}")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# 检查订单表结构
cursor.execute("SELECT * FROM sqlite_master WHERE type='table' AND name='orders'")
table_info = cursor.fetchone()
if table_info:
    print("\n订单表结构:")
    print(table_info[4])  # 表创建SQL
else:
    print("\n订单表不存在!")

# 检查订单表内容
try:
    cursor.execute("SELECT id, account, package, status, created_at FROM orders ORDER BY id DESC LIMIT 10")
    orders = cursor.fetchall()
    print("\n订单表内容(最近10条):")
    for order in orders:
        print(order)
except Exception as e:
    print(f"\n查询订单表出错: {e}")

# 关闭连接
conn.close() 