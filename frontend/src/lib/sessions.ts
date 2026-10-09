/**
 * 会话（页面）索引：侧栏的本地持久化清单
 *
 * 只存索引不存内容 —— 内容以「后端按会话回读」为准。索引的作用是让侧栏在刷新后
 * 立刻可渲染，并在后端暂时不可达时不至于一片空白。
 */
import type { SessionSummary } from "../types";

export interface SessionIndex {
  /** 会话标识，即 thread_id */
  id: string;
  /** 首条提问生成；空串表示这一页还没问过任何问题 */
  title: string;
  createdAt: number;
  updatedAt: number;
  messageCount: number;
}

const STORAGE_KEY = "deepsearch.sessions";
const TITLE_MAX_CHARS = 24;
const MAX_SESSIONS = 200;

/** 空标题的占位名：索引里的空串表示「这一页还没问过」，展示时才补默认名 */
export const NEW_SESSION_TITLE = "新会话";

export function buildSessionTitle(text: string): string {
  const clean = text.trim().replace(/\s+/g, " ");
  return clean.length > TITLE_MAX_CHARS ? `${clean.slice(0, TITLE_MAX_CHARS)}…` : clean;
}

export function createSessionEntry(id: string, now: number = Date.now()): SessionIndex {
  return { id, title: "", createdAt: now, updatedAt: now, messageCount: 0 };
}

function sortByUpdated(list: SessionIndex[]): SessionIndex[] {
  return [...list].sort((a, b) => b.updatedAt - a.updatedAt);
}

function isSessionIndex(value: unknown): value is SessionIndex {
  const item = value as SessionIndex | null;
  return Boolean(item && typeof item.id === "string" && item.id.length > 0);
}

export function loadSessionIndex(): SessionIndex[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return [];
    }
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return sortByUpdated(parsed.filter(isSessionIndex));
  } catch {
    // 本地索引坏掉不该拦住整个页面：按「没有历史会话」处理，后端回读会把它补回来
    return [];
  }
}

export function saveSessionIndex(list: SessionIndex[]): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(list.slice(0, MAX_SESSIONS)));
  } catch {
    // 存储被禁用 / 写满时忽略：索引丢失只影响侧栏渲染，会话本身在后端
  }
}

/** 记一次提问：首问定标题，之后只刷新时间与条数 */
export function touchSession(
  list: SessionIndex[],
  id: string,
  question: string,
  now: number = Date.now()
): SessionIndex[] {
  const existing = list.find((item) => item.id === id);
  const next: SessionIndex = existing
    ? {
        ...existing,
        title: existing.title || buildSessionTitle(question),
        updatedAt: now,
        messageCount: existing.messageCount + 1
      }
    : {
        ...createSessionEntry(id, now),
        title: buildSessionTitle(question),
        messageCount: 1
      };
  return sortByUpdated([...list.filter((item) => item.id !== id), next]);
}

export function removeSessionEntry(list: SessionIndex[], id: string): SessionIndex[] {
  return list.filter((item) => item.id !== id);
}

/**
 * 用后端索引校正本地索引
 *
 * 远端优先（它是权威：标题、条数、时间都由落库写入维护），本地多出来的行保留 ——
 * 那多半是后端暂时不可达时新建的会话，丢掉就等于把用户刚建的一页吃了。
 * 后端时间戳是 epoch 秒，本地是毫秒，这里统一到毫秒。
 */
export function mergeSessionIndex(
  local: SessionIndex[],
  remote: SessionSummary[]
): SessionIndex[] {
  const merged = new Map<string, SessionIndex>();
  local.forEach((item) => merged.set(item.id, item));

  remote.forEach((item) => {
    const existing = merged.get(item.session_id);
    const updatedAt = Math.round((item.updated_at || 0) * 1000) || existing?.updatedAt || 0;
    const createdAt = Math.round((item.created_at || 0) * 1000) || existing?.createdAt || updatedAt;
    merged.set(item.session_id, {
      id: item.session_id,
      title: buildSessionTitle(item.title) || existing?.title || "",
      createdAt,
      updatedAt,
      messageCount: item.message_count || existing?.messageCount || 0
    });
  });

  return sortByUpdated(Array.from(merged.values())).slice(0, MAX_SESSIONS);
}
