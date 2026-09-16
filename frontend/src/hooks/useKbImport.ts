/**
 * 知识库导入任务状态管理
 *
 * 职责：上传文件 → 跟踪任务进度 → 供「知识导入」页展示。
 * 对应参考实现中的 useImportTasks（上传）+ useImportPoller（轮询），
 * 本项目按「一个 hook 管一块」的既有风格合并到一处。
 *
 * 进度来源有两条，互为补充：
 *   1. **轮询** `GET /api/kb/task/{id}`（本 hook 负责，2s 一次）；
 *   2. **WebSocket** 的 `kb_progress` 事件（由 useDeepAgentSession 统一接收）。
 *      实时性更好，但轮询是无条件兜底，二者不冲突。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { getKbTask, importKbFiles, listKbTasks } from "../lib/kbApi";
import { importProgress } from "../lib/nodes";
import type { KbImportTask, KbTaskStatus } from "../types";

/** 轮询间隔：与参考实现保持一致 */
const POLL_INTERVAL_MS = 2000;
/** 单文件上限，与后端 `_MAX_FILE_BYTES` 保持一致 */
const MAX_FILE_BYTES = 100 * 1024 * 1024;
/** 允许的文件类型，与后端 `_ALLOWED_SUFFIXES` 保持一致 */
const ALLOWED_SUFFIXES = [".pdf", ".md", ".markdown"];

/** 前端任务模型：在后端字段之上补充本地行标识 */
export interface KbImportTaskItem extends KbImportTask {
  /** 本地行标识（上传阶段还没有 task_id，用它做列表 key） */
  fileId: string;
}

export interface UploadOutcome {
  accepted: number;
  rejected: Array<{ name: string; reason: string }>;
}

function makeFileId(): string {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function emptyStatus(threadId: string): KbTaskStatus {
  return {
    task_id: "",
    status: "processing",
    done_list: [],
    running_list: [],
    file_name: "",
    file_size: 0,
    thread_id: threadId,
    created_at: Date.now() / 1000,
    error: ""
  };
}

function resolvePhase(status: string): KbImportTask["phase"] {
  if (status === "completed") {
    return "completed";
  }
  if (status === "failed") {
    return "failed";
  }
  return "processing";
}

function toTaskItem(status: KbTaskStatus, fileId: string): KbImportTaskItem {
  const phase = resolvePhase(status.status);
  return {
    ...status,
    fileId,
    phase,
    // 完成直接给 100；进行中/失败都按已完成节点换算（失败时保留已跑到的进度）
    progress: phase === "completed" ? 100 : importProgress(status.done_list)
  };
}

export function useKbImport(threadId: string) {
  const [tasks, setTasks] = useState<KbImportTaskItem[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const [lastError, setLastError] = useState("");
  const tasksRef = useRef<KbImportTaskItem[]>(tasks);
  tasksRef.current = tasks;

  const patchTask = useCallback((fileId: string, patch: Partial<KbImportTaskItem>) => {
    setTasks((previous) =>
      previous.map((task) => (task.fileId === fileId ? { ...task, ...patch } : task))
    );
  }, []);

  /** 首次挂载：拉取后端已有任务（刷新页面后仍能看到进度） */
  const refreshFromServer = useCallback(async () => {
    try {
      const response = await listKbTasks();
      const items = (response.tasks || []).map((status) => toTaskItem(status, status.task_id));
      setTasks(items);
    } catch (error) {
      setLastError(error instanceof Error ? error.message : "获取导入任务列表失败");
    }
  }, []);

  useEffect(() => {
    refreshFromServer().catch(() => undefined);
  }, [refreshFromServer]);

  /** 轮询：只要还有进行中的任务就持续查询 */
  const processingIds = tasks
    .filter((task) => task.phase === "processing" && task.task_id)
    .map((task) => task.task_id)
    .sort()
    .join(",");

  useEffect(() => {
    if (!processingIds) {
      return;
    }
    let disposed = false;

    const tick = async () => {
      const ids = processingIds.split(",").filter(Boolean);
      for (const id of ids) {
        try {
          const status = await getKbTask(id);
          if (disposed) {
            return;
          }
          const existing = tasksRef.current.find((task) => task.task_id === id);
          patchTask(existing ? existing.fileId : id, toTaskItem(status, existing ? existing.fileId : id));
        } catch {
          // 单次轮询失败忽略，等下一轮
        }
      }
    };

    const timer = window.setInterval(() => {
      tick().catch(() => undefined);
    }, POLL_INTERVAL_MS);

    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [patchTask, processingIds]);

  /** 上传文件并创建导入任务 */
  const uploadFiles = useCallback(
    async (files: File[]): Promise<UploadOutcome> => {
      const rejected: UploadOutcome["rejected"] = [];
      const accepted: File[] = [];

      for (const file of files) {
        const lower = file.name.toLowerCase();
        if (!ALLOWED_SUFFIXES.some((suffix) => lower.endsWith(suffix))) {
          rejected.push({ name: file.name, reason: "仅支持 PDF / Markdown" });
          continue;
        }
        if (file.size > MAX_FILE_BYTES) {
          rejected.push({ name: file.name, reason: "超过 100MB 上限" });
          continue;
        }
        accepted.push(file);
      }

      if (accepted.length === 0) {
        return { accepted: 0, rejected };
      }

      // 先插入本地占位卡（phase=uploading），让用户立刻看到反馈
      const placeholders: KbImportTaskItem[] = accepted.map((file) => ({
        ...emptyStatus(threadId),
        fileId: makeFileId(),
        file_name: file.name,
        file_size: file.size,
        phase: "uploading",
        progress: 0
      }));
      setTasks((previous) => [...placeholders, ...previous]);
      setIsUploading(true);
      setLastError("");

      try {
        const response = await importKbFiles(accepted, threadId);
        // 后端按上传顺序返回任务，与 placeholders 一一对应
        response.tasks.forEach((brief, index) => {
          const placeholder = placeholders[index];
          if (!placeholder) {
            return;
          }
          patchTask(placeholder.fileId, {
            task_id: brief.task_id,
            file_name: brief.file_name,
            file_size: brief.file_size,
            phase: "processing",
            status: "processing",
            progress: importProgress(["开始上传文件"])
          });
        });
        return { accepted: accepted.length, rejected };
      } catch (error) {
        placeholders.forEach((placeholder) =>
          patchTask(placeholder.fileId, {
            phase: "failed",
            status: "failed",
            error: error instanceof Error ? error.message : "上传失败"
          })
        );
        const message = error instanceof Error ? error.message : "上传失败";
        setLastError(message);
        throw error;
      } finally {
        setIsUploading(false);
      }
    },
    [patchTask, threadId]
  );

  const removeTask = useCallback((fileId: string) => {
    setTasks((previous) => previous.filter((task) => task.fileId !== fileId));
  }, []);

  return {
    tasks,
    isUploading,
    lastError,
    uploadFiles,
    removeTask,
    refreshFromServer
  };
}
