import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelTask,
  deleteSessionFile,
  fetchSessionHistory,
  listSessionFiles,
  startTask,
  uploadSessionFiles
} from "../lib/api";
import { WS_BASE_URL } from "../lib/config";
import { createThreadId, getStoredThreadId, storeThreadId } from "../lib/thread";
import type {
  ConnectionState,
  MonitorMessage,
  OutputFile,
  RagFinalPayload,
  SocketMessage,
  SubAgentNotice,
  UploadedItem
} from "../types";

const MAX_EVENTS = 120;
const MAX_NOTICES = 12;
/** 单轮最多展示的知识库配图数：一次检索可能命中很多图，超量会把轮次撑爆 */
export const MAX_TURN_IMAGES = 12;


function normalizeUrl(url: string): string {
  return url.replace(/\s+/g, "").toLowerCase();
}

/** 从 `rag_final` 事件负载里取出本轮图片地址（非字符串项一律丢弃） */
export function readImageUrlsFromEvent(
  data: RagFinalPayload | Record<string, unknown>
): string[] {
  const raw = (data as RagFinalPayload).image_urls;
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw
    .filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    .map((item) => item.trim());
}

/** 合并本轮图片：按「忽略空白与大小写」去重，并截断到上限 */
export function mergeImageUrls(
  existing: string[],
  incoming: string[],
  cap: number = MAX_TURN_IMAGES
): string[] {
  const seen = new Set(existing.map(normalizeUrl));
  const merged = [...existing];
  for (const url of incoming) {
    const key = normalizeUrl(url);
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    merged.push(url);
    if (merged.length >= cap) {
      break;
    }
  }
  return merged.slice(0, cap);
}

function extractString(data: Record<string, unknown>, key: string): string | null {
  const value = data[key];
  return typeof value === "string" ? value : null;
}

/** ：把 subagent_report 事件的 data 归一成强类型提示（缺字段则忽略该条） */
function toSubAgentNotice(data: Record<string, unknown>): SubAgentNotice | null {
  const subagent = extractString(data, "subagent");
  if (!subagent) {
    return null;
  }

  const sources = data.sources;

  return {
    subagent,
    parsed: data.parsed === true,
    truncated_by_limit: data.truncated_by_limit === true,
    error: extractString(data, "error"),
    sources: typeof sources === "number" ? sources : 0
  };
}

