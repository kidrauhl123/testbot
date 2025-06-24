# YouTube会员充值系统

这是一个简单高效的YouTube会员充值系统，允许用户通过上传YouTube账号二维码快速购买一年期会员套餐。

## 功能特点

- **简化的充值流程**：只需上传YouTube二维码，选择套餐，完成支付
- **自动处理**：订单自动分配给客服处理
- **Telegram Bot集成**：通过Telegram机器人接收订单通知和处理订单
- **账户管理**：用户可以查看自己的订单历史和账户余额
- **一年期会员**：支持YouTube Premium一年期充值

## 技术栈

- **后端**：Python, Flask
- **数据库**：SQLite/PostgreSQL
- **消息系统**：Telegram Bot API
- **前端**：HTML, CSS, JavaScript

## 安装与配置

### 环境要求

- Python 3.7+
- pip包管理器
- Telegram Bot Token

### 安装步骤

1. 克隆仓库
```bash
git clone https://github.com/yourusername/youtube-recharge-system.git
cd youtube-recharge-system
```

2. 安装依赖
```bash
pip install -r requirements.txt
```

3. 配置环境变量
```bash
# Telegram Bot配置
export BOT_TOKEN=your_telegram_bot_token

# 管理员账号
export ADMIN_USERNAME=your_admin_username
export ADMIN_PASSWORD=your_admin_password

# 卖家Telegram ID（可选）
export SELLER_CHAT_IDS=id1,id2,id3
```

4. 初始化数据库
```bash
python check_db.py
```

5. 启动应用
```bash
python app.py
```

### 生产环境部署

可以使用gunicorn在生产环境运行:

```bash
gunicorn app:app
```

## 使用指南

### 用户流程

1. 访问网站首页
2. 上传YouTube账号二维码（支持拖拽、粘贴或点击上传）
3. 选择一年期会员套餐
4. 完成支付后等待处理
5. 通过网站查看订单状态

### 管理员功能

1. 登录管理员后台
2. 查看所有订单状态
3. 管理用户余额
4. 配置系统参数

### Telegram Bot命令

卖家可使用的命令:
- `/accept [订单号]` - 接单
- `/stats` - 查看统计数据

## 常见问题

**Q: 充值需要多长时间?**  
A: 通常在接单后24小时内完成。

**Q: 支持哪些YouTube账号?**  
A: 支持全球大部分地区的YouTube账号。

**Q: 如何获取YouTube二维码?**  
A: 在YouTube应用中点击个人头像 -> 设置 -> 管理Google账号 -> 搜索"获取QR码"。

## 贡献

欢迎提交Issue和Pull Request。

## 许可证

MIT 