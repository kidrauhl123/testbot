import asyncio
import logging

from flask import jsonify

from modules.database import get_china_time
from modules.telegram_bot import check_and_push_orders

logger = logging.getLogger(__name__)


def register_utility_routes(app, notification_queue):
    # 添加一个测试路由
    @app.route('/test')
    def test_route():
        logger.info("访问测试路由")
        return jsonify({
            'status': 'ok',
            'message': '服务器正常运行',
            'time': get_china_time(),
        })

    # 添加一个路由用于手动触发订单检查
    @app.route('/check-orders')
    def manual_check_orders():
        logger.info("手动触发订单检查")
        
        try:
            # 检查机器人实例
            if notification_queue is None:
                return jsonify({
                    'status': 'error',
                    'message': 'Telegram机器人实例未初始化'
                })
            
            # 创建事件循环并执行订单检查
            asyncio.run(check_and_push_orders())
            
            return jsonify({
                'status': 'ok',
                'message': '订单检查已触发',
                'time': get_china_time()
            })
        except Exception as e:
            logger.error(f"手动触发订单检查失败: {str(e)}", exc_info=True)
            return jsonify({
                'status': 'error',
                'message': f'触发失败: {str(e)}'
            })