export function useDeepAgentSession() {
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | undefined>(undefined);
  const heartbeatTimerRef = useRef<number | undefined>(undefined);
  const uploadedNameSetRef = useRef<Set<string>>(new Set());
  const [threadId, setThreadId] = useState(getStoredThreadId);
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [events, setEvents] = useState<MonitorMessage[]>([]);
  const [files, setFiles] = useState<OutputFile[]>([]);
  const [sessionPath, setSessionPath] = useState("");
  const [result, setResult] = useState("");
  const [notices, setNotices] = useState<SubAgentNotice[]>([]);
  const [lastError, setLastError] = useState("");
  const [lastPongAt, setLastPongAt] = useState("");
  const [isRunning, setIsRunning] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadedItems, setUploadedItems] = useState<UploadedItem[]>([]);
  /** 本轮知识库命中配图：跨多次检索累加去重，下一次提问时清空 */
  const [imageUrls, setImageUrls] = useState<string[]>([]);
  // 供回调读取最新清单：回调不依赖 uploadedItems，避免每次上传都重建
  const uploadedItemsRef = useRef<UploadedItem[]>([]);
  uploadedItemsRef.current = uploadedItems;

  const clearSocketTimers = useCallback(() => {
    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = undefined;
    }
    if (heartbeatTimerRef.current) {
      window.clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = undefined;
    }
  }, []);

  /** 清空会话附件：新建会话与任务终态共用，避免上一轮附件挂在本轮下面 */
  const clearUploadedAttachments = useCallback(() => {
    setUploadedItems([]);
    uploadedNameSetRef.current.clear();
  }, []);

  /** 移除一个已上传附件：先删磁盘（D1），成功后才收敛本地清单；失败时清单保持不变 */
  const removeUploadedItem = useCallback(
    async (uid: string) => {
      const target = uploadedItemsRef.current.find((item) => item.uid === uid);
      if (!target) {
        return;
      }
      await deleteSessionFile(threadId, target.name);
      uploadedNameSetRef.current.delete(target.name);
      setUploadedItems((previous) => previous.filter((item) => item.uid !== uid));
    },
    [threadId]
  );

  const resetSession = useCallback(() => {
    const nextThreadId = createThreadId();
    storeThreadId(nextThreadId);
    setThreadId(nextThreadId);
    setEvents([]);
    setFiles([]);
    setSessionPath("");
    setResult("");
    setNotices([]);
    setLastError("");
    setImageUrls([]);
    clearUploadedAttachments();
    setIsRunning(false);
    setIsCancelling(false);
  }, [clearUploadedAttachments]);

  const refreshFiles = useCallback(async () => {
    if (!sessionPath) {
      return;
    }

    const response = await listSessionFiles(sessionPath);
    if (response.error) {
      throw new Error(response.error);
    }
    setFiles(response.files || []);
  }, [sessionPath]);

  useEffect(() => {
    let disposed = false;

    function connect() {
      clearSocketTimers();
      const hadSocket = Boolean(socketRef.current);
      socketRef.current?.close();
      setConnectionState(hadSocket ? "reconnecting" : "connecting");

      const socket = new WebSocket(`${WS_BASE_URL}/ws/${encodeURIComponent(threadId)}`);
      socketRef.current = socket;

      socket.onopen = () => {
        if (disposed) {
          return;
        }
        setConnectionState("connected");
        setLastError("");
        heartbeatTimerRef.current = window.setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) {
            socket.send("ping");
          }
        }, 25000);

        // P1-6：最终答案此前只走 WebSocket 推送，刷新/断线后就永久看不到。
        // 连上后从后端回读最近一次助手回复补进结果区；只在自己还没有结果时补，
        // 避免覆盖正在流式推送的新结果。回读失败不影响实时链路。
        fetchSessionHistory(threadId)
          .then((history) => {
            if (disposed) {
              return;
            }
            const lastAssistant = [...history.messages]
              .reverse()
              .find((message) => message.role === "assistant" && message.text);
            if (lastAssistant) {
              setResult((previous) => previous || lastAssistant.text);
            }
          })
          .catch(() => {
            /* 历史回读失败属可接受降级，不打扰用户 */
          });
      };

      socket.onmessage = (event) => {
        if (socketRef.current !== socket) {
          return;
        }
        try {
          const payload = JSON.parse(event.data) as SocketMessage;
          if (payload.type === "pong") {
            setLastPongAt(new Date().toISOString());
            return;
          }

          if (payload.type !== "monitor_event") {
            return;
          }

          setEvents((previous) => [...previous, payload].slice(-MAX_EVENTS));

          if (payload.event === "session_created") {
            const path = extractString(payload.data, "path");
            if (path) {
              setSessionPath(path);
            }
          }

          if (payload.event === "subagent_report") {
            const notice = toSubAgentNotice(payload.data);
            if (notice) {
              setNotices((previous) => [...previous, notice].slice(-MAX_NOTICES));
            }
          }

          // 检索链路完成事件带本轮命中配图；与最终答复是两条事件，需单独收集
          if (payload.event === "rag_final") {
            const urls = readImageUrlsFromEvent(payload.data);
            if (urls.length > 0) {
              setImageUrls((previous) => mergeImageUrls(previous, urls));
            }
          }

          if (payload.event === "task_result") {
            const finalResult = extractString(payload.data, "result");
            setResult(finalResult || payload.message);
            setIsRunning(false);
            setIsCancelling(false);
            clearUploadedAttachments();
          }

          if (payload.event === "task_cancelled") {
            setResult((previous) => previous || payload.message);
            setIsRunning(false);
            setIsCancelling(false);
            clearUploadedAttachments();
          }

          if (payload.event === "error") {
            setLastError(payload.message);
            setIsRunning(false);
            setIsCancelling(false);
            clearUploadedAttachments();
          }
        } catch (error) {
          setLastError(error instanceof Error ? error.message : "WebSocket 消息解析失败");
        }
      };

      socket.onerror = () => {
        if (!disposed && socketRef.current === socket) {
          setLastError("WebSocket 连接异常，请确认后端服务已启动");
        }
      };

      socket.onclose = () => {
        if (socketRef.current !== socket) {
          return;
        }
        clearSocketTimers();
        if (disposed) {
          setConnectionState("closed");
          return;
        }
        setConnectionState("reconnecting");
        reconnectTimerRef.current = window.setTimeout(connect, 2000);
      };
    }

    connect();

    return () => {
      disposed = true;
      clearSocketTimers();
      socketRef.current?.close();
    };
  }, [clearSocketTimers, clearUploadedAttachments, threadId]);

  useEffect(() => {
    if (!sessionPath) {
      return;
    }

    refreshFiles().catch((error: unknown) => {
      setLastError(error instanceof Error ? error.message : "文件列表刷新失败");
    });

    const timer = window.setInterval(() => {
      refreshFiles().catch((error: unknown) => {
        setLastError(error instanceof Error ? error.message : "文件列表刷新失败");
      });
    }, isRunning ? 2500 : 6000);

    return () => window.clearInterval(timer);
  }, [isRunning, refreshFiles, sessionPath]);

  const submitTask = useCallback(
    async (query: string) => {
      const cleanQuery = query.trim();
      if (!cleanQuery) {
        throw new Error("请输入研搜任务");
      }

      setIsRunning(true);
      setIsCancelling(false);
      setEvents([]);
      setResult("");
      setNotices([]);
      setLastError("");
      setImageUrls([]);
      try {
        const response = await startTask(cleanQuery, threadId);
        if (response.thread_id && response.thread_id !== threadId) {
          storeThreadId(response.thread_id);
          setThreadId(response.thread_id);
        }
        return response;
      } catch (error) {
        setIsRunning(false);
        setIsCancelling(false);
        throw error;
      }
    },
    [threadId]
  );

  const cancelCurrentTask = useCallback(async () => {
    if (!isRunning) {
      throw new Error("当前没有正在执行的任务");
    }

    setIsCancelling(true);
    setLastError("");
    try {
      const response = await cancelTask(threadId);
      if (response.status === "cancelled") {
        setIsRunning(false);
        setIsCancelling(false);
        setResult((previous) => previous || "任务已取消");
      }
      return response;
    } catch (error) {
      setIsCancelling(false);
      throw error;
    }
  }, [isRunning, threadId]);

  const uploadFiles = useCallback(
    async (items: UploadedItem[]) => {
      if (items.length === 0) {
        throw new Error("请选择要上传的文件");
      }

      const nextItems = items.filter((item) => !uploadedNameSetRef.current.has(item.name));

      if (nextItems.length === 0) {
        return {
          status: "uploaded",
          files: Array.from(uploadedNameSetRef.current)
        };
      }

      setIsUploading(true);
      setLastError("");
      try {
        const response = await uploadSessionFiles(
          nextItems.map((item) => item.raw),
          threadId
        );
        setUploadedItems((previous) => {
          const names = new Set(previous.map((item) => item.name));
          const next = [...previous];
          nextItems.forEach((item) => {
            if (!names.has(item.name)) {
              names.add(item.name);
              uploadedNameSetRef.current.add(item.name);
              next.push(item);
            }
          });
          return next;
        });
        return response;
      } finally {
        setIsUploading(false);
      }
    },
    [threadId]
  );

  const stats = useMemo(() => {
    const toolEvents = events.filter((event) => event.event === "tool_start").length;
    const assistantEvents = events.filter((event) => event.event === "assistant_call").length;
    const errorEvents = events.filter((event) => event.event === "error").length;

    return {
      toolEvents,
      assistantEvents,
      errorEvents,
      fileCount: files.length
    };
  }, [events, files.length]);

  return {
    connectionState,
    events,
    files,
    imageUrls,
    isCancelling,
    isRunning,
    isUploading,
    lastError,
    lastPongAt,
    notices,
    refreshFiles,
    resetSession,
    result,
    sessionPath,
    stats,
    cancelCurrentTask,
    removeUploadedItem,
    submitTask,
    threadId,
    uploadFiles,
    uploadedItems
  };
}
