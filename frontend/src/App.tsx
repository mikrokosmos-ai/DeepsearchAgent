import {
  ApiOutlined,
  AppstoreOutlined,
  ArrowDownOutlined,
  BranchesOutlined,
  CheckCircleOutlined,
  CloudServerOutlined,
  CloseCircleOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  MessageOutlined,
  PlusOutlined,
  SearchOutlined,
  ToolOutlined
} from "@ant-design/icons";
import { Alert, App as AntApp } from "antd";
import { useEffect, useMemo, useRef, useState } from "react";
import { AgentWelcome } from "./components/AgentWelcome";
import { ChatComposer } from "./components/ChatComposer";
import { ConversationThread } from "./components/ConversationThread";
import type { ChatTurn } from "./components/ConversationThread";
import { ImportPage } from "./components/importer/ImportPage";
import { KbManagerPage } from "./components/kbmanager/KbManagerPage";
import { SessionRail } from "./components/SessionRail";
import { API_BASE_URL, WS_BASE_URL } from "./lib/config";
import {
  deleteSession as deleteSessionApi,
  fetchSessionHistory,
  listSessions
} from "./lib/api";
import {
  createSessionEntry,
  loadSessionIndex,
  mergeSessionIndex,
  removeSessionEntry,
  saveSessionIndex,
  touchSession
} from "./lib/sessions";
import type { SessionIndex } from "./lib/sessions";
import { getStoredUserId } from "./lib/thread";
import { useDeepAgentSession } from "./hooks/useDeepAgentSession";
import type { ConnectionState, SessionHistoryMessage, UploadedItem } from "./types";

/** 顶层视图：对话研搜 / 知识导入 / 知识库管理 */
type AppView = "chat" | "import" | "kb";

const VIEWS: { key: AppView; label: string; icon: React.ReactNode }[] = [
  { key: "chat", label: "深度研搜", icon: <MessageOutlined /> },
  { key: "import", label: "知识导入", icon: <AppstoreOutlined /> },
  { key: "kb", label: "知识库管理", icon: <DatabaseOutlined /> }
];

/** 连接态 → 顶栏徽标态（Ragent 的 online / probing / offline 三态） */
function badgeStatus(state: ConnectionState): "online" | "probing" | "offline" {
  if (state === "connected") {
    return "online";
  }
  if (state === "connecting" || state === "reconnecting") {
    return "probing";
  }
  return "offline";
}

function connectionLabel(state: ConnectionState): string {
  const labels: Record<ConnectionState, string> = {
    connecting: "连接中",
    connected: "已连接",
    reconnecting: "重连中",
    closed: "已关闭"
  };
  return labels[state];
}

