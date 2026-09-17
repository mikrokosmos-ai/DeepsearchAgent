"""
Milvus 客户端门面
"""

from app.rag.clients.manager.milvus_client_manager import milvus_client_manager


def get_milvus_client():
    """获取 Milvus 客户端；未初始化时触发一次懒加载兜底"""
    if milvus_client_manager.client is None:
        milvus_client_manager.init()
    return milvus_client_manager.client
