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
  MoreOutlined,
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
import { API_BASE_URL, WS_BASE_URL } from "./lib/config";
import { useDeepAgentSession } from "./hooks/useDeepAgentSession";
import type { ConnectionState, UploadedItem } from "./types";

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

/** 会话标题：取首个提问的前 18 字，空会话给占位名 */
function turnTitle(turn: ChatTurn): string {
  const text = turn.content.trim().replace(/\s+/g, " ");
  if (!text) {
    return "新会话";
  }
  return text.length > 18 ? `${text.slice(0, 18)}…` : text;
}

/** 距底多少像素内仍算「正在看最新内容」；越小越容易被判为已上滚 */
const BOTTOM_EPS = 12;

function formatClock(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleTimeString("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit"
  });
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

export default function App() {
  const { message } = AntApp.useApp();
  const [view, setView] = useState<AppView>("chat");
  const [query, setQuery] = useState("");
  const [stagedItems, setStagedItems] = useState<UploadedItem[]>([]);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [sessionFilter, setSessionFilter] = useState("");
  const [pickedTurnId, setPickedTurnId] = useState<string | null>(null);
  const streamRef = useRef<HTMLDivElement | null>(null);
  const wasRunningRef = useRef(false);
  // 是否跟随最新内容：由滚动位置派生，用户一旦离开底部立即置否
  const followRef = useRef(true);
  // 新一轮提交强制回到跟随态：提交时用户可能正停在历史位置上
  const forceFollowRef = useRef(false);
  // 浮标可见性单独用状态承载，只在布尔翻转时重渲染（滚动事件很密）
  const [atBottom, setAtBottom] = useState(true);
  const session = useDeepAgentSession();

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

  function handleNewSession() {
    session.resetSession();
    setTurns([]);
    setQuery("");
    setStagedItems([]);
    setPickedTurnId(null);
    setView("chat");
    // 新会话从空白页开始：重新回到跟随态，避免继承上一会话的回看位置
    followRef.current = true;
    forceFollowRef.current = false;
    setAtBottom(true);
  }

  /** 会话分组：照 Ragent 的 今天 / 更早 两桶（本项目只有内存态会话，不落库按天分） */
  const sessionGroups = useMemo(() => {
    const keyword = sessionFilter.trim().toLowerCase();
    const matched = keyword
      ? turns.filter((turn) => turnTitle(turn).toLowerCase().includes(keyword))
      : turns;
    return matched.slice().reverse();
  }, [sessionFilter, turns]);

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

          <section className="agent-sessions">
            <div className="agent-session-wrap">
              <div className="agent-session-list">
                {turns.length === 0 ? (
                  <div className="agent-rail-empty">
                    <MessageOutlined aria-hidden="true" />
                    <p>暂无会话记录</p>
                  </div>
                ) : sessionGroups.length === 0 ? (
                  <div className="agent-rail-empty">
                    <SearchOutlined aria-hidden="true" />
                    <p>无匹配会话</p>
                  </div>
                ) : (
                  <>
                    <div className="agent-session-group">最近提问</div>
                    {sessionGroups.map((turn) => (
                      <div
                        className="agent-session-item"
                        data-active={pickedTurnId === turn.id}
                        key={turn.id}
                      >
                        <button
                          className="agent-session-btn"
                          onClick={() => {
                            setView("chat");
                            setPickedTurnId(turn.id);
                            const node = document.getElementById(`turn-${turn.id}`);
                            node?.scrollIntoView({ behavior: "smooth", block: "start" });
                          }}
                          title={turn.content}
                          type="button"
                        >
                          <span className="agent-session-title agent-session-title--fade">
                            {turnTitle(turn)}
                          </span>
                          <span className="agent-session-meta">
                            {formatClock(turn.timestamp)}
                          </span>
                        </button>
                        <span className="agent-item-btn" aria-hidden="true">
                          <MoreOutlined />
                        </span>
                      </div>
                    ))}
                  </>
                )}
              </div>
              <span className="agent-session-fade" aria-hidden="true" />
            </div>
          </section>

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
