"""
MongoDB 客户端门面
"""

from app.clients.manager.mongo_client_manager import mongo_client_manager


def get_history_mongo_tool():
    """获取会话历史读写工具；未初始化时触发一次懒加载兜底"""
    if mongo_client_manager.client is None:
        mongo_client_manager.init()
    return mongo_client_manager.client
