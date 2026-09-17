"""
MongoDB 客户端管理器

统一创建和管理 MongoDB 客户端，服务于会话历史与商品名确认记忆的读写。
"""

from typing import Optional

from pymongo import MongoClient

from app.rag.conf.mongo_config import mongo_config
from app.core.exceptions import ClientInitError, MongoError
from app.core.logger import logger

# 会话历史集合名：按 session_id 查询某次对话的全部消息
CHAT_MESSAGE_COLLECTION = "chat_message"


class HistoryMongoTool:
    """
    MongoDB 会话历史读写工具

    封装连接、集合获取与索引创建，为上层提供统一的数据库操作入口。
    """

    def __init__(self):
        # 连接串与库名来自配置（敏感信息不硬编码）
        self.mongo_url = mongo_config.mongo_url
        self.db_name = mongo_config.db_name

        if not self.mongo_url:
            raise ClientInitError("Mongo 配置缺失：请在 .env 中配置 MONGO_URL")

        # serverSelectionTimeoutMS 调小：连不上时快速失败，避免启动阶段长时间卡住
        self.client = MongoClient(self.mongo_url, serverSelectionTimeoutMS=5000)
        self.db = self.client[self.db_name]
        self.chat_message = self.db[CHAT_MESSAGE_COLLECTION]

        # 复合索引：session_id 升序 + ts 降序，匹配「按会话取最新消息」这一核心查询
        self.chat_message.create_index([("session_id", 1), ("ts", -1)])

        logger.info(f"MongoDB 连接成功：{self.db_name}")


class MongoClientManager:
    def __init__(self, mongo_config):
        # 保存配置，init() 时按它建立连接
        self.mongo_config = mongo_config
        # 声明为 None，真正的连接建立放到 init() 里
        self.client: Optional[HistoryMongoTool] = None

    def init(self):
        # 幂等：已初始化则直接返回，避免重复建连
        if self.client is not None:
            return
        try:
            self.client = HistoryMongoTool()
        except ClientInitError:
            raise
        except Exception as e:
            raise ClientInitError("MongoDB 建连失败", cause=e) from e

    def close(self):
        # 进程退出前关闭连接，释放网络资源
        if self.client is not None:
            try:
                self.client.client.close()
            except Exception as e:
                # 关闭失败不应影响进程退出流程，仅记录
                logger.warning(f"关闭 MongoDB 连接时出现异常：{e}")
            self.client = None


# 全局可复用的 MongoDB 客户端管理器单例
mongo_client_manager = MongoClientManager(mongo_config)
