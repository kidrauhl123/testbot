# 精简版二维码转发系统

这是一个精简版的二维码转发系统，只保留了二维码转发和TG反馈功能。

## 功能

1. **二维码转发**：用户上传二维码图片，系统将图片转发给Telegram卖家
2. **TG反馈**：卖家通过Telegram机器人接收通知并处理订单

## 安装

1. 克隆代码库
2. 安装依赖：`pip install -r requirements_minimal.txt`
3. 设置环境变量：
   - `TELEGRAM_BOT_TOKEN`：Telegram机器人的API令牌
   - `SELLER_IDS`：卖家的Telegram ID，多个ID用逗号分隔

## 运行

```bash
python app_minimal.py
```

默认情况下，系统将在 `http://localhost:5000` 上运行。

## 目录结构

- `app_minimal.py`：主应用程序
- `modules_minimal/`：精简版模块
  - `database.py`：数据库操作
  - `telegram_bot.py`：Telegram机器人功能
  - `web_routes.py`：Web路由
  - `constants.py`：常量定义
- `templates/minimal/`：精简版模板
  - `index.html`：首页模板 