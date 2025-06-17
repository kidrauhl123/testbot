# 闲鱼自动发货机器人使用指南

## 功能介绍

闲鱼自动发货机器人可以自动监控闲鱼卖家平台的新订单，自动为订单创建激活码，并通过闲鱼的无需物流发货功能发送激活码兑换链接给买家。

## 环境要求

- Python 3.7+
- Chrome浏览器
- 已登录的闲鱼卖家账号

## 安装步骤

1. 安装所需的Python包：

```bash
pip install -r requirements.txt
```

2. 复制配置文件模板并进行配置：

```bash
cp xianyu_config.json.example xianyu_config.json
```

3. 编辑配置文件 `xianyu_config.json`：

```json
{
  "api_key": "xianyu_secret_key",  // 与服务端配置的API密钥一致
  "api_base_url": "https://your-service-domain.com",  // 您的服务地址
  "xianyu_username": "您的闲鱼账号",  // 通常不需要填写
  "xianyu_password": "您的密码",  // 通常不需要填写
  "check_interval": 300,  // 检查新订单的间隔时间（秒）
  "headless": false,  // 是否隐藏浏览器界面
  "browser_data_dir": "C:\\Users\\用户名\\AppData\\Local\\Google\\Chrome\\User Data\\XianyuProfile"  // Chrome用户数据目录
}
```

### 配置说明

- **api_key**: 与服务端配置的`XIANYU_API_KEY`环境变量需一致
- **api_base_url**: 您的激活码系统的网址
- **check_interval**: 检查新订单的时间间隔（秒）
- **headless**: 是否在后台运行浏览器（无界面模式）
  - 首次运行建议设置为`false`，便于调试
  - 稳定后可设为`true`在后台运行
- **browser_data_dir**: Chrome浏览器用户数据目录
  - 使用单独的用户配置文件可以避免影响您日常使用的浏览器
  - 首次运行时会自动创建此配置文件

## 服务端配置

在您的激活码系统服务器上，需要设置以下环境变量：

```
XIANYU_API_KEY=xianyu_secret_key
```

确保此密钥与机器人配置中的`api_key`一致。

## 使用方法

1. 首次运行（建议设置`headless: false`）：

```bash
python xianyu_delivery_bot.py
```

2. 首次运行时，浏览器会打开并要求您登录闲鱼卖家账号。登录后，机器人会记住您的登录状态。

3. 后续运行时可以使用后台模式（`headless: true`）：

```bash
python xianyu_delivery_bot.py
```

## 自动发货流程

1. 机器人定期检查闲鱼卖家平台的待发货订单
2. 从订单商品标题中提取套餐信息（如"1个月"、"3个月"等）
3. 调用API创建对应套餐的激活码
4. 构造包含激活码和兑换链接的发货信息
5. 通过闲鱼无需物流功能发送激活码给买家
6. 记录已处理的订单信息到本地文件

## 套餐识别规则

机器人会自动从商品标题中识别套餐信息，支持以下格式：

- "1个月"、"2个月"、"3个月"、"6个月"、"12个月"
- "一个月"、"三个月"、"六个月"、"十二个月"、"一年"

如需添加更多套餐识别规则，请编辑`xianyu_delivery_bot.py`文件中的`package_mapping`字典。

## 商品发布建议

为了让机器人能准确识别套餐，建议商品标题中明确包含套餐信息，例如：

- "会员充值 1个月 自动发货"
- "VIP服务 12个月 即时到账"

## 常见问题

1. **问题**：机器人无法登录闲鱼
   **解决方案**：确保`browser_data_dir`配置正确，并在首次运行时手动登录

2. **问题**：无法识别套餐信息
   **解决方案**：检查商品标题是否包含支持的套餐关键词，或添加新的套餐映射

3. **问题**：API调用失败
   **解决方案**：确认`api_key`和`api_base_url`配置正确，并检查网络连接

4. **问题**：机器人启动后立即退出
   **解决方案**：检查日志文件`xianyu_bot.log`查看详细错误信息

## 定时任务设置

### Windows系统

可以使用任务计划程序创建定时任务，在计算机启动时自动运行机器人：

1. 打开任务计划程序
2. 创建基本任务
3. 触发器选择"计算机启动时"
4. 操作选择"启动程序"
5. 程序路径填写`python.exe`，参数填写`xianyu_delivery_bot.py`，起始位置填写脚本所在目录

### Linux系统

可以使用crontab创建定时任务：

```bash
@reboot cd /path/to/script && python3 xianyu_delivery_bot.py
```

## 安全提示

- 请勿将配置文件提交到公开的代码仓库
- 定期更改API密钥以提高安全性
- 建议为机器人单独创建一个Chrome用户配置文件

## 日志查看

机器人运行日志保存在`xianyu_bot.log`文件中，可以通过查看此文件了解机器人的运行状态和可能的错误。 