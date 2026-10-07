const STORAGE_KEY = "deepsearch.thread_id";
const USER_STORAGE_KEY = "deepsearch.user_id";

function randomId(fallbackPrefix: string): string {
  if (crypto.randomUUID) {
    return crypto.randomUUID();
  }

  return `${fallbackPrefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function createThreadId(): string {
  return randomId("manual");
}

export function getStoredThreadId(): string {
  const existing = window.localStorage.getItem(STORAGE_KEY);
  if (existing) {
    return existing;
  }

  const threadId = createThreadId();
  window.localStorage.setItem(STORAGE_KEY, threadId);
  return threadId;
}

export function storeThreadId(threadId: string): void {
  window.localStorage.setItem(STORAGE_KEY, threadId);
}

/** 生成稳定用户 ID：跨会话记忆按用户聚合，故与 thread_id 分开持久化（换会话不失忆） */
export function createUserId(): string {
  return randomId("user");
}

export function getStoredUserId(): string {
  const existing = window.localStorage.getItem(USER_STORAGE_KEY);
  if (existing) {
    return existing;
  }

  const userId = createUserId();
  window.localStorage.setItem(USER_STORAGE_KEY, userId);
  return userId;
}

export function storeUserId(userId: string): void {
  window.localStorage.setItem(USER_STORAGE_KEY, userId);
}
