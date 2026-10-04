/**
 * 知识库文档管理状态钩子
 *
 * 统一封装列表 / 详情 / 编辑 / 重建 / 启停 / 删除 / 存量迁移的调用与乐观刷新。
 * 变更类操作一律「先请求后端 → 成功后重新拉列表」，确保前端展示始终以
 * Mongo 登记为准（MD 是唯一事实源，前端不算状态）。
 */
import { useCallback, useEffect, useRef, useState } from "react";

import {
  deleteKbDoc,
  disableKbDoc,
  enableKbDoc,
  getKbDocChunks,
  getKbDocContent,
  listKbDocs,
  reindexKbDoc,
  saveKbDocContent,
  syncKbDocs
} from "../lib/kbApi";
import type { KbChunk, KbDocListResponse, KbDocument } from "../types";

export interface UseKbDocsResult {
  docs: KbDocument[];
  total: number;
  isLoading: boolean;
  lastError: string;
  refresh: () => Promise<void>;
  loadContent: (docId: string) => Promise<{ content: string; edited: boolean } | null>;
  loadChunks: (docId: string) => Promise<KbChunk[]>;
  saveContent: (docId: string, content: string, reindex: boolean) => Promise<void>;
  reindex: (docId: string) => Promise<void>;
  setEnabled: (docId: string, enabled: boolean) => Promise<void>;
  remove: (docId: string) => Promise<void>;
  sync: () => Promise<{ registered: number; inactive: number }>;
}

export function useKbDocs(): UseKbDocsResult {
  const [docs, setDocs] = useState<KbDocument[]>([]);
  const [total, setTotal] = useState(0);
  const [isLoading, setIsLoading] = useState(false);
  const [lastError, setLastError] = useState("");
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    try {
      const result: KbDocListResponse = await listKbDocs();
      if (!mountedRef.current) {
        return;
      }
      setDocs(result.items ?? []);
      setTotal(result.total ?? 0);
      setLastError("");
    } catch (error) {
      if (mountedRef.current) {
        setLastError(error instanceof Error ? error.message : "加载知识库文档失败");
      }
    } finally {
      if (mountedRef.current) {
        setIsLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    refresh().catch(() => undefined);
  }, [refresh]);

  const loadContent = useCallback(async (docId: string) => {
    const result = await getKbDocContent(docId);
    return { content: result.content, edited: result.edited_not_reindexed };
  }, []);

  const loadChunks = useCallback(async (docId: string) => {
    const result = await getKbDocChunks(docId);
    return result.chunks ?? [];
  }, []);

  const saveContent = useCallback(
    async (docId: string, content: string, reindex: boolean) => {
      await saveKbDocContent(docId, content, reindex);
      await refresh();
    },
    [refresh]
  );

  const reindex = useCallback(
    async (docId: string) => {
      await reindexKbDoc(docId, "full");
      await refresh();
    },
    [refresh]
  );

  const setEnabled = useCallback(
    async (docId: string, enabled: boolean) => {
      if (enabled) {
        await enableKbDoc(docId);
      } else {
        await disableKbDoc(docId);
      }
      await refresh();
    },
    [refresh]
  );

  const remove = useCallback(
    async (docId: string) => {
      await deleteKbDoc(docId);
      await refresh();
    },
    [refresh]
  );

  const sync = useCallback(async () => {
    const result = await syncKbDocs();
    await refresh();
    return { registered: result.registered ?? 0, inactive: result.inactive ?? 0 };
  }, [refresh]);

  return {
    docs,
    total,
    isLoading,
    lastError,
    refresh,
    loadContent,
    loadChunks,
    saveContent,
    reindex,
    setEnabled,
    remove,
    sync
  };
}
