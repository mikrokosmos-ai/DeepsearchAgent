/**
 * 知识库导入接口客户端
 *
 * 对应后端 `app/api/kb_routes.py` 的三个接口。与对话链路共用同一个服务与
 * 同一个 WebSocket 通道（进度既有轮询也有 `kb_progress` 实时事件），
 * 因此这里只需要三个 HTTP 方法，不引入独立通道。
 */
import { requestJson } from "./api";
import { API_BASE_URL } from "./config";
import type { KbImportResponse, KbTaskListResponse, KbTaskStatus } from "../types";

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
