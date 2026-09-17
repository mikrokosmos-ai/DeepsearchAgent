"""
MinIO 客户端门面
"""

from app.rag.clients.manager.minio_client_manager import minio_client_manager


def get_minio_client():
    """获取 MinIO 客户端；未初始化时触发一次懒加载兜底"""
    if minio_client_manager.client is None:
        minio_client_manager.init()
    return minio_client_manager.client
