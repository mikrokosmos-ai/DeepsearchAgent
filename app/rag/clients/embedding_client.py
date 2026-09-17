"""
Embedding 客户端门面
"""

from app.rag.clients.manager.embedding_client_manager import embedding_client_manager


def get_bge_m3_ef():
    """获取 BGE-M3 模型单例；未初始化时触发一次懒加载兜底"""
    if embedding_client_manager.client is None:
        embedding_client_manager.init()
    return embedding_client_manager.client


def generate_embeddings(texts):
    """
    为文本列表生成稠密 + 稀疏混合向量

    :param texts: 待向量化文本列表，单条文本也需封装为列表
    :return: {"dense": list[list[float]], "sparse": list[dict[int, float]]}
    """
    return embedding_client_manager.encode(texts)
