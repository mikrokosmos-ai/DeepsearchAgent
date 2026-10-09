import { MessageOutlined, MoreOutlined, SearchOutlined } from "@ant-design/icons";
import { NEW_SESSION_TITLE } from "../lib/sessions";
import type { SessionIndex } from "../lib/sessions";

interface SessionRailProps {
  sessions: SessionIndex[];
  activeId: string;
  /** 是否处于搜索过滤态：空态文案据此区分「没有会话」与「没有匹配」 */
  filtered: boolean;
  menuId: string | null;
  onDelete: (session: SessionIndex) => void;
  onSelect: (session: SessionIndex) => void;
  onToggleMenu: (id: string | null) => void;
}

/** 侧栏的行尾时间：当天只给时刻，跨天补月日，避免「11:20」看不出是哪天 */
function formatClock(value: number): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const clock = date.toLocaleTimeString("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit"
  });
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return clock;
  }
  return `${date.getMonth() + 1}/${date.getDate()} ${clock}`;
}

/**
 * 侧栏会话列表：一个会话一条，行尾「更多」展开删除入口。
 *
 * 这里刻意不用「一行一轮」的平铺：侧栏的实体是会话页，轮次属于页内内容。
 */
export function SessionRail({
  sessions,
  activeId,
  filtered,
  menuId,
  onDelete,
  onSelect,
  onToggleMenu
}: SessionRailProps) {
  return (
    <section className="agent-sessions">
      <div className="agent-session-wrap">
        <div className="agent-session-list">
          {sessions.length === 0 ? (
            <div className="agent-rail-empty">
              {filtered ? <SearchOutlined aria-hidden="true" /> : <MessageOutlined aria-hidden="true" />}
              <p>{filtered ? "无匹配会话" : "暂无会话记录"}</p>
            </div>
          ) : (
            <>
              <div className="agent-session-group">最近会话</div>
              {sessions.map((item) => {
                const title = item.title || NEW_SESSION_TITLE;
                const open = menuId === item.id;
                return (
                  <div
                    className="agent-session-item"
                    data-active={item.id === activeId}
                    data-menu={open}
                    key={item.id}
                  >
                    <button
                      className="agent-session-btn"
                      onClick={() => onSelect(item)}
                      title={title}
                      type="button"
                    >
                      <span className="agent-session-title agent-session-title--fade">{title}</span>
                      <span className="agent-session-meta">{formatClock(item.updatedAt)}</span>
                    </button>

                    <button
                      aria-expanded={open}
                      aria-label={`会话操作：${title}`}
                      className="agent-item-btn"
                      onClick={() => onToggleMenu(open ? null : item.id)}
                      type="button"
                    >
                      <MoreOutlined />
                    </button>

                    {open ? (
                      <div className="agent-session-menu">
                        <button
                          className="agent-session-action"
                          data-danger="true"
                          onClick={() => onDelete(item)}
                          type="button"
                        >
                          删除会话
                        </button>
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </>
          )}
        </div>
        <span className="agent-session-fade" aria-hidden="true" />
      </div>
    </section>
  );
}
