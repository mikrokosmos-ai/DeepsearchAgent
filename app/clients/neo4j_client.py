"""
Neo4j 客户端门面
"""

from app.clients.manager.neo4j_client_manager import neo4j_client_manager


def get_neo4j_driver():
    """获取 Neo4j driver；未初始化时触发一次懒加载兜底"""
    if neo4j_client_manager.driver is None:
        neo4j_client_manager.init()
    return neo4j_client_manager.driver
