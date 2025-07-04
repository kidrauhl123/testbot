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
    cursor.execute("SELECT id, account, status, updated_at, created_at FROM orders ORDER BY id DESC LIMIT 10")
    orders = cursor.fetchall()
    print("\n订单表内容(最近10条):")
    for order in orders:
        print(order)
    
    # 检查充值成功的订单
    cursor.execute("SELECT id, status, updated_at, created_at FROM orders WHERE status='充值成功'")
    success_orders = cursor.fetchall()
    print("\n充值成功的订单:")
    for order in success_orders:
        print(order)
    
    # 检查今日订单
    import datetime
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    cursor.execute("SELECT id, status, updated_at, created_at FROM orders WHERE updated_at LIKE ?", (f"{today}%",))
    today_orders = cursor.fetchall()
    print(f"\n今日({today})订单:")
    for order in today_orders:
        print(order)
        
    # 检查今日充值成功的订单
    cursor.execute("SELECT id, status, updated_at, created_at FROM orders WHERE status='充值成功' AND updated_at LIKE ?", (f"{today}%",))
    today_success_orders = cursor.fetchall()
    print(f"\n今日({today})充值成功的订单:")
    for order in today_success_orders:
        print(order)
except Exception as e:
    print(f"\n查询订单表出错: {e}")

# 关闭连接
conn.close() 