export type ConnectionState = "connecting" | "connected" | "reconnecting" | "closed";

export type MonitorEventName =
  | "session_created"
  | "tool_start"
  | "assistant_call"
  | "task_result"
  | "task_cancelled"
  | "error"
  | string;

export interface MonitorMessage {
  type: "monitor_event";
  event: MonitorEventName;
  message: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface PongMessage {
  type: "pong";
  message: string;
}

export type SocketMessage = MonitorMessage | PongMessage;

export interface TaskResponse {
  status: "started" | string;
  thread_id: string;
}

export interface CancelTaskResponse {
  status: "cancelled" | "cancelling" | string;
  thread_id: string;
  message?: string;
}

export interface UploadResponse {
  status: "uploaded" | string;
  files: string[];
}


export interface DeleteUploadResponse {
  status: "deleted" | string;
  name: string;
  removed: boolean;
  message?: string;
}

export interface OutputFile {
  name: string;
  type: "file" | string;
  path: string;
  size: number;
  mtime: number;
}

export interface FileListResponse {
  files?: OutputFile[];
  error?: string;
}

export interface UploadedItem {
  uid: string;
  name: string;
  size: number;
  raw: File;
}

/** 主智能体的一条问答消息（P1-6：落库后可在断线/刷新后回读） */
export interface SessionHistoryMessage {
  role: "user" | "assistant" | string;
  text: string;
  ts: number;
}

/** GET /api/history/{thread_id} 的响应 */
export interface SessionHistoryResponse {
  thread_id: string;
  messages: SessionHistoryMessage[];
}

/** 子智能体返回契约的一次观察结果（§2：契约从"提示词约定"升级为"可观测事实"） */
export interface SubAgentNotice {
  /** 子智能体名，如「网络搜索助手」 */
  subagent: string;
  /** 是否解析到契约 JSON 块；false 表示已按正文内容降级理解 */
  parsed: boolean;
  /** 是否因次数/行数上限被截断（true 时结论可能不完整） */
  truncated_by_limit: boolean;
  /** 检索链路故障原文；无故障为 null */
  error: string | null;
  /** 来源标注条数 */
  sources: number;
}

/* ========================= 知识库导入（/api/kb/*）========================= */

/** 后端受理导入后返回的单条任务摘要 */
export interface KbImportTaskBrief {
  task_id: string;
  file_name: string;
  file_size: number;
  /** 受理时后端已登记的完成节点（中文名），前端据此立即点亮进度轨首格 */
  done_list?: string[];
  /** 产物目录（output/kb/{task_id}），受理时即已知，供前端直接浏览本次导入产物 */
  output_dir?: string;
}

export interface KbImportResponse {
  status: string;
  tasks: KbImportTaskBrief[];
}

/** 后端任务进度（与 task_utils 的登记字段一一对应） */
export interface KbTaskStatus {
  task_id: string;
  status: string;
  /** 已完成节点（中文名，与 lib/nodes.ts 的 IMPORT_STEPS.key 逐字对应） */
  done_list: string[];
  /** 正在运行节点（中文名） */
  running_list: string[];
  file_name: string;
  file_size: number;
  /** 产物目录（output/kb/{task_id}），供前端浏览本次导入的全部产物 */
  output_dir?: string;
  thread_id: string;
  created_at: number;
  error: string;
}

export interface KbTaskListResponse {
  tasks: KbTaskStatus[];
}

/** DELETE /api/kb/task/{id} 的响应：cancelled=true 表示移除前曾请求取消 */
export interface KbTaskDeleteResponse {
  status: "removed" | string;
  task_id: string;
  cancelled: boolean;
  removed: boolean;
}

/** 前端展示用的任务模型：在后端字段之外补充本地进度与错误态 */
export type KbTaskPhase = "uploading" | "processing" | "completed" | "failed";

export interface KbImportTask extends KbTaskStatus {
  phase: KbTaskPhase;
  /** 0-100，由 done_list 经 IMPORT_STEPS 权重换算得到 */
  progress: number;
}

/* ========================= 知识库文档管理（/api/kb/docs*）========================= */

/** 文档状态（与后端 kb_doc_repo 的状态常量逐字对应） */
export type KbDocStatus =
  | "active"
  | "inactive"
  | "processing"
  | "reindexing"
  | "failed"
  | "deleted";

/** 「飞行中」状态：此时禁止编辑 / 重建 / 启停 / 删除（后端每 doc 单飞） */
export const KB_DOC_BUSY_STATUSES: KbDocStatus[] = ["processing", "reindexing"];

/** 一条知识库文档登记（kb_document 集合） */
export interface KbDocument {
  doc_id: string;
  /** 三存储的删除锚点，与导入链路一致 */
  item_name: string;
  file_title: string;
  /** 只读展示；后端不接受前端传路径 */
  md_path: string;
  output_dir: string;
  status: KbDocStatus;
  chunk_count: number;
  /** 正文当前 hash */
  content_hash: string;
  /** 最后一次重建时写入的 hash；与 content_hash 不等 = 已编辑未重建 */
  indexed_hash: string;
  /** 由后端派生：content_hash != indexed_hash */
  edited_not_reindexed: boolean;
  edit_log: KbDocEditLogEntry[];
  origin_task_id: string;
  last_reindex_task_id: string;
  ts: number;
}

export interface KbDocEditLogEntry {
  action: string;
  ts: number;
  [key: string]: unknown;
}

/** GET /api/kb/docs */
export interface KbDocListResponse {
  total: number;
  items: KbDocument[];
}

/** GET /api/kb/docs/{id}/content */
export interface KbDocContentResponse {
  doc_id: string;
  content: string;
  content_hash: string;
  indexed_hash: string;
  edited_not_reindexed: boolean;
}

/** Milvus chunks 集合中的一条切片（只读） */
export interface KbChunk {
  chunk_id?: string;
  file_title?: string;
  title?: string;
  parent_title?: string;
  part?: string;
  content?: string;
}

/** GET /api/kb/docs/{id}/chunks */
export interface KbDocChunksResponse {
  doc_id: string;
  chunks: KbChunk[];
}

/** PUT /api/kb/docs/{id}/content 的保存结果 */
export interface KbDocSaveResult {
  status: "saved" | "saved_and_reindexing" | string;
  save?: { doc?: KbDocument; [key: string]: unknown };
  reindex?: KbReindexResult;
}

/** POST /api/kb/docs/{id}/reindex 的返回 */
export interface KbReindexResult {
  status: string;
  doc_id: string;
  task_id?: string;
  scope?: string;
  purge?: Record<string, unknown>;
}

/** POST /api/kb/docs/sync 的返回 */
export interface KbDocSyncItem {
  md_path: string;
  status: string;
  item_name?: string;
}

export interface KbDocSyncResult {
  scanned: number;
  registered: number;
  /** MD 在但向量库无命中 → 登记为 inactive（待重建） */
  inactive: number;
  skipped: number;
  items: KbDocSyncItem[];
}
