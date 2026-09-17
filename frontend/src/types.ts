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

/* ========================= 知识库导入（/api/kb/*）========================= */

/** 后端受理导入后返回的单条任务摘要 */
export interface KbImportTaskBrief {
  task_id: string;
  file_name: string;
  file_size: number;
  /** 受理时后端已登记的完成节点（中文名），前端据此立即点亮进度轨首格 */
  done_list?: string[];
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

/** 前端展示用的任务模型：在后端字段之外补充本地进度与错误态 */
export type KbTaskPhase = "uploading" | "processing" | "completed" | "failed";

export interface KbImportTask extends KbTaskStatus {
  phase: KbTaskPhase;
  /** 0-100，由 done_list 经 IMPORT_STEPS 权重换算得到 */
  progress: number;
}