function createTurn(content: string): ChatTurn {
  return {
    id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}`,
    content,
    events: [],
    files: [],
    imageUrls: [],
    isRunning: true,
    notices: [],
    result: "",
    timestamp: new Date().toISOString()
  };
}

/** 轮次距底多少像素内仍算「正在看最新内容」；越小越容易被判为已上滚 */
const BOTTOM_EPS = 12;

function historyTurn(
  id: string,
  user: SessionHistoryMessage,
  assistant: SessionHistoryMessage | null
): ChatTurn {
  const at = (assistant?.ts || user.ts || 0) * 1000;
  return {
    id,
    content: user.text,
    events: [],
    files: [],
    imageUrls: assistant?.image_urls ?? [],
    isRunning: false,
    notices: [],
    result: assistant?.text ?? "",
    timestamp: new Date(at || Date.now()).toISOString()
  };
}

/**
 * 把后端回读的问答历史还原成轮次
 *
 * 只还原「问了什么 / 答了什么 / 配图」：过程事件不落库，所以还原出来的轮次没有轨迹。
 * 没有配对答复的提问也保留一条 —— 用户至少能看到自己问过什么，而不是凭空少一轮。
 */
function turnsFromHistory(messages: SessionHistoryMessage[]): ChatTurn[] {
  const turns: ChatTurn[] = [];
  let pending: SessionHistoryMessage | null = null;

  messages.forEach((item, index) => {
    if (item.role === "user") {
      pending = item;
      return;
    }
    if (item.role !== "assistant") {
      return;
    }
    turns.push(
      historyTurn(`h-${index}-${Math.round(item.ts * 1000)}`, pending ?? item, item)
    );
    pending = null;
  });

  const tail = pending as SessionHistoryMessage | null;
  if (tail) {
    turns.push(historyTurn(`h-tail-${Math.round(tail.ts * 1000)}`, tail, null));
  }
  return turns;
}

export default function App() {
  const { message, modal } = AntApp.useApp();
  const [view, setView] = useState<AppView>("chat");
  const [query, setQuery] = useState("");
  const [stagedItems, setStagedItems] = useState<UploadedItem[]>([]);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [sessions, setSessions] = useState<SessionIndex[]>([]);
  const [activeSessionId, setActiveSessionId] = useState("");
  const [sessionFilter, setSessionFilter] = useState("");
  const [menuSessionId, setMenuSessionId] = useState<string | null>(null);
  const streamRef = useRef<HTMLDivElement | null>(null);
  const wasRunningRef = useRef(false);
  // 是否跟随最新内容：由滚动位置派生，用户一旦离开底部立即置否
  const followRef = useRef(true);
  // 新一轮提交强制回到跟随态：提交时用户可能正停在历史位置上
  const forceFollowRef = useRef(false);
  // 浮标可见性单独用状态承载，只在布尔翻转时重渲染（滚动事件很密）
  const [atBottom, setAtBottom] = useState(true);
  // 历史回读出来的轮次不接实时流状态，否则上一轮的答案会被盖到它身上
  const restoredTurnIdsRef = useRef<Set<string>>(new Set());
  // 会话切换后要把「最近提问」那一轮滚进视口（等轮次渲染出来再滚）
  const pendingScrollRef = useRef(false);
  const sessionsRef = useRef<SessionIndex[]>([]);
  sessionsRef.current = sessions;
  const session = useDeepAgentSession();

  // 首屏：本地索引先立起来（刷新后侧栏立刻有内容），随后由后端索引校正
  useEffect(() => {
    const local = loadSessionIndex();
    const current = session.threadId;
    const base = local.some((item) => item.id === current)
      ? local
      : [createSessionEntry(current), ...local];
    setSessions(base);
    setActiveSessionId(current);
    saveSessionIndex(base);
    // 只跑首屏一次：之后 activeSessionId 由会话切换 / 新建驱动
  }, []);

  useEffect(() => {
    let disposed = false;
    listSessions(getStoredUserId())
      .then((response) => {
        if (disposed) {
          return;
        }
        setSessions((previous) => {
          const merged = mergeSessionIndex(previous, response.sessions);
          saveSessionIndex(merged);
          return merged;
        });
      })
      .catch(() => {
        // 后端不可达时保留本地索引：侧栏退化为本地视图，不打扰用户
      });
    return () => {
      disposed = true;
    };
  }, []);

  // 切会话 / 刷新后按会话回读内容（本地索引只有标题，正文以后端为准）
  useEffect(() => {
    if (!activeSessionId) {
      return;
    }
    let disposed = false;
    fetchSessionHistory(activeSessionId)
      .then((history) => {
        if (disposed) {
          return;
        }
        setTurns((previous) => {
          if (previous.length > 0) {
            return previous;
          }
          const restored = turnsFromHistory(history.messages);
          restored.forEach((turn) => restoredTurnIdsRef.current.add(turn.id));
          return restored;
        });
      })
      .catch(() => {
        // 回读失败保留本地已有内容：新会话本来就没有历史，属正常分支
      });
    return () => {
      disposed = true;
    };
  }, [activeSessionId]);

  // 任务进入终态（成功 / 已停止 / 失败）即清空暂存附件，避免跨轮残留
  useEffect(() => {
    if (wasRunningRef.current && !session.isRunning) {
      setStagedItems([]);
    }
    wasRunningRef.current = session.isRunning;
  }, [session.isRunning]);

  useEffect(() => {
    setTurns((previous) => {
      if (previous.length === 0) {
        return previous;
      }

      const latestTurn = previous[previous.length - 1];
      if (restoredTurnIdsRef.current.has(latestTurn.id)) {
        return previous;
      }
      const nextLatestTurn = {
        ...latestTurn,
        events: session.events,
        files: session.files,
        imageUrls: session.imageUrls,
        isRunning: session.isRunning,
        notices: session.notices,
        result: session.result
      };

      return [...previous.slice(0, -1), nextLatestTurn];
    });
  }, [
    session.events,
    session.files,
    session.imageUrls,
    session.isRunning,
    session.notices,
    session.result
  ]);

  useEffect(() => {
    const streamNode = streamRef.current;
    if (!streamNode) {
      return;
    }

    // 新一轮提交后强制回到跟随态：此刻用户可能正停在历史位置回看
    if (forceFollowRef.current) {
      forceFollowRef.current = false;
      followRef.current = true;
      setAtBottom(true);
    }

    // 用户已上滚离开底部：不再自动滚动，把屏幕还给回看的人
    if (!followRef.current) {
      return;
    }

    window.requestAnimationFrame(() => {
      streamNode.scrollTo({
        top: streamNode.scrollHeight,
        // 流式期间用即时定位：平滑动画会被高频事件反复打断，叠加成持续下拽
        behavior: session.isRunning ? "auto" : "smooth"
      });
    });
  }, [turns, session.isRunning]);

  // 会话切换后落到「最近提问」那一轮：等轮次渲染出来再滚
  useEffect(() => {
    if (!pendingScrollRef.current || turns.length === 0) {
      return;
    }
    pendingScrollRef.current = false;
    const latest = turns[turns.length - 1];
    window.requestAnimationFrame(() => {
      document
        .getElementById(`turn-${latest.id}`)
        ?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }, [turns]);

  /** 是否仍停在底部附近：容器底距 ≤ BOTTOM_EPS 才算「正在看最新内容」 */
  function handleStreamScroll() {
    const node = streamRef.current;
    if (!node) {
      return;
    }
    const near = node.scrollHeight - node.scrollTop - node.clientHeight <= BOTTOM_EPS;
    followRef.current = near;
    setAtBottom((previous) => (previous === near ? previous : near));
  }

  /** 回到底部：恢复跟随并立即定位（不做平滑动画，避免被后到的事件打断） */
  function handleJumpToBottom() {
    const streamNode = streamRef.current;
    followRef.current = true;
    setAtBottom(true);
    streamNode?.scrollTo({ top: streamNode.scrollHeight, behavior: "auto" });
  }

  async function handleSubmit() {
    const cleanQuery = query.trim();
    if (!cleanQuery) {
      message.warning("请输入研搜任务");
      return;
    }

    // 暂存附件在这里才真正上传；上传失败即中止本轮，不让任务在缺附件的情况下跑
    if (stagedItems.length > 0) {
      try {
        await session.uploadFiles(stagedItems);
        setStagedItems([]);
        message.success(`已附带 ${stagedItems.length} 个文件`);
      } catch (error) {
        message.error(error instanceof Error ? error.message : "附件上传失败，任务未启动");
        return;
      }
    }

    const nextTurn = createTurn(cleanQuery);
    // 提交即恢复跟随，否则上一轮回看留下的「不跟随」会一直生效
    forceFollowRef.current = true;
    setTurns((previous) => [...previous, nextTurn]);
    setQuery("");

    // 会话索引：首问定标题，之后只刷新时间与条数（侧栏一个会话一条）
    setSessions((previous) => {
      const next = touchSession(previous, activeSessionId, cleanQuery);
      saveSessionIndex(next);
      return next;
    });

    try {
      await session.submitTask(cleanQuery);
      message.success("任务已启动，执行过程会显示在对话中");
    } catch (error) {
      setTurns((previous) =>
        previous.map((turn) =>
          turn.id === nextTurn.id
            ? {
                ...turn,
                isRunning: false,
                result: error instanceof Error ? error.message : "任务启动失败"
              }
            : turn
        )
      );
      message.error(error instanceof Error ? error.message : "任务启动失败");
    }
  }

  async function handleCancel() {
    try {
      const response = await session.cancelCurrentTask();
      message.info(
        response.status === "cancelling" ? "取消请求已发送，正在等待当前调用结束" : "任务已取消"
      );
    } catch (error) {
      message.error(error instanceof Error ? error.message : "取消任务失败");
    }
  }

  function handleRemoveStaged(uid: string) {
    setStagedItems((previous) => previous.filter((item) => item.uid !== uid));
  }

  async function handleRemoveUploaded(uid: string) {
    try {
      await session.removeUploadedItem(uid);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "移除附件失败");
    }
  }

  /** 新建研搜 = 新建会话页：旧会话留在侧栏，随时可点回 */
  function handleNewSession() {
    const nextSessionId = session.resetSession();
    setTurns([]);
    setQuery("");
    setStagedItems([]);
    setSessionFilter("");
    setMenuSessionId(null);
    setView("chat");
    setActiveSessionId(nextSessionId);
    restoredTurnIdsRef.current.clear();
    followRef.current = true;
    forceFollowRef.current = false;
    setAtBottom(true);
    setSessions((previous) => {
      const next = [createSessionEntry(nextSessionId), ...previous];
      saveSessionIndex(next);
      return next;
    });
  }

  /** 点侧栏条目：切到该会话页，并落到它最近的一轮 */
  function handleSelectSession(target: SessionIndex) {
    setView("chat");
    setMenuSessionId(null);

    if (target.id !== activeSessionId) {
      session.selectSession(target.id);
      setTurns([]);
      setQuery("");
      setStagedItems([]);
      setActiveSessionId(target.id);
      restoredTurnIdsRef.current.clear();
    }

    // 定位交给「落到最近一轮」的那条副作用：它等轮次渲染完才动。
    // 同时把跟随关掉，避免自动跟随立刻把视图拉到底、把这次定位抵消掉。
    followRef.current = false;
    setAtBottom(false);
    pendingScrollRef.current = true;
    if (target.id === activeSessionId) {
      // 同一会话时 turns 引用不变，副作用不会触发，这里补一次空更新把它叫醒
      setTurns((previous) => [...previous]);
    }
  }

  /** 删除会话：二次确认后连同该会话的对话记忆与产物一起删（长期记忆不动） */
  function handleDeleteSession(target: SessionIndex) {
    setMenuSessionId(null);
    const title = target.title || "新会话";

    modal.confirm({
      cancelText: "取消",
      content:
        "将同时删除该会话的全部对话记忆与产物（消息、会话图状态、上传附件、输出文件）。长期记忆不受影响。",
      okButtonProps: { danger: true },
      okText: "删除",
      onOk: async () => {
        try {
          await deleteSessionApi(target.id, getStoredUserId());
        } catch (error) {
          message.error(error instanceof Error ? error.message : "删除会话失败");
          return;
        }

        const rest = removeSessionEntry(sessionsRef.current, target.id);
        message.success("会话已删除");

        if (target.id !== activeSessionId) {
          saveSessionIndex(rest);
          setSessions(rest);
          return;
        }

        // 删的是当前会话：切到相邻会话；一个都不剩就新开一页空白会话
        const fallbackId = rest.length > 0 ? rest[0].id : session.resetSession();
        const nextIndex = rest.length > 0 ? rest : [createSessionEntry(fallbackId)];
        saveSessionIndex(nextIndex);
        setSessions(nextIndex);
        if (rest.length > 0) {
          session.selectSession(fallbackId);
        }
        setActiveSessionId(fallbackId);
        setTurns([]);
        setStagedItems([]);
        restoredTurnIdsRef.current.clear();
      },
      title: `删除会话「${title}」？`
    });
  }

  /** 侧栏按会话渲染：搜索只过滤索引，条目数始终等于会话数 */
  const visibleSessions = useMemo(() => {
    const keyword = sessionFilter.trim().toLowerCase();
    if (!keyword) {
      return sessions;
    }
    return sessions.filter((item) =>
      (item.title || "新会话").toLowerCase().includes(keyword)
    );
  }, [sessionFilter, sessions]);

  const online = session.connectionState === "connected";
  const status = badgeStatus(session.connectionState);

  // 两种布局共用同一条输入条：空态居中、有会话时落底
  const composer = (
    <ChatComposer
      isCancelling={session.isCancelling}
      isRunning={session.isRunning}
      isUploading={session.isUploading}
      onCancel={handleCancel}
      onNewSession={handleNewSession}
      onQueryChange={setQuery}
      onRemoveStaged={handleRemoveStaged}
      onRemoveUploaded={handleRemoveUploaded}
      onStagedItemsChange={setStagedItems}
      onSubmit={handleSubmit}
      query={query}
      stagedItems={stagedItems}
      uploadedItems={session.uploadedItems}
    />
  );

  return (
    <div className="agent-app">
      <header className="agent-header">
        <div className="agent-brand">
          <span className="agent-wordmark">DEEPSEARCH</span>
          <span className="agent-brand-sep">/</span>
          <span className="agent-brand-tag">多智能体研究台</span>
        </div>

        <div className="agent-header-center">
          <span className="agent-badge">
            <span className="agent-dot" data-status={status} aria-hidden="true" />
            <span className="agent-badge-name">{connectionLabel(session.connectionState)}</span>
          </span>
          <span className="agent-badge-model" title={session.threadId}>
            thread {session.threadId.slice(0, 8)}
          </span>
        </div>

        <div className="agent-header-right">
          <span className="agent-head-btn" data-on={session.isRunning} aria-live="polite">
            <span className="agent-btn-glyph">{session.isRunning ? "●" : "○"}</span>
            {session.isRunning ? "研搜中" : "待命"}
          </span>
        </div>
      </header>

      <div className="agent-body">
        <aside className="agent-rail" aria-label="会话信息">
          <div className="agent-quick">
            <button className="agent-new-btn" onClick={handleNewSession} type="button">
              <span className="agent-new-icon" aria-hidden="true">
                <PlusOutlined />
              </span>
              <span className="agent-new-text">
                <span className="agent-new-title">新建研搜</span>
                <span className="agent-new-sub">从空白开始</span>
              </span>
            </button>
          </div>

          <nav className="agent-nav-card" aria-label="功能导航">
            {VIEWS.map((item) => (
              <button
                className="agent-nav-item"
                data-active={view === item.key}
                key={item.key}
                onClick={() => setView(item.key)}
                type="button"
              >
                <span className="agent-nav-glyph" aria-hidden="true">
                  {item.icon}
                </span>
                <span>{item.label}</span>
              </button>
            ))}
          </nav>

          <div className="agent-search-card">
            <div className="agent-search-head">
              <span className="agent-search-label">搜索会话</span>
            </div>
            <div className="agent-search-box">
              <SearchOutlined className="agent-search-icon" aria-hidden="true" />
              <input
                aria-label="搜索会话"
                className="agent-search-input"
                onChange={(event) => setSessionFilter(event.target.value)}
                placeholder="搜索会话..."
                spellCheck={false}
                value={sessionFilter}
              />
            </div>
          </div>

          <SessionRail
            activeId={activeSessionId}
            filtered={sessionFilter.trim().length > 0}
            menuId={menuSessionId}
            onDelete={handleDeleteSession}
            onSelect={handleSelectSession}
            onToggleMenu={setMenuSessionId}
            sessions={visibleSessions}
          />

          <div className="agent-rail-stats" aria-label="运行统计">
            <div className="agent-rail-stat">
              <span>助手调度</span>
              <strong>{session.stats.assistantEvents}</strong>
            </div>
            <div className="agent-rail-stat">
              <span>工具调用</span>
              <strong>{session.stats.toolEvents}</strong>
            </div>
            <div className="agent-rail-stat">
              <span>输出文件</span>
              <strong>{session.stats.fileCount}</strong>
            </div>
            <div className="agent-rail-stat">
              <span>异常</span>
              <strong>{session.stats.errorEvents}</strong>
            </div>
          </div>

          <div className="agent-railbase">
            <div className="agent-account">
              <span className="agent-avatar" aria-hidden="true">
                {online ? "WB" : "—"}
              </span>
              <span className="agent-account-name">深度研搜工作台</span>
              <span className="agent-account-role">{online ? "在线" : "离线"}</span>
            </div>
          </div>
        </aside>

        <main
          className="agent-main"
          data-empty={view === "chat" && turns.length === 0 ? "true" : undefined}
        >
          {view === "import" || view === "kb" ? (
            <div className="admin-layout" style={{ height: "100%", overflowY: "auto" }}>
              <div className="admin-content">
                {view === "import" ? (
                  <ImportPage threadId={session.threadId} />
                ) : (
                  <KbManagerPage />
                )}
              </div>
            </div>
          ) : turns.length === 0 ? (
            <AgentWelcome composer={composer} onUseCase={setQuery} />
          ) : (
            <>
              {session.lastError ? (
                <div style={{ padding: "12px 32px 0" }}>
                  <Alert message={session.lastError} showIcon type="error" />
                </div>
              ) : null}

              <div className="agent-stream-wrap">
                <div className="agent-stream" onScroll={handleStreamScroll} ref={streamRef}>
                  <ConversationThread turns={turns} />
                </div>

                {session.isRunning && !atBottom ? (
                  <button
                    aria-label="回到底部"
                    className="agent-jump-bottom"
                    onClick={handleJumpToBottom}
                    type="button"
                  >
                    <ArrowDownOutlined aria-hidden="true" />
                    <span>回到底部</span>
                  </button>
                ) : null}
              </div>

              {composer}
            </>
          )}
        </main>
      </div>
    </div>
  );
}
