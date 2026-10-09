import { API_BASE_URL } from "./config";
import type {
  CancelTaskResponse,
  DeleteUploadResponse,
  FileListResponse,
  SessionDeleteResponse,
  SessionHistoryResponse,
  SessionListResponse,
  TaskResponse,
  UploadResponse
} from "../types";

export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

export async function requestJson<T>(input: RequestInfo | URL, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const message =
      typeof payload === "object" && payload && "detail" in payload
        ? String(payload.detail)
        : `HTTP ${response.status}`;
    throw new Error(message);
  }

  return payload as T;
}

/** 启动一次研搜任务；userId 为可选身份，缺省时后端按会话级处理记忆 */
export async function startTask(
  query: string,
  threadId: string,
  userId?: string
): Promise<TaskResponse> {
  return requestJson<TaskResponse>(apiUrl("/api/task"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      query,
      thread_id: threadId,
      user_id: userId
    })
  });
}

export async function cancelTask(threadId: string): Promise<CancelTaskResponse> {
  return requestJson<CancelTaskResponse>(apiUrl(`/api/task/${encodeURIComponent(threadId)}/cancel`), {
    method: "POST"
  });
}

export async function uploadSessionFiles(
  files: File[],
  threadId: string
): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("thread_id", threadId);
  files.forEach((file) => formData.append("files", file));

  return requestJson<UploadResponse>(apiUrl("/api/upload"), {
    method: "POST",
    body: formData
  });
}


export async function deleteSessionFile(
  threadId: string,
  name: string
): Promise<DeleteUploadResponse> {
  const url = new URL(apiUrl("/api/upload"));
  url.searchParams.set("thread_id", threadId);
  url.searchParams.set("name", name);
  return requestJson<DeleteUploadResponse>(url, { method: "DELETE" });
}

export async function listSessionFiles(path: string): Promise<FileListResponse> {
  const url = new URL(apiUrl("/api/files"));
  url.searchParams.set("path", path);
  return requestJson<FileListResponse>(url);
}

export function getDownloadUrl(path: string): string {
  const url = new URL(apiUrl("/api/download"));
  url.searchParams.set("path", path);
  return url.toString();
}

/** 读取某个会话的主智能体问答历史（P1-6：断线 / 刷新后回读最终结果） */
export async function fetchSessionHistory(threadId: string): Promise<SessionHistoryResponse> {
  return requestJson<SessionHistoryResponse>(
    apiUrl(`/api/history/${encodeURIComponent(threadId)}`)
  );
}

/** 列出会话索引（后端按更新时间倒序返回），侧栏据此渲染「一个会话一条」 */
export async function listSessions(userId?: string, limit = 100): Promise<SessionListResponse> {
  const url = new URL(apiUrl("/api/sessions"));
  if (userId) {
    url.searchParams.set("user_id", userId);
  }
  url.searchParams.set("limit", String(limit));
  return requestJson<SessionListResponse>(url);
}

/** 按会话删除：后端一次清掉消息 / 图状态 / 上传附件 / 输出产物四类数据 */
export async function deleteSession(
  threadId: string,
  userId?: string
): Promise<SessionDeleteResponse> {
  const url = new URL(apiUrl(`/api/sessions/${encodeURIComponent(threadId)}`));
  if (userId) {
    url.searchParams.set("user_id", userId);
  }
  return requestJson<SessionDeleteResponse>(url, { method: "DELETE" });
}
