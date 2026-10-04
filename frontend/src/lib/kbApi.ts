/**
 * 知识库导入 / 文档管理接口客户端
 *
 * 导入：对应后端 `app/api/kb_routes.py`；文档管理：对应 `app/api/kb_doc_routes.py`。
 * 与对话链路共用同一个服务与同一个 WebSocket 通道（进度既有轮询也有
 * `kb_progress` 实时事件），因此这里只暴露 HTTP 方法，不引入独立通道。
 */
import { requestJson } from "./api";
import { API_BASE_URL } from "./config";
import type {
  KbDocChunksResponse,
  KbDocContentResponse,
  KbDocListResponse,
  KbDocSaveResult,
  KbDocSyncResult,
  KbImportResponse,
  KbReindexResult,
  KbTaskListResponse,
  KbTaskStatus
} from "../types";

/** 上传文件并触发导入任务（后端立即返回 task_id，解析在后台进行） */
export async function importKbFiles(
  files: File[],
  threadId: string
): Promise<KbImportResponse> {
  const formData = new FormData();
  formData.append("thread_id", threadId);
  files.forEach((file) => formData.append("files", file));

  return requestJson<KbImportResponse>(`${API_BASE_URL}/api/kb/import`, {
    method: "POST",
    body: formData
  });
}

/** 查询单个导入任务的进度（轮询入口） */
export async function getKbTask(taskId: string): Promise<KbTaskStatus> {
  return requestJson<KbTaskStatus>(
    `${API_BASE_URL}/api/kb/task/${encodeURIComponent(taskId)}`
  );
}

/** 列出全部导入任务（按创建时间倒序） */
export async function listKbTasks(): Promise<KbTaskListResponse> {
  return requestJson<KbTaskListResponse>(`${API_BASE_URL}/api/kb/tasks`);
}

/** 取消一个正在进行的导入任务（后端置位协作式取消标志，最多等当前节点跑完） */
export async function cancelKbTask(
  taskId: string
): Promise<{ status: string; task_id: string; message?: string }> {
  return requestJson<{ status: string; task_id: string; message?: string }>(
    `${API_BASE_URL}/api/kb/task/${encodeURIComponent(taskId)}/cancel`,
    { method: "POST" }
  );
}

/* ========================= 文档管理（/api/kb/docs*）========================= */

/** 列出知识库文档（Mongo 登记为准，非内存态任务表） */
export async function listKbDocs(
  status?: string
): Promise<KbDocListResponse> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return requestJson<KbDocListResponse>(`${API_BASE_URL}/api/kb/docs${query}`);
}

/** 读取文档正文（含「已编辑未重建」信号） */
export async function getKbDocContent(
  docId: string
): Promise<KbDocContentResponse> {
  return requestJson<KbDocContentResponse>(
    `${API_BASE_URL}/api/kb/docs/${encodeURIComponent(docId)}/content`
  );
}

/** 只读浏览文档切片 */
export async function getKbDocChunks(
  docId: string,
  limit = 200
): Promise<KbDocChunksResponse> {
  return requestJson<KbDocChunksResponse>(
    `${API_BASE_URL}/api/kb/docs/${encodeURIComponent(docId)}/chunks?limit=${limit}`
  );
}

/** 保存正文（后端做版本备份 + hash 更新）；reindex=true 时保存后串联一次重建 */
export async function saveKbDocContent(
  docId: string,
  content: string,
  reindex = false
): Promise<KbDocSaveResult> {
  return requestJson<KbDocSaveResult>(
    `${API_BASE_URL}/api/kb/docs/${encodeURIComponent(docId)}/content`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, reindex })
    }
  );
}

/** 触发重建：先按 item_name 锚点清三存储 → 走既有导入链路 */
export async function reindexKbDoc(
  docId: string,
  scope: "full" | "chunks_only" = "full"
): Promise<KbReindexResult> {
  return requestJson<KbReindexResult>(
    `${API_BASE_URL}/api/kb/docs/${encodeURIComponent(docId)}/reindex?scope=${scope}`,
    { method: "POST" }
  );
}

/** 停用：清三存储 + inactive（检索不再命中，保留 MD 与登记） */
export async function disableKbDoc(
  docId: string
): Promise<{ status: string; doc: Record<string, unknown> }> {
  return requestJson<{ status: string; doc: Record<string, unknown> }>(
    `${API_BASE_URL}/api/kb/docs/${encodeURIComponent(docId)}/disable`,
    { method: "POST" }
  );
}

/** 启用 = 一次重建（确保三存储与 MD 一致） */
export async function enableKbDoc(docId: string): Promise<KbReindexResult> {
  return requestJson<KbReindexResult>(
    `${API_BASE_URL}/api/kb/docs/${encodeURIComponent(docId)}/enable`,
    { method: "POST" }
  );
}

/** 软删：清三存储 + deleted（保留 MD 与登记，可逆） */
export async function deleteKbDoc(
  docId: string
): Promise<{ status: string; doc: Record<string, unknown> }> {
  return requestJson<{ status: string; doc: Record<string, unknown> }>(
    `${API_BASE_URL}/api/kb/docs/${encodeURIComponent(docId)}`,
    { method: "DELETE" }
  );
}

/** 存量迁移：扫 output/kb/ 登记现有 MD（向量库无命中者标 inactive） */
export async function syncKbDocs(): Promise<KbDocSyncResult> {
  return requestJson<KbDocSyncResult>(`${API_BASE_URL}/api/kb/docs/sync`, {
    method: "POST"
  });
}
