"""
Redis 客户端门面

与 mongo_client.py 同构：只暴露无状态的 get_xxx() 函数，gate 住 manager 单例，
并带「懒加载兜底」—— 即使 lifespan 未预热（或预热失败），首次调用也会尝试建连，
把失败点收敛到真正使用该能力的那一次调用上。
"""

from app.rag.clients.manager.redis_client_manager import redis_client_manager


def get_redis_client():
    """获取 Redis 客户端；未初始化时触发一次懒加载兜底（失败会抛 ClientInitError）"""
    if redis_client_manager.client is None:
        redis_client_manager.init()
    return redis_client_manager.client
