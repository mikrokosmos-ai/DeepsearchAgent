"""
RAG 能力域

聚合从 EcomKbAgent 移植的自建 RAG 全流程：配置层、客户端层、数据访问层
与 import / query 两条 LangGraph 流水线。

本包依赖 app.core（日志/路径/异常）与 app.utils、app.prompts 中的共用资产；
反向依赖不存在 —— 主流程通过 app.tools.local_rag_tool 单向调用本包。

接入面仍留在主流程原目录（属「实现层隔离」）：
    app/api/kb_routes.py       HTTP 入库入口
    app/api/rag_event_bridge.py 进度事件桥接
    app/utils/{task_utils,sse_utils,escape_milvus_string_utils,normalize_sparse_vector}.py
    app/prompts/loader.py + templates/
"""
