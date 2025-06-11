from modules.database import execute_query

# 添加accepted_by_username字段到orders表
try:
    execute_query('ALTER TABLE orders ADD COLUMN accepted_by_username TEXT')
    print("成功添加accepted_by_username字段")
except Exception as e:
    print(f"添加字段时出错: {str(e)}") 