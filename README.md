# DeepsearchAgent（研搜智能体）

> 面向电商 / 通用研究场景的 **DeepAgents 多智能体「深度研搜」系统**。  
> 主智能体编排「数据库问数 / 联网搜索 / 本地知识库 RAG」三个子智能体，配合 WebSocket 实时进度推送，把用户的开放问题拆解成多步研搜任务，最终产出带引用、可附配图的研究报告（Markdown / PDF）。其中本地知识库 RAG 只是被主智能体调度的一种能力，而非项目主体。

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python\&logoColor=white)

![FastAPI](https://img.shields.io/badge/FastAPI-0.100%2B-009688?logo=fastapi\&logoColor=white)

![DeepAgents](https://img.shields.io/badge/DeepAgents-0.5.7-1C3C3C)

![LangGraph](https://img.shields.io/badge/LangGraph-1.1%2B-1C3C3C)

![Milvus](https://img.shields.io/badge/Milvus-2.6-00A1FF?logo=milvus\&logoColor=white)

![uv](https://img.shields.io/badge/uv-managed-6C7AFF?logo=uv\&logoColor=white)

![React](https://img.shields.io/badge/React-19-61DAFB?logo=react\&logoColor=white)

![pnpm](https://img.shields.io/badge/pnpm-10-6C7AFF?logo=pnpm\&logoColor=white)

![Vite](https://img.shields.io/badge/Vite-7-646CFF?logo=vite\&logoColor=white)

---

## 目录

- [一、项目简介](#一项目简介)
  - [1.1 背景](#11-背景)
  - [1.2 核心功能](#12-核心功能)
  - [1.3 关键特性](#13-关键特性)
  - [1.4 技术栈](#14-技术栈)
- [二、系统架构](#二系统架构)
- [三、目录结构](#三目录结构)
- [四、快速开始](#四快速开始)
  - [4.1 环境要求](#41-环境要求)
  - [4.2 安装依赖](#42-安装依赖)
  - [4.3 启动基础设施](#43-启动基础设施)
  - [4.4 配置环境变量](#44-配置环境变量)
  - [4.5 启动服务](#45-启动服务)
  - [4.6 前端开发与构建（React + pnpm）](#46-前端开发与构建react--pnpm)
- [五、基础使用](#五基础使用)
  - [5.1 Web 界面](#51-web-界面)
  - [5.2 调用示例](#52-调用示例)
  - [5.3 接口一览](#53-接口一览)
- [六、模块说明](#六模块说明)
  - [6.1 主智能体编排（DeepAgents）](#61-主智能体编排deepagents)
  - [6.2 导入链路 Import Pipeline（RAG）](#62-导入链路-import-pipelinerag)
  - [6.3 查询链路 Query Pipeline（RAG）](#63-查询链路-query-pipelinerag)
  - [6.4 基础设施层](#64-基础设施层)
- [七、开发指南](#七开发指南)
- [八、测试](#八测试)
- [九、常见问题 FAQ](#九常见问题-faq)
- [十、路线图](#十路线图)

---

## 一、项目简介

### 1.1 背景

深度研究类任务往往不是「一问一答」能解决的：用户可能既要查业务数据库里的销售/库存数字，又要补充公网最新信息，还要引用内部产品手册里「图片里的操作步骤」。这些能力各自有独立的数据源与工具链：

| 能力         | 数据源 / 工具                                   | 痛点                                              |
| ---------- | ------------------------------------------ | ----------------------------------------------- |
| **数据库问数**  | 业务 MySQL 数仓（`dw`）                          | 自然语言转 SQL 需要安全护栏，避免误删/误改                          |
| **联网搜索**   | Tavily / 阿里云百炼 MCP WebSearch                | 库外新知识必须走公网，且与本地证据统一排序                          |
| **本地知识库**  | 厂商 PDF 手册（Milvus 向量库 + MinerU 解析 + BGE-M3） | 图文强耦合、型号高度相似，纯文本检索会答不准、会串型号                   |

DeepsearchAgent 用 **DeepAgents 多智能体框架**把三者编排成一个主智能体：主智能体负责理解意图、拆解子任务、调度三个子智能体（数据库问数 / 联网搜索 / 本地知识库），并通过 WebSocket 把每一步的执行轨迹实时推给前端，最终汇总成一份结构化研究报告。

### 1.2 核心功能

1. **多智能体编排（Main Agent）**：基于 DeepAgents（`create_deep_agent`）组装主智能体，内置 `generate_markdown` / `convert_md_to_pdf` / `read_file_content` 三个工具，并挂载 `database_query_agent` / `network_search_agent` / `local_knowledge_agent` 三个子智能体；用 `InMemorySaver` 做会话级 checkpointer。
2. **数据库问数（DB Sub-Agent）**：`db_tools` 连接业务 MySQL 数仓，执行自然语言转写的 SQL 查询并返回结果（含安全约束）。
3. **联网搜索（Web Sub-Agent）**：`tavily_tool` 调 Tavily，查询链路另通过 `openai-agents` 的 MCP Streamable HTTP 调阿里云百炼 WebSearch 兜底。
4. **本地知识库 RAG（KB Sub-Agent）**：导入链路把 PDF/Markdown 解析、切分、向量化写入 Milvus；查询链路以「多路召回 → RRF 融合 → Cross-Encoder 重排 → 引用生成」回答，答案可回链原文与图片。该链路被封装为 `local_rag_search` 工具，挂在 `local_knowledge_agent` 上，由主智能体间接触发。
5. **实时进度推送**：后端 `monitor` 按 `thread_id` 通过 `WebSocket /ws/{thread_id}` 推送「任务开始 / 子智能体调用 / 工具结果 / 异常 / 取消」等事件，前端逐事件渲染时间线。
6. **多格式产出与文件管理**：研究报告可生成 Markdown 并转 PDF（ReportLab）；支持文件上传、输出文件浏览（`/api/files`）、安全下载（`/api/download`，限定 `output/` 根目录防路径穿越）。

### 1.3 关键特性

- **多智能体分工**：主智能体做规划与汇总，三个子智能体各管一类数据源，能力边界清晰、可独立扩展。
- **本地知识库复用完整 RAG 链路**：多模态入库（VLM 把插图转语义文本 + MinIO 存原图）、主体感知检索（`item_name` 标量过滤防串型号）、稠密+稀疏混合检索（BGE-M3 + `WeightedRanker`）、HyDE 增强召回、联网兜底、动态 TopK 断崖重排——与独立 RAG 系统一致。
- **WebSocket 实时可观测**：前端 Socket 连接带 25s 心跳与自动重连，进度不依赖轮询。
- **任务可取消**：`POST /api/task/{thread_id}/cancel` 向后台 asyncio 任务注入 `CancelledError`，前端实时显示 `cancelled` / `cancelling`。
- **提示词工程化**：主/子智能体提示词在 `app/prompts/agents.yml`，RAG 模板在 `app/prompts/templates/*.prompt`，统一用 `load_prompt(name, **kwargs)` 渲染，改提示词不改代码。
- **配置集中 + 可调参数外置**：外部连接信息从 `.env` 读取；RAG 的切分/重排/检索等可调参数收敛到 `app/rag/conf/*_config.py`。
- **统一生命周期管理**：`lifespan` 按必需/可选分级初始化外部客户端——Milvus、MongoDB、MySQL 失败即阻断启动；MinIO、Neo4j、本地模型失败仅告警降级。
- **第三方 API 限流**：内置滑动窗口限速器（默认 60 秒 9 次），防止批量导入触发 VLM/LLM 平台限流。
- **运行时目录统一收敛到仓库根**：`output/`（会话工作区 + 交付文档）、`updated/`（上传暂存）、`logs/`（日志），均由 `app/core/paths.PROJECT_ROOT` 推导，不写死层级。

### 1.4 技术栈

| 层次         | 技术选型                                                                              |
| ---------- | --------------------------------------------------------------------------------- |
| 语言 / 依赖管理  | 后端 Python 3.12（仅 3.12，`requires-python = ">=3.12,<3.13"`）+ [uv](https://docs.astral.sh/uv/)；前端 Node.js ≥ 18.12 + [pnpm](https://pnpm.io/) |
| Web 框架     | 后端 FastAPI + Uvicorn（CORS、WebSocket、BackgroundTasks）；前端经 Vite 代理调用             |
| 前端         | React 19.2 + TypeScript 5.9 + Vite 7.3 + Tailwind CSS 4.1 + Ant Design 5.26 + react-markdown 10 |
| 编排框架       | [DeepAgents](https://github.com/langchain-ai/deepagents) 0.5.7（底层 LangGraph 1.1.10 / LangChain 1.2.17） |
| Agent 工具协议 | OpenAI Agents SDK（`openai-agents` 0.4.2）+ MCP Streamable HTTP（百炼 WebSearch）            |
| 文档解析       | MinerU（`mineru-open-sdk`）云端解析                                                       |
| 向量模型       | BGE-M3（dense 1024 维 + sparse，FP16）                                                  |
| 重排模型       | BAAI/bge-reranker-large（Cross-Encoder）                                              |
| 大语言模型      | 任意 OpenAI 兼容接口（默认阿里云 DashScope `qwen` 系列，文本 `qwen-max` / `qwen3.7-flash`，VLM `qwen-vl-max`） |
| 向量数据库      | Milvus 2.6.23（AUTOINDEX / SPARSE_INVERTED_INDEX，IP 度量）                                |
| 对象存储       | MinIO（业务图片资产持久化）                                                              |
| 会话/图谱存储    | MongoDB 7（历史）、Neo4j 5.26（知识图谱，可选）、MySQL 8（业务数仓）                            |
| 联网检索       | Tavily + 阿里云百炼 MCP WebSearch                                                       |
| 日志         | Loguru（控制台 + 文件双输出，按天滚动、自动清理）                                                 |
| 基础设施       | Docker Compose（MySQL / Milvus / etcd / MinIO ×2 / Attu / MongoDB / Neo4j）             |

---

## 二、系统架构

```
                       ┌───────────────── 主智能体 (DeepAgents) ─────────────────┐
 用户提问 ──WebSocket──▶│ create_deep_agent(                                       │
 (thread_id)            │   model, system_prompt,                                  │
                        │   tools=[generate_markdown, convert_md_to_pdf,           │
                        │          read_file_content],                             │
                        │   subagents=[db_agent, web_agent, kb_agent],             │
                        │   checkpointer=InMemorySaver())                          │
                        │        │                                                 │
                        │        ├─▶ database_query_agent ── db_tools ──────────▶ MySQL(dw)
                        │        ├─▶ network_search_agent ── tavily_tool ────────▶ Tavily / 百炼 MCP
                        │        └─▶ local_knowledge_agent ── local_rag_search ──▶ 查询链路（见下）
                        └───────────────────────────┬─────────────────────────────┘
                                                    │ monitor 按 thread_id 推送事件
                                                    ▼
                                          前端(React) WebSocket /ws/{thread_id}
```

RAG 两条 LangGraph 流水线（被 `local_knowledge_agent` 以工具形式调用）：

```
                         ┌──────────────────── Import Pipeline (LangGraph) ────────────────────┐
   PDF / MD ──upload──▶  │ node_entry ─▶ node_pdf_to_md ─▶ node_md_img ─▶ node_document_split  │
                         │   (类型判定)      (MinerU)      (VLM+MinIO)       (语义切分)          │
                         │                                     │                               │
                         │                                     ▼                               │
                         │        node_import_milvus ◀── node_bge_embedding ◀── node_item_name_recognition │
                         │          (写入向量库)          (BGE-M3 稠密+稀疏)        (LLM 识别商品主体)        │
                         │                                     │                               │
                         │                                     ▼                               │
                         │                            node_import_kg（知识图谱，可选）              │
                         └──────────────────────────────────┬─────────────────────────────────┘
                                                            │
                                                     Milvus (chunks)
                                                            │
   ┌──────────────────── Query Pipeline (LangGraph) ────────┴─────────────────────────────────┐
   │                                                                                          │
   │  node_item_name_confirm ──(改写 + 主体识别 + 澄清判定)──┬─▶ 需澄清 ─▶ node_answer_output   │
   │                                                        │                                  │
   │                           ┌────────────────────────────┴───────────┐                     │
   │                           ▼              ▼             ▼            │                     │
   │                 node_search_embedding  node_search_embedding_hyde  node_web_search_mcp    │
   │                    (混合检索·item_name过滤)   (HyDE 假设文档)         (MCP 联网)             │
   │                           └────────────────┬───────────┘            │                     │
   │                                            ▼                        │                     │
   │                           node_rrf (RRF 倒排融合 k=60)              │                     │
   │                                            ▼                        │                     │
   │                     node_rerank (bge-reranker-large 动态 TopK) ◀────┘                     │
   │                                            ▼                                            │
   │                              node_answer_output (引用生成 + 返回结果)                      │
   └──────────────────────────────────────────────────────────────────────────────────────────┘
```

> 主智能体通过 DeepAgents 的 `task` 工具调用 `local_knowledge_agent`，后者把问题交给 `local_rag_search`（`app/tools/local_rag_tool.py`，`@tool` 包装 `query_app.invoke`），从而触发上面的查询链路。导入链路则由 `POST /api/kb/import` 后台触发，与主对话解耦。

---

## 三、目录结构

```
DeepsearchAgent/
├── main.py                       # 占位模板（不参与运行，真实入口见 4.5）
├── pyproject.toml                # 后端项目元数据与依赖声明（uv 管理，仅支持 Python 3.12）
├── uv.lock                       # 后端依赖锁定文件
├── .env                          # 本地环境变量（含密钥，已在 .gitignore 中排除）
├── .env.example                  # 环境变量样例（脱敏占位符，可提交；cp .env.example .env 后填写）
├── docker/                       # 基础设施编排（不在仓库根）
│   ├── docker-compose.yaml       # 8 个服务：MySQL / MinIO / etcd / milvus-minio / milvus / Attu / mongo / neo4j
│   └── mysql/mysql.sql           # MySQL 初始化脚本
│
├── app/
│   ├── agent/                    # ── 主/子智能体编排层 ──
│   │   ├── main_agent.py         #    run_deep_agent()：组装 DeepAgents 主智能体并执行
│   │   ├── llm.py                #    模型加载（init_chat_model，读 LLM_QWEN_MAX）
│   │   └── subagents/            #    database_query_agent / network_search_agent / local_knowledge_agent
│   │
│   ├── api/                      # ── 接口层 ──
│   │   ├── server.py             #    FastAPI 应用 + 全部 REST/WS 路由（真实入口）
│   │   ├── monitor.py            #    ConnectionManager：按 thread_id 推送事件
│   │   ├── context.py            #    会话目录 / thread_id 的 ContextVar
│   │   ├── lifespan.py           #    启动/关闭时的外部客户端初始化与释放
│   │   ├── kb_routes.py          #    /api/kb/import、/api/kb/task/{id}、/api/kb/tasks
│   │   └── rag_event_bridge.py   #    RAG 事件桥接（导入进度 → 前端）
│   │
│   ├── tools/                    # ── 工具层 ──
│   │   ├── local_rag_tool.py     #    local_rag_search：@tool 包装 RAG 查询链路（供 kb 子智能体）
│   │   ├── db_tools.py           #    数据库问数工具（MySQL dw）
│   │   ├── tavily_tool.py        #    Tavily 联网搜索工具
│   │   ├── pdf_tools.py          #    PDF 工具（转 PDF 等，依赖 word_converter）
│   │   ├── markdown_tools.py     #    generate_markdown 等
│   │   └── upload_file_read_tool.py  # 读取用户上传文件
│   │
│   ├── utils/                    # ── 工具函数 ──
│   │   ├── path_utils.py         #    路径辅助
│   │   ├── word_converter.py     #    Markdown → PDF（ReportLab 渲染器）
│   │   ├── task_utils.py         #    任务状态追踪 + 节点名中文化
│   │   ├── sse_utils.py          #    SSE/事件队列封装
│   │   ├── escape_milvus_string_utils.py  # 过滤表达式转义
│   │   └── normalize_sparse_vector.py     # 稀疏向量归一化
│   │
│   ├── core/                     # ── 核心基建 ──
│   │   ├── logger.py             #    Loguru 配置 + @node_log / @step_log 装饰器
│   │   ├── exceptions.py         #    领域异常体系：AppError 基类 + 四支
│   │   ├── paths.py              #    PROJECT_ROOT 探测（以 .env 为根目录锚点）
│   │   └── rate_limit.py         #    滑动窗口限速器
│   │
│   ├── prompts/                  # ── 提示词层（主/子智能体）──
│   │   ├── loader.py             #    load_prompt(name, **kwargs)：渲染 templates/*.prompt
│   │   ├── agent_loader.py       #    导出 main_agent_content / sub_agents_content
│   │   ├── agents.yml            #    主/子智能体提示词配置
│   │   ├── __init__.py           #    统一导出上述符号
│   │   └── templates/            #    7 个 *.prompt 模板
│   │       ├── product_recognition_system.prompt
│   │       ├── item_name_recognition.prompt
│   │       ├── image_summary.prompt
│   │       ├── knowledge_graph.prompt
│   │       ├── hyde_prompt.prompt
│   │       ├── rewritten_query_and_itemnames.prompt
│   │       └── answer_out.prompt
│   │
│   └── rag/                      # ── 本地知识库 RAG（已归并到本目录）──
│       ├── conf/                 #    配置层（13 个 *_config.py：llm/embedding/reranker/milvus/minio/mongo/neo4j/mineru/bailian_mcp/import_pipeline/query_pipeline…）
│       ├── clients/              #    外部客户端门面 + manager/ 单例管理器
│       ├── repositories/         #    数据访问层（history / vector_search / graph）
│       └── pipelines/            #    业务编排层（LangGraph）
│           ├── import_pipeline/  #    graph.py + state.py + nodes/（8 个导入节点）
│           └── query_pipeline/   #    graph.py + state.py + nodes/（8 个查询节点）
│
├── frontend/                     # 前端工程（React 19 + TS + Vite，需构建）
│   ├── src/                      #   源码：main.tsx / App.tsx / components / hooks / lib / types
│   │   ├── App.tsx               #   两视图：chat（研搜）/ import（知识导入）
│   │   ├── hooks/                #   useDeepAgentSession.ts（WebSocket + 任务编排）、useKbImport.ts
│   │   ├── lib/                  #   api.ts（REST）、config.ts（基址）、thread.ts、nodes.ts、kbApi.ts
│   │   └── components/           #   ChatComposer / ConversationThread / EventStream / ResultPanel / UploadPanel / AgentTopology / importer/*
│   ├── package.json              #   pnpm 管理，packageManager: pnpm@10.33.0
│   ├── vite.config.ts            #   开发代理：/api→8000、/ws→ws://8000；dev 端口 5173
│   └── .env.example              #   VITE_API_BASE_URL / VITE_WS_BASE_URL
│
├── doc/                          # 本地知识库语料（*.pdf，默认不入库，见 .gitignore）
├── output/                       # 运行时产物：按 session_{id} 分层的工作区 + 交付文档
├── updated/                      # 上传暂存：updated/session_{tid}/（对话）、updated/kb_import/{task_id}/（知识库）
├── logs/                         # 运行日志（app_YYYYMMDD.log，默认保留 7 天）
└── docker/volumes/               # Docker 挂载数据卷（MySQL / MinIO / Milvus / 等）
```

---

## 四、快速开始

### 4.1 环境要求

| 项目         | 要求                                                                  | 说明                                                                                  |
| ---------- | ------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| 操作系统       | Windows / Linux / macOS                                             | Windows 已在 Python 3.12 + Docker Desktop 验证                                                  |
| Python     | **仅 3.12**（`requires-python = ">=3.12,<3.13"`）                       | 不要使用 3.11 或 3.13，依赖（pandas 3.x / torch cu130）按 3.12 锁版                                     |
| Node.js    | **≥ 18.12**                                                          | 前端 pnpm@10 的运行要求                                                                   |
| 包管理器（后端） | [uv](https://docs.astral.sh/uv/)                                    | 推荐；`pyproject.toml` 已配置 `[tool.uv]`                                                 |
| 包管理器（前端） | [pnpm](https://pnpm.io/)（v10.33）                                    | 前端依赖管理；`packageManager` 字段已锁定版本                                                  |
| Docker     | Docker Desktop / Engine + Compose v2                                | 用于 MySQL、Milvus、MinIO、MongoDB、Attu、Neo4j                                            |
| GPU（可选但推荐） | NVIDIA GPU + CUDA 13.0 驱动                                         | BGE-M3 与 bge-reranker-large 本地推理；无 GPU 请将 `BGE_DEVICE` / `BGE_RERANKER_DEVICE` 改为 `cpu` |
| 外部服务账号     | MinerU API Token、OpenAI 兼容 LLM API Key（默认 DashScope）、Tavily Key、百炼 WebSearch MCP | 见 4.4                                                                               |
| 磁盘         | ≥ 20GB                                                              | BGE-M3 + bge-reranker-large 约 11GB（含缓存）                                              |

### 4.2 安装依赖

```bash
# 1) 克隆仓库
git clone <your-repo-url> DeepsearchAgent
cd DeepsearchAgent

# 2) 创建虚拟环境并安装后端依赖（uv 会读取 pyproject.toml + uv.lock）
uv venv                 # 默认创建 .venv（Python 3.12）
uv sync                 # 安装锁定版本的全部依赖

# 3) 激活虚拟环境
# Windows (PowerShell)
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate
```

> **关于 PyTorch**：`pyproject.toml` 中 `torch / torchvision` 从 CUDA 13.0 源（`https://download.pytorch.org/whl/cu130`）安装（避免 PyPI 默认解析到 CPU-only 版本）。  
> 若只需 CPU 版本，请在执行 `uv sync` 前临时调整 `[tool.uv.sources]` 中 `torch` / `torchvision` 的 index 指向 CPU 源，避免下载数 GB 的 CUDA 依赖。

不使用 uv 的场景：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r <(uv export --format requirements.txt)   # 或手动 pip install 主要依赖
```

### 4.3 启动基础设施

基础设施编排文件在 **`docker/` 子目录**（不在仓库根）。compose 只读同目录 `.env` 与 shell 环境，而端口等插值变量来自根 `.env`，因此需显式传 `--env-file ../.env`：

```bash
cd docker
docker compose --env-file ../.env up -d

# 查看各服务健康状态（Milvus 首次启动约需 30~90 秒）
docker compose ps
```

| 服务              | 容器名                  | 宿主端口             | 用途                                       |
| --------------- | -------------------- | ---------------- | ---------------------------------------- |
| MySQL           | `deepsearch-mysql`   | `3307` / `3306`  | 业务关系库（dw），初始化脚本 `docker/mysql/mysql.sql`      |
| MinIO           | `deepsearch-minio`   | `9000` / `9001`  | 业务对象存储（图片资产），控制台 `http://127.0.0.1:9001` |
| etcd            | `deepsearch-etcd`    | —                | Milvus 元数据存储                             |
| Milvus 内部 MinIO | `deepsearch-milvus-minio` | `9002` / `9003`  | Milvus 内部对象存储                            |
| Milvus          | `deepsearch-milvus`  | `19530` / `9091` | 向量数据库（gRPC / 健康检查）                       |
| Attu            | `deepsearch-attu`    | `8010`           | Milvus 可视化管理界面（`http://127.0.0.1:8010`）     |
| MongoDB         | `deepsearch-mongo`   | `27017`          | 会话历史存储                                   |
| Neo4j           | `deepsearch-neo4j`   | `7474` / `7687`  | 知识图谱（Browser `http://127.0.0.1:7474` / Bolt `7687`），**可选** |

> ⚠️ **MySQL 库名一致性**：`docker-compose.yaml` 中初始化库名为 `axw0505`（`MYSQL_DATABASE: axw0505`），而根 `.env.example` 的 `MYSQL_DATABASE=deepsearch_db`。后端 `db_tools` 读取 `.env` 的库名。请让两者一致（推荐统一为 `deepsearch_db`，并相应修改 compose 或 `.env`），否则问数子智能体会连错库。

### 4.4 配置环境变量

在项目根目录创建 `.env`（**切勿提交到仓库**，仓库已通过 `.gitignore` 排除）：

```bash
cp .env.example .env        # 直接由样例文件生成，再按实际环境填写各项值
```

`.env.example` 已随仓库提供，含**全部变量的键名与格式说明**（值均为占位符，可安全提交）。填写时请保持键名不变。

| 分类        | 变量                                          | 示例                                                  | 说明                                       |
| --------- | ------------------------------------------- | --------------------------------------------------- | ---------------------------------------- |
| 日志        | `LOG_CONSOLE_ENABLE` / `LOG_CONSOLE_LEVEL`  | `True` / `INFO`                                     | 控制台日志开关与级别                               |
|           | `LOG_FILE_ENABLE` / `LOG_FILE_LEVEL`        | `True` / `INFO`                                     | 文件日志（输出到 `logs/`）                        |
|           | `LOG_FILE_RETENTION`                        | `7 days`                                            | 日志保留时长                                   |
| LLM / VLM | `OPENAI_API_KEY`                            | `sk-...`                                            | OpenAI 兼容接口密钥（同时用于 MCP 鉴权）                   |
|           | `OPENAI_BASE_URL`                           | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容接口地址                              |
|           | `LLM_QWEN_MAX`                              | `qwen-max`                                          | 主智能体文本模型（app/agent/llm.py 使用）             |
|           | `LLM_DEFAULT_MODEL`                         | `qwen3.7-flash`                                     | RAG 链路配置类使用的文本模型                         |
|           | `LLM_DEFAULT_TEMPERATURE`                   | `0.1`                                               | 生成温度                                     |
|           | `VL_MODEL`                                  | `qwen-vl-max`                                       | 多模态模型（图片理解）                              |
| 联网        | `TAVILY_API_KEY`                            | `tvly-...`                                          | Tavily 联网搜索密钥                             |
|           | `MCP_DASHSCOPE_BASE_URL`                    | `https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp` | 百炼 WebSearch MCP 地址                  |
| 文档解析      | `MINERU_API_TOKEN`                          | `sk-...`                                            | MinerU 平台 API Token                        |
|           | `MINERU_BASE_URL`                           | `https://mineru.net/api/v4`                         | MinerU 服务地址                              |
| MySQL     | `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_DATABASE` / `MYSQL_HOST` / `MYSQL_PORT` | `root` / `root` / `deepsearch_db` / `localhost` / `3307` | 业务数仓连接（端口默认 3307，避开本机 3306）    |
| Milvus    | `MILVUS_URL`                                | `http://127.0.0.1:19530`                            | **必须带协议前缀**（pymilvus 3.x 要求）                 |
|           | `CHUNKS_COLLECTION` / `ITEM_NAME_COLLECTION` / `ENTITY_NAME_COLLECTION` | `kd_db_chunks` / `kd_db_item_names` / `kd_db_entity_names` | 三个集合名                        |
| MongoDB   | `MONGO_URL` / `MONGO_DB_NAME`               | `mongodb://127.0.0.1:27017` / `deepsearch_rag`      | 连接串 / 库名                                |
| Neo4j     | `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` / `NEO4J_DATABASE` | `bolt://127.0.0.1:7687` / `neo4j` / `neo4j123456` / `neo4j` | 知识图谱（可选）；密码需与 compose `NEO4J_AUTH` 一致 |
| MinIO     | `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` / `MINIO_BUCKET_NAME` / `MINIO_IMG_DIR` / `MINIO_SECURE` | `127.0.0.1:9000` / `minioadmin` / `minioadmin` / `knowledge-base-files` / `/upload-images` / `False` | 业务图片对象存储                  |
| 向量模型      | `BGE_M3_PATH` / `BGE_M3` / `BGE_DEVICE` / `BGE_FP16` | `<缓存目录>/BAAI--bge-m3/snapshots/master` / `BAAI/bge-m3` / `cuda:0` / `1` | BGE-M3 本地目录 / 仓库标识 / 设备 / 精度  |
| 重排模型      | `BGE_RERANKER_LARGE` / `BGE_RERANKER_DEVICE` / `BGE_RERANKER_FP16` | `<缓存目录>/BAAI--bge-reranker-large/snapshots/master` / `cuda:0` / `1` | bge-reranker-large 本地目录 / 设备 / 精度 |
| 可选        | `WARMUP_ENABLE`                             | `false`                                             | `true` 可在启动时预加载本地模型（默认懒加载）               |

> `EMBEDDING_DIM`（默认 1024）、`MILVUS_PORT` / `MINIO_*_PORT` 等端口变量仅用于 compose 插值，应用层不读取。RAG 的切分粒度、TopK、断崖阈值、检索权重等「业务可调参数」已外置到 `app/rag/conf/import_pipeline_config.py` 与 `query_pipeline_config.py`，键名与默认值见 `.env.example` 的「导入链路调参 / 查询链路调参」段；配置矛盾时服务会在启动阶段抛异常并指出冲突项。

首次运行建议先预下载模型（以 ModelScope 为例）：

```bash
modelscope download --model BAAI/bge-m3 --local_dir <你的缓存目录>/BAAI--bge-m3
modelscope download --model BAAI/bge-reranker-large --local_dir <你的缓存目录>/BAAI--bge-reranker-large
```

### 4.5 启动服务

后端是单进程 FastAPI 服务，**固定监听 8000**，无 `--service` 之类的命令行开关。请在**仓库根目录**执行（模块以 `app` 为顶层包）：

```bash
# 仓库根目录执行（reload 仅开发用）
uvicorn app.api.server:app --host 0.0.0.0 --port 8000 --reload
```

> ⚠️ `app/api/server.py` 的 `__main__` 里写的是 `uvicorn.run("api.server:app", ...)`，但 `server.py` 内部 import 用的是 `from app...`（以仓库根为包根）。因此推荐始终在**仓库根目录**用 `uvicorn app.api.server:app` 启动，避免模块路径歧义。根 `main.py` 只是占位模板，不参与运行。

启动后访问：

| 地址                                       | 内容              |
| ---------------------------------------- | --------------- |
| `http://127.0.0.1:8000/docs`             | Swagger 自动接口文档  |
| `http://localhost:5173`                  | 前端开发态（React，`pnpm run dev`，见 4.6） |
| `ws://127.0.0.1:8000/ws/{thread_id}`     | 实时进度 WebSocket |

> 服务启动时会强制初始化 Milvus、MongoDB、MySQL 连接，失败将直接退出——这是刻意的「快速失败」设计。MinIO、Neo4j 与本地模型属于可选依赖，初始化失败只会打印告警并降级。

### 4.6 前端开发与构建（React + pnpm）

前端位于 `frontend/`，基于 React 19 + TypeScript + Vite 7。开发态由 Vite 提供 dev server（默认 `http://localhost:5173`），并把 `/api`、`/ws` 两类请求代理到后端 `http://localhost:8000`（见 `vite.config.ts`）；接口基址也可经 `frontend/.env` 的 `VITE_API_BASE_URL` / `VITE_WS_BASE_URL` 覆盖。

```bash
cd frontend

# 1) 安装依赖（pnpm 管理，镜像走默认源；若代理干扰可清空后装）
pnpm install

# 2) 启动开发服务器（默认 5173 端口）
pnpm run dev

# 3) 类型检查 + 生产构建（产出 frontend/dist/）
pnpm run build

# 4) 预览构建产物
pnpm run preview
```

> ℹ️ **生产部署**：当前前端为独立 React 工程，后端 `server.py` 未挂载 `dist/` 静态资源，生产态请自行用 `pnpm run build` + 任意静态服务器（或反向代理到 8000）托管；开发态直接用 Vite dev server（5173）即可，无需额外配置。

---

## 五、基础使用

### 5.1 Web 界面

前端为单页应用，根 `App.tsx` 提供两个视图（按需切换）：

- **研搜页（chat）**：输入开放问题，主智能体会调度子智能体并实时把执行轨迹推到页面的事件流（EventStream）；答案可附带来源引用与配图链接，支持导出 Markdown / PDF；左侧可新建 / 切换 / 清空会话。
- **知识导入页（import）**：上传 PDF / Markdown，实时展示导入任务进度与节点状态，导入完成即可回到研搜页让本地知识库子智能体引用。

访问步骤：

1. 启动基础设施（4.3）与后端服务（4.5，`uvicorn app.api.server:app --port 8000`）；
2. 在 `frontend/` 目录执行 `pnpm run dev`，打开 `http://localhost:5173`；
3. 首次使用先到「知识导入」上传商品手册，等待进度完成；
4. 回到「研搜」提问，主智能体会自动在需要时调用本地知识库子智能体。

### 5.2 调用示例

**① 启动一次研搜任务**

```bash
curl -X POST "http://127.0.0.1:8000/api/task" \
  -H "Content-Type: application/json" \
  -d '{"query": "HAK180 烫金机的色带更换步骤和温度设置范围分别是多少？", "thread_id": "demo-001"}'
```

```json
{ "status": "started", "thread_id": "demo-001" }
```

**② 建立 WebSocket 接收实时进度**

```bash
# 用任意 WebSocket 客户端连接；以下为概念示意（前端用浏览器原生 WebSocket）
ws://127.0.0.1:8000/ws/demo-001
# 服务端按 thread_id 推送：任务开始 / 子智能体调用 / 工具结果 / 异常 / 完成；客户端每 25s 发一次 ping 维持心跳
```

**③ 取消任务**

```bash
curl -X POST "http://127.0.0.1:8000/api/task/demo-001/cancel"
```

```json
{ "status": "cancelled", "thread_id": "demo-001" }
```

**④ 上传对话附件**

```bash
curl -X POST "http://127.0.0.1:8000/api/upload" \
  -F "thread_id=demo-001" \
  -F "files=@本地路径/某手册.pdf"
```

**⑤ 导入知识库（后台 RAG 链路）**

```bash
curl -X POST "http://127.0.0.1:8000/api/kb/import" \
  -F "files=@doc/hak180使用说明书.pdf"
```

```json
{ "task_id": "8f1c...-...-...", "status": "importing" }
```

```bash
curl "http://127.0.0.1:8000/api/kb/task/8f1c...-...-..."
# => { "task_id": "...", "status": "processing", "done_list": [...], "running_list": ["文档切分"] }
```

**⑥ 浏览 / 下载产物**

```bash
curl "http://127.0.0.1:8000/api/files?path=<output_dir>/session_demo-001"
curl "http://127.0.0.1:8000/api/download?path=<output_dir>/session_demo-001/report.md"
```

### 5.3 接口一览

**`app/api/server.py`**

| 方法       | 路径                         | 说明                                          |
| -------- | -------------------------- | ------------------------------------------- |
| `POST`   | `/api/task`                | 启动后台 DeepAgents 任务，立即返回 `thread_id`            |
| `POST`   | `/api/task/{thread_id}/cancel` | 取消指定任务，返回 `cancelled` / `cancelling`         |
| `POST`   | `/api/upload`              | 多文件上传（form-data，`thread_id` 表单字段），落盘到 `updated/session_{thread_id}` |
| `GET`    | `/api/download?path=`      | 下载文件（限定 `output/` 根目录，防路径穿越）                 |
| `GET`    | `/api/files?path=`          | 列出 `output/` 下文件元数据（递归）                     |
| `WS`     | `/ws/{thread_id}`          | 实时事件推送（monitor）+ ping/pong 心跳              |

**`app/api/kb_routes.py`（前缀 `/api/kb`）**

| 方法       | 路径                       | 说明                              |
| -------- | ------------------------ | ------------------------------- |
| `POST`   | `/api/kb/import`          | 上传 PDF/MD → 后台 RAG 入库链路（限 `.pdf/.md/.markdown`，单文件 ≤ 100MB） |
| `GET`    | `/api/kb/task/{task_id}`  | 查询单个导入任务进度                       |
| `GET`    | `/api/kb/tasks`           | 列出全部导入任务（按创建时间倒序）                 |

> 当前**未提供** `/api/health` 等健康检查路由；MySQL / Milvus / Mongo 的连通性由 `lifespan` 启动时强制校验（失败即退出）。

---

## 六、模块说明

### 6.1 主智能体编排（DeepAgents）

核心文件：`app/agent/main_agent.py`（`run_deep_agent`）、`app/agent/llm.py`、三个子智能体在 `app/agent/subagents/`。

- **组装**：`main_agent = create_deep_agent(model, system_prompt, tools=[generate_markdown, convert_md_to_pdf, read_file_content], checkpointer=InMemorySaver(), subagents=[database_query_agent, network_search_agent, local_knowledge_agent])`。
- **执行**：`run_deep_agent(query, session_id)` 建 `output/session_{session_id}` → 从 `updated/session_{session_id}` 复制上传文件 → 写 `ContextVar`（会话目录 + thread_id）→ `astream` + `config={"configurable":{"thread_id": session_id}}` → 经 `monitor` 上报轨迹。
- **子智能体**：
  - `database_query_agent`：挂 `db_tools`，查询业务 MySQL 数仓；
  - `network_search_agent`：挂 `tavily_tool`，做公网检索；
  - `local_knowledge_agent`：挂 `local_rag_search`（RAG 查询链路工具），回答本地知识库问题。
- **实时推送**：`app/api/monitor.py` 的 `ConnectionManager` 按 `thread_id` 维护 WebSocket 连接，`run_deep_agent` 在各阶段推送事件；前端 `useDeepAgentSession.ts` 建立连接并处理心跳/重连。

### 6.2 导入链路 Import Pipeline（RAG）

编排文件：`app/rag/pipelines/import_pipeline/graph.py`；状态定义：`state.py` 的 `ImportGraphState`。入口节点 `node_entry` 依文件后缀分流（非 PDF/MD 直接结束）。

| 节点                           | 职责                                                                                                              |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `node_entry`                 | 输入校验、文件类型判定、提取 `file_title`                                                                                     |
| `node_pdf_to_md`             | 调用 MinerU 解析 PDF，产出 Markdown 与插图目录                                                                              |
| `node_md_img`                | 扫描 Markdown 中的图片 → 截取上下文 → VLM 生成语义摘要 → 上传 MinIO → 用「摘要 + 图片 URL」回写 Markdown → 备份原文件                            |
| `node_document_split`        | 清洗 Markdown → 按标题语义切分 → 超长块二次切分（size 200 / overlap 20）→ 合并过碎片（< 100 字符）                                      |
| `node_item_name_recognition` | LLM 识别商品主体名，写入 `item_name` 集合                                                                                    |
| `node_bge_embedding`         | 按批（5 条/批）生成 dense + sparse 向量，单批失败不影响整体                                                                        |
| `node_import_milvus`         | 建集合与索引（dense: AUTOINDEX/IP；sparse: SPARSE_INVERTED_INDEX/IP）→ 按 `item_name` 删除旧数据（幂等重导入）→ 写入新切片       |
| `node_import_kg`             | **可选**：逐切片调 LLM 抽取实体/关系 → 清洗过滤 → 实体向量入实体集合 → 三元组入 Neo4j → 落 `kg.json`；失败只告警、不阻断导入                  |

切片集合 Schema（`CHUNKS_COLLECTION`，稠密 1024 维）：

| 字段                                                    | 类型                  | 说明                |
| ----------------------------------------------------- | ------------------- | ----------------- |
| `chunk_id`                                            | INT64（主键，auto_id）   | 切片唯一标识            |
| `file_title` / `item_name` / `title` / `parent_title` | VARCHAR(512)        | 文档名、商品主体、当前标题、父标题 |
| `part`                                                | INT8                | 分片序号              |
| `content`                                             | VARCHAR(65535)      | 切片正文（图片已被语义描述替换）  |
| `dense_vector`                                        | FLOAT_VECTOR(1024)  | BGE-M3 稠密向量       |
| `sparse_vector`                                       | SPARSE_FLOAT_VECTOR | BGE-M3 稀疏向量       |

### 6.3 查询链路 Query Pipeline（RAG）

编排文件：`app/rag/pipelines/query_pipeline/graph.py`；状态定义：`state.py` 的 `QueryGraphState`。该链路被 `local_rag_search` 工具封装，由 `local_knowledge_agent` 调用。

`node_item_name_confirm` 为入口与「守门人」：问题改写 + 商品主体抽取；若提问为空、主体不确定或库中没有对应主体，则直接短路返回澄清话术，避免无效检索。

| 节点                           | 职责                                                                                                      |
| ---------------------------- | ------------------------------------------------------------------------------------------------------- |
| `node_item_name_confirm`     | 读取历史 → LLM 输出 `{rewritten_query, item_names}` → 与库内主体比对 → 决定继续检索或直接澄清                                   |
| `node_search_embedding`      | 改写问题向量化后在 Milvus 做混合检索，以 `item_name in [...]` 过滤，`WeightedRanker(0.8, 0.2)`、`norm_score=True`、`limit=5` |
| `node_search_embedding_hyde` | LLM 先生成假设性答案，再以「原问题 + 假设答案」联合向量化检索，补充语义召回                                                               |
| `node_web_search_mcp`        | 通过 MCP Streamable HTTP 调百炼 `WebSearch`，为库外问题提供兜底语料                                            |
| `node_query_kg`              | 第 4 路并行召回：问题向量对齐实体集合取种子实体 → Neo4j 一跳扩展 → 组装 `kg_chunks`；**可选**，失败只告警                          |
| `node_rrf`                   | RRF 倒排融合（`(1/(k+rank)) * weight`，`k=60`，取 Top5），融合向量路 / HyDE 路 / 图谱路（权重 0.7）                            |
| `node_rerank`                | 统一本地切片与网页结果 → bge-reranker-large 打分 → 断崖式动态 TopK（阈值 0.25 / 0.5，下限 3 / 上限 10）→ 分区保底（本地至少 3 条）         |
| `node_answer_output`         | 组装引用化 Prompt（本地 / 图谱 / 联网 / 历史四区，上下文上限 12000 字符）→ LLM 生成 → 抽取图片 URL → 返回结果（由调用方落库/推送）             |

### 6.4 基础设施层

| 模块                        | 说明                                                                                 |
| ------------------------- | ---------------------------------------------------------------------------------- |
| `app/core/logger.py`      | Loguru 双通道日志；`@node_log` / `@step_log` 装饰器自动打印节点与步骤的进入、耗时、异常                       |
| `app/core/exceptions.py`  | 领域异常体系：`AppError` 基类，下设配置、导入、查询、存储四支                                              |
| `app/core/paths.py`       | `PROJECT_ROOT` 探测（优先环境变量，否则从 `__file__` 向上找 `.env` 锚点）                               |
| `app/core/rate_limit.py`  | 滑动窗口限速（默认 60 秒 9 次），用于 VLM/LLM 调用保护                                                |
| `app/rag/clients/manager/*` | 各外部客户端的单例管理器，统一 `init()` / `close()`；门面函数（如 `get_milvus_client()`）在 client 为空时自动懒加载兜底 |
| `app/rag/repositories/`   | 数据访问封装：MongoDB 会话历史、Milvus 混合检索与按 ID 批量取回、Neo4j 图谱（纯 Cypher）                     |
| `app/utils/task_utils.py` | 内存态任务追踪（`pending/processing/completed/failed`），含节点名 → 中文名映射，供前端展示                  |
| `app/utils/sse_utils.py`   | 事件队列与封装（导入进度推送）                                                            |

---

## 七、开发指南

项目遵循以下约定，新增代码请保持一致：

1. **提示词外置**：主/子智能体提示词放 `app/prompts/agents.yml`，RAG 模板放 `app/prompts/templates/*.prompt`，用 `load_prompt(name, **kwargs)` / `agent_loader` 渲染，不在代码里硬编码长文本。
2. **配置集中**：新增外部服务时在 `app/rag/conf/` 下新增 dataclass 配置，从 `.env` 读取；主智能体模型配置经 `app/agent/llm.py` 读 `LLM_QWEN_MAX` 等环境变量。
3. **可调参数外置**：凡属「调优类」参数（切分粒度、TopK、断崖/置信度阈值、检索权重、上下文预算），一律加到 `app/rag/conf/import_pipeline_config.py` 或 `query_pipeline_config.py`，禁止在节点里硬编码魔数。
4. **客户端收敛**：所有外部连接通过 `app/rag/clients/manager/` 的单例管理器创建，并在 `lifespan` 中按「必需 / 可选」分级初始化。
5. **状态即契约**：跨节点数据一律通过 `ImportGraphState` / `QueryGraphState`（`TypedDict`）流转，新增字段需同步更新默认状态工厂。
6. **节点分层**：每个 LangGraph 节点内部按 `step_1_xxx` / `step_2_xxx` 拆纯函数步骤，用 `@step_log` 装饰；节点函数用 `@node_log` 装饰并统一调用任务追踪。
7. **日志规范**：运行时路径统一 `from app.core.logger import logger`，禁止 `print`（仅文件底部 `__main__` 自测块允许），也不要 `import logging` 或调用 `logging.basicConfig`，避免与 Loguru 全局配置冲突。
8. **异常规范**：业务失败抛 `app/core/exceptions.py` 中的领域异常（如 `MilvusError` / `StateFieldError`），并携带根因，便于按类型分支处理。
9. **前端规范**：React 组件按 `components/{chat,importer,ui}` 分层；API 调用统一收敛到 `lib/api.ts` / `lib/kbApi.ts`，禁止在组件里直接 `fetch` 裸写地址；状态由 hooks（`useDeepAgentSession` 等）承载，无独立 `store/` 目录。
10. **路径推导铁律**：所有运行时目录一律从 `app.core.paths.PROJECT_ROOT` 推导；同目录内取资源用 `Path(__file__).parent / "x"`。**禁止用「数层数」推根目录**（`Path(__file__).parents[N]`），目录一搬层级就变且极难排查。
11. **前端改动必过 `tsc`**：改完前端后必须 `pnpm run build`（含 `tsc -b`）过关再交付。

新增一个查询节点的典型步骤（RAG 侧）：

```python
# app/rag/pipelines/query_pipeline/nodes/node_xxx.py
from app.core.logger import logger, node_log, step_log
from app.rag.conf.query_pipeline_config import query_pipeline_config

SOME_LIMIT = query_pipeline_config.chunk_search_limit   # 调优参数一律来自配置

@step_log("step_1_do_something")
def step_1_do_something(state):
    ...

@node_log("node_xxx")
def node_xxx(state):
    return {"your_field": step_1_do_something(state)}
```

然后在 `query_pipeline/graph.py` 中 `add_node` 并接入边。

---

## 八、测试

> 当前仓库**尚未提供**统一的 pytest 测试套件（详见[路线图](#十路线图)）。  
> 现阶段的验证方式为：各节点 / 模块文件内置 `__main__` 自测入口 + 导入全链路跑通脚本。

```bash
# 导入全链路跑通（使用 doc/ 下的样例 PDF，输出到 output/）
python -m app.rag.pipelines.import_pipeline.graph

# 单节点 / 单工具自测（示例）
python -m app.tools.local_rag_tool          # 直接 local_rag_search.invoke(...)
python -m app.rag.pipelines.query_pipeline.nodes.node_rrf
python -m app.rag.pipelines.query_pipeline.nodes.node_search_embedding
```

运行前请确认：Milvus / MongoDB / MinIO / MySQL 已启动，`.env` 配置完整，本地模型已下载。日志输出到控制台与 `logs/app_YYYYMMDD.log`。

---

## 九、常见问题 FAQ

**Q1：启动时报 Milvus 连接失败？**  
检查 `MILVUS_URL` 是否带了 `http://` 前缀（pymilvus 3.x 会把纯 `host:port` 判为非法），并确认 `docker compose ps` 中 Milvus 已 healthy（首次启动需 30~90 秒）。

**Q2：导入时 `node_md_img` 报 `NoSuchBucket`？**  
`knowledge-base-files` 桶由 `minio_client_manager.init()` 自动创建。若被手动删除，重启服务即可重建；也可登录 `http://127.0.0.1:9001` 手动创建。

**Q3：显存不足（CUDA OOM）？**  
将 `.env` 中 `BGE_DEVICE`、`BGE_RERANKER_DEVICE` 改为 `cpu`；若仍不足，可关闭 `WARMUP_ENABLE`（默认已关闭，模型为懒加载）。

**Q4：图片处理阶段很慢？**  
`node_md_img` 会对每张图片调用 VLM，且内置 60 秒 9 次的滑动窗口限速，是为避免触发平台限流的刻意设计，批量导入大手册时请预留时间。

**Q5：问数子智能体连错库 / 查不到表？**  
后端 `db_tools` 读 `.env` 的 `MYSQL_DATABASE`（默认 `deepsearch_db`），而 `docker/mysql/mysql.sql` 初始化的是 compose 里的库名（默认 `axw0505`）。请统一两者（推荐都改 `deepsearch_db`），否则会连到空库。

**Q6：前端页面打不开 / 白屏？**  
前端是独立的 React 工程，开发态在 `frontend/` 跑 `pnpm run dev`（默认 5173 端口）再访问 `http://localhost:5173`；生产态需 `pnpm run build` 后自行托管 `dist/`。确认 `vite.config.ts` 的代理 `/api`、`/ws` 指向 `http://localhost:8000`。

**Q7：`pnpm install` 报错 ECONNRESET？**  
本机若存在 `HTTP_PROXY` / `HTTPS_PROXY` 代理变量会干扰安装，用 `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy pnpm install` 临时清空后重试。

**Q8：任务进度在哪里看？**  
导入进度调 `GET /api/kb/task/{task_id}`（返回 `status` 与 `done_list` / `running_list`）；主研搜任务的实时轨迹走 `WebSocket /ws/{thread_id}`，前端事件流直接渲染。

**Q9：任务状态在重启后丢失？**  
当前任务追踪是单进程内存态实现。多实例部署或需要持久化时，应将其替换为 Redis 等外部存储（见[路线图](#十路线图)）。

---

## 十、路线图

> 目标：在不改动既有检索链路与多智能体编排的前提下，逐步补齐工程规范化能力。每项落地前建立 `_backup_YYYYMMDD_HHMM/` 快照、改后跑通「导入 + 查询 + 研搜」验证，并保持「默认值 = 现值」确保零行为漂移。

### 10.1 已完成

- [x] **RAG 四目录归并**：`app/conf`、`app/clients`、`app/pipelines`、`app/repositories` 整体迁入 `app/rag/`（conf / clients / repositories / pipelines），实现「实现层隔离」，主流程目录（agent / api / tools / utils / core / prompts）保持原地。
- [x] **双 LLM 配置链并存收敛**：主流程 `app/agent/llm.py`（`LLM_QWEN_MAX` + `init_chat_model`）与 RAG `app/rag/conf/lm_config.py` + `clients/llm_client` 各司其职，互不知晓但职责清晰。
- [x] **提示词资产统一**：全部 Prompt 外置到 `app/prompts/templates/*.prompt` + `agents.yml`，由 `loader.py` / `agent_loader.py` 加载，并做占位符完备性校验（把裸 `KeyError → 500` 变成结构性防御）。
- [x] **运行时目录收敛到仓库根**：`output/` / `updated/` / `logs/` 统一由 `PROJECT_ROOT` 推导，废弃 `app/` 下的 output。
- [x] **知识库导入接口独立化**：新增 `app/api/kb_routes.py`（`/api/kb/import`、`/api/kb/task/{id}`、`/api/kb/tasks`），与主对话解耦。
- [x] **领域异常体系与日志规范化**：`app/core/exceptions.py` + `app/core/logger.py` 统一异常与日志出口。

### 10.2 规划中 / 待评估

- [ ] 统一 pytest 测试套件（当前为节点 `__main__` 自测 + 导入全链路脚本）。
- [ ] 任务追踪持久化（当前为单进程内存态，多实例部署需替换为 Redis 等外部存储，见 Q9）。
- [ ] 生产态前端托管：后端 `server.py` 挂载 `frontend/dist/` 静态资源（当前前端独立运行）。
- [ ] 检索性能优化（异步批处理、结果缓存、向量索引调优）。
- [ ] 用户权限与多租户。
