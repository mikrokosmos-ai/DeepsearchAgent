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

# 主智能体会话集合名：只存「主智能体」这一层的人机问答。
AGENT_MESSAGE_COLLECTION = "agent_message"

# 任务运行元数据集合名：每次任务收尾写一条（耗时/结局/工具用量/故障与截断路数）。
AGENT_RUN_COLLECTION = "agent_run"

# 知识库文档登记集合名：文档级管理（编辑/重建/启停/软删）的锚点登记表。
# MD 是唯一事实源，本集合只登记元数据，不存正文。
KB_DOC_COLLECTION = "kb_document"

# 统一会话模型（阶段 2）：conversations 一行一个会话，messages 一行一条消息。
# 旧集合 chat_message / agent_message 保留为只读兜底，不再写入。
CONVERSATION_COLLECTION = "conversations"
MESSAGE_COLLECTION = "messages"


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
        # 主智能体的问答历史（与 RAG 多轮历史分离，见上方常量注释）
        self.agent_message = self.db[AGENT_MESSAGE_COLLECTION]
        # 每次任务的运行元数据（可观测性与评测）
        self.agent_run = self.db[AGENT_RUN_COLLECTION]
        # 知识库文档登记
        self.kb_document = self.db[KB_DOC_COLLECTION]
        # 统一会话模型（新写入目标）
        self.conversation = self.db[CONVERSATION_COLLECTION]
        self.message = self.db[MESSAGE_COLLECTION]

        # 复合索引：session_id 升序 + ts 降序，匹配「按会话取最新消息」这一核心查询
        self.chat_message.create_index([("session_id", 1), ("ts", -1)])
        self.agent_message.create_index([("session_id", 1), ("ts", -1)])
        self.agent_run.create_index([("session_id", 1), ("ts", -1)])
        # doc_id 唯一：登记表的业务主锚点
        self.kb_document.create_index([("doc_id", 1)], unique=True)
        # 列表页核心查询：按状态过滤 + 最新在前
        self.kb_document.create_index([("status", 1), ("ts", -1)])
        # 会话按 session_id 唯一（thread_id 即会话主键）；另建用户维度的列举索引
        self.conversation.create_index([("session_id", 1)], unique=True)
        self.conversation.create_index([("user_id", 1), ("updated_at", -1)])
        # 消息的两条读取路径：按「会话 + 层」取窗口；按会话整体回读
        self.message.create_index([("session_id", 1), ("layer", 1), ("ts", -1)])
        self.message.create_index([("session_id", 1), ("ts", -1)])

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
