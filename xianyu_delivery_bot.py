#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import requests
import logging
import argparse
import schedule
from datetime import datetime
import pytz
import re
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('xianyu_bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger('xianyu_bot')

# 中国时区
CN_TIMEZONE = pytz.timezone('Asia/Shanghai')

def get_china_time():
    """获取当前中国时间（UTC+8）"""
    utc_now = datetime.now(pytz.utc)
    china_now = utc_now.astimezone(CN_TIMEZONE)
    return china_now.strftime("%Y-%m-%d %H:%M:%S")

class XianyuBot:
    def __init__(self, config_file):
        self.config = self.load_config(config_file)
        self.api_key = self.config.get('api_key', os.environ.get('XIANYU_API_KEY', 'xianyu_secret_key'))
        self.api_base_url = self.config.get('api_base_url', 'http://localhost:5000')
        self.xianyu_username = self.config.get('xianyu_username', '')
        self.xianyu_password = self.config.get('xianyu_password', '')
        self.check_interval = self.config.get('check_interval', 300)  # 默认5分钟检查一次
        self.processed_orders = self.load_processed_orders()
        self.driver = None
        self.headless = self.config.get('headless', True)
        self.browser_data_dir = self.config.get('browser_data_dir', '')  # Chrome用户数据目录

        # 套餐映射
        self.package_mapping = {
            "1个月": "1",
            "2个月": "2", 
            "3个月": "3",
            "6个月": "6",
            "12个月": "12",
            "一个月": "1",
            "三个月": "3",
            "六个月": "6",
            "十二个月": "12",
            "一年": "12"
        }

    def load_config(self, config_file):
        """加载配置文件"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"无法加载配置文件 {config_file}: {str(e)}")
            return {}

    def load_processed_orders(self):
        """加载已处理的订单"""
        try:
            if os.path.exists('processed_orders.json'):
                with open('processed_orders.json', 'r', encoding='utf-8') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"无法加载已处理订单记录: {str(e)}")
            return {}

    def save_processed_orders(self):
        """保存已处理的订单"""
        try:
            with open('processed_orders.json', 'w', encoding='utf-8') as f:
                json.dump(self.processed_orders, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"无法保存已处理订单记录: {str(e)}")

    def initialize_browser(self):
        """初始化浏览器"""
        try:
            options = Options()
            if self.headless:
                options.add_argument('--headless')
            options.add_argument('--disable-gpu')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-extensions')
            options.add_argument('--window-size=1920,1080')
            options.add_argument('--start-maximized')
            options.add_argument('--disable-blink-features=AutomationControlled')
            options.add_experimental_option('excludeSwitches', ['enable-automation'])
            options.add_experimental_option('useAutomationExtension', False)

            # 如果提供了用户数据目录，则使用它
            if self.browser_data_dir:
                options.add_argument(f'--user-data-dir={self.browser_data_dir}')

            self.driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
            self.driver.execute_script('Object.defineProperty(navigator, "webdriver", {get: () => undefined})')
            logger.info("浏览器初始化完成")
            return True
        except Exception as e:
            logger.error(f"浏览器初始化失败: {str(e)}")
            return False

    def login_to_xianyu(self):
        """登录到闲鱼"""
        try:
            # 打开闲鱼
            self.driver.get('https://seller.idle.taobao.com/')
            
            # 检查是否已经登录
            try:
                if WebDriverWait(self.driver, 10).until(EC.visibility_of_element_located((By.CSS_SELECTOR, '.avatar-wrapper'))):
                    logger.info("已检测到登录状态")
                    return True
            except:
                logger.info("未检测到登录状态，尝试登录")

            # 如果使用了用户数据目录，可能已经登录了，再次检查
            if self.browser_data_dir:
                try:
                    self.driver.get('https://seller.idle.taobao.com/')
                    time.sleep(5)
                    if '闲鱼' in self.driver.title:
                        logger.info("已通过用户数据目录登录")
                        return True
                except:
                    pass

            # 这里需要根据闲鱼的实际登录流程进行修改
            # 由于登录可能涉及扫码等复杂操作，这里假设已通过用户数据目录自动登录
            # 如需手动登录，可以在这里添加等待时间，让用户手动操作
            
            logger.info("请在浏览器中手动完成登录操作")
            time.sleep(60)  # 等待用户手动登录
            
            # 检查登录状态
            try:
                if WebDriverWait(self.driver, 10).until(EC.visibility_of_element_located((By.CSS_SELECTOR, '.avatar-wrapper'))):
                    logger.info("登录成功")
                    return True
                else:
                    logger.error("登录失败，未检测到用户头像")
                    return False
            except:
                logger.error("登录失败，超时或元素未找到")
                return False
        except Exception as e:
            logger.error(f"登录闲鱼过程中出错: {str(e)}")
            return False

    def check_new_orders(self):
        """检查新订单"""
        try:
            # 打开订单页面
            self.driver.get('https://seller.idle.taobao.com/order.htm?tab=3')
            time.sleep(5)
            
            # 等待订单列表加载
            WebDriverWait(self.driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '.trade-order-list'))
            )
            
            # 找到等待发货的订单
            pending_orders = self.driver.find_elements(By.CSS_SELECTOR, '.trade-order-item')
            logger.info(f"发现 {len(pending_orders)} 个订单")
            
            for order in pending_orders:
                try:
                    # 获取订单ID
                    order_id_elem = order.find_element(By.CSS_SELECTOR, '.trade-order-id')
                    order_id = order_id_elem.text.replace('订单号：', '').strip()
                    
                    # 检查是否已处理
                    if order_id in self.processed_orders:
                        logger.debug(f"订单 {order_id} 已处理，跳过")
                        continue
                    
                    # 获取商品标题
                    title_elem = order.find_element(By.CSS_SELECTOR, '.trade-order-item-info-title')
                    title = title_elem.text.strip()
                    logger.info(f"发现新订单: {order_id}, 商品: {title}")
                    
                    # 从标题中提取套餐信息
                    package = self.extract_package_from_title(title)
                    if not package:
                        logger.warning(f"无法从标题 '{title}' 提取套餐信息，跳过此订单")
                        continue
                    
                    # 调用API创建激活码
                    activation_code = self.create_activation_code(order_id, package)
                    if not activation_code:
                        logger.error(f"为订单 {order_id} 创建激活码失败，跳过")
                        continue
                    
                    # 自动发货
                    if self.send_delivery(order, activation_code):
                        logger.info(f"订单 {order_id} 自动发货成功")
                        self.processed_orders[order_id] = {
                            "time": get_china_time(),
                            "title": title,
                            "package": package,
                            "code": activation_code['code'],
                            "redeem_url": activation_code['redeem_url']
                        }
                        self.save_processed_orders()
                    else:
                        logger.error(f"订单 {order_id} 自动发货失败")
                except Exception as e:
                    logger.error(f"处理订单时出错: {str(e)}")
                    continue
        except Exception as e:
            logger.error(f"检查新订单过程中出错: {str(e)}")

    def extract_package_from_title(self, title):
        """从商品标题中提取套餐信息"""
        for key in self.package_mapping:
            if key in title:
                return self.package_mapping[key]
        
        # 尝试直接匹配数字+月
        pattern = r'(\d+)个*月'
        match = re.search(pattern, title)
        if match:
            months = match.group(1)
            if months in self.package_mapping.values():
                return months
        
        return None

    def create_activation_code(self, order_id, package):
        """调用API创建激活码"""
        try:
            url = f"{self.api_base_url}/api/xianyu/auto-delivery"
            headers = {
                'Content-Type': 'application/json',
                'X-API-Key': self.api_key
            }
            data = {
                'package': package,
                'order_id': order_id
            }
            
            response = requests.post(url, headers=headers, json=data)
            if response.status_code == 200:
                result = response.json()
                if result.get('success'):
                    logger.info(f"创建激活码成功: {result.get('code')}")
                    return result
                else:
                    logger.error(f"创建激活码API返回错误: {result.get('error')}")
                    return None
            else:
                logger.error(f"创建激活码API请求失败，状态码: {response.status_code}")
                return None
        except Exception as e:
            logger.error(f"调用创建激活码API出错: {str(e)}")
            return None

    def send_delivery(self, order_element, activation_code):
        """对订单进行发货操作"""
        try:
            # 构建发货信息
            redeem_url = activation_code['redeem_url']
            code = activation_code['code']
            package_name = activation_code['package_name']
            
            delivery_message = f"""
感谢购买，您的激活码已生成：

兑换地址: {redeem_url}

激活码: {code}

套餐: {package_name}

使用方法:
1. 打开上方链接，登录账号
2. 输入激活码进行兑换
3. 如有问题请联系客服

祝您使用愉快！
            """
            
            # 点击发货按钮
            try:
                # 找到当前订单的发货按钮
                delivery_btn = order_element.find_element(By.XPATH, ".//button[contains(text(), '发货') or contains(text(), '填写运单')]")
                delivery_btn.click()
                time.sleep(2)
            except Exception as e:
                logger.error(f"无法找到发货按钮: {str(e)}")
                return False
            
            # 等待发货弹窗出现
            try:
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, '.delivery-dialog'))
                )
            except:
                logger.error("发货弹窗未出现")
                return False
            
            # 选择无需物流
            try:
                no_logistics_btn = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), '无需物流')]"))
                )
                no_logistics_btn.click()
                time.sleep(1)
            except:
                logger.info("已经在无需物流页面或未找到无需物流按钮")
            
            # 填写发货信息
            try:
                # 查找文本输入框
                message_textarea = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'textarea[placeholder*="填写发货说明"]'))
                )
                message_textarea.clear()
                message_textarea.send_keys(delivery_message)
                time.sleep(1)
            except Exception as e:
                logger.error(f"无法填写发货信息: {str(e)}")
                return False
            
            # 确认发货
            try:
                confirm_btn = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), '确定发货')]"))
                )
                confirm_btn.click()
                time.sleep(3)
                
                # 检查是否有额外的确认弹窗
                try:
                    final_confirm_btn = WebDriverWait(self.driver, 5).until(
                        EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), '确定') or contains(text(), '确认')]"))
                    )
                    final_confirm_btn.click()
                    time.sleep(2)
                except:
                    logger.info("无额外确认弹窗")
                
                return True
            except Exception as e:
                logger.error(f"确认发货失败: {str(e)}")
                return False
            
        except Exception as e:
            logger.error(f"发货过程中出错: {str(e)}")
            return False

    def run(self):
        """运行机器人"""
        if not self.initialize_browser():
            logger.error("浏览器初始化失败，无法运行机器人")
            return
        
        try:
            if not self.login_to_xianyu():
                logger.error("登录失败，无法运行机器人")
                return
            
            # 首次检查
            self.check_new_orders()
            
            # 设置定时任务
            schedule.every(self.check_interval).seconds.do(self.check_new_orders)
            
            logger.info(f"机器人已启动，每 {self.check_interval} 秒检查一次新订单")
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("机器人被手动中止")
        except Exception as e:
            logger.error(f"机器人运行出错: {str(e)}")
        finally:
            if self.driver:
                self.driver.quit()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='闲鱼自动发货机器人')
    parser.add_argument('--config', default='xianyu_config.json', help='配置文件路径')
    args = parser.parse_args()
    
    bot = XianyuBot(args.config)
    bot.run() 