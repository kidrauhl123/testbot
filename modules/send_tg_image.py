import telebot
import os
import logging
import sys
from modules.constants import BOT_TOKEN

# 设置日志
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

def send_image_to_telegram(chat_id, image_path, caption=None):
    """
    使用TeleBot库直接发送图片到Telegram
    
    参数:
    chat_id: 接收者的Telegram ID
    image_path: 图片文件的完整路径
    caption: 可选的图片说明文字
    
    返回:
    成功返回True，失败返回False
    """
    try:
        # 检查文件是否存在
        if not os.path.exists(image_path):
            logger.error(f"图片文件不存在: {image_path}")
            return False
            
        # 检查文件大小
        file_size = os.path.getsize(image_path)
        if file_size == 0:
            logger.error(f"图片文件大小为0: {image_path}")
            return False
            
        logger.info(f"开始发送图片 {image_path} 到用户 {chat_id}")
        
        # 创建TeleBot实例
        bot = telebot.TeleBot(BOT_TOKEN)
        
        # 打开并发送文件
        with open(image_path, 'rb') as photo:
            message = bot.send_photo(chat_id, photo, caption=caption)
            
        logger.info(f"图片发送成功: {message.message_id}")
        return True
        
    except Exception as e:
        logger.error(f"发送图片失败: {str(e)}", exc_info=True)
        return False

if __name__ == "__main__":
    # 测试函数
    if len(sys.argv) < 3:
        print("用法: python send_tg_image.py <chat_id> <image_path> [caption]")
        sys.exit(1)
        
    chat_id = sys.argv[1]
    image_path = sys.argv[2]
    caption = sys.argv[3] if len(sys.argv) > 3 else None
    
    success = send_image_to_telegram(chat_id, image_path, caption)
    print(f"发送结果: {'成功' if success else '失败'}") 