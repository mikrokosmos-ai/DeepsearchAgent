import {
  BranchesOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  DownloadOutlined,
  FileMarkdownOutlined,
  FilePdfOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  InfoCircleOutlined,
  LoadingOutlined,
  MinusCircleOutlined,
  StopOutlined,
  ToolOutlined
} from "@ant-design/icons";
import { useEffect, useRef, useState } from "react";
import { getDownloadUrl } from "../lib/api";
import { KnowledgeImage, MarkdownRenderer } from "./MarkdownRenderer";
import type { MonitorMessage, OutputFile, SubAgentNotice } from "../types";

export interface ChatTurn {
  id: string;
  content: string;
  events: MonitorMessage[];
  files: OutputFile[];
  /** 本轮知识库命中配图（来自检索链路的 rag_final 事件） */
  imageUrls: string[];
  isRunning: boolean;
  notices: SubAgentNotice[];
  result: string;
  timestamp: string;
}

interface ConversationThreadProps {
  turns: ChatTurn[];
}

/**
 * 轨迹通道：与 Ragent 的 TraceChannel 同名同义。
 * 每个通道配一枚 mono 字形，▮ 答复节点是全流唯一的橙节点。
 */
type TraceChannel = "user" | "reasoning" | "tool" | "answer" | "hint" | "error";

const GLYPH: Record<TraceChannel, string> = {
  user: "▷",
  reasoning: "○",
  tool: "●",
  answer: "▮",
  hint: "·",
  error: "✕"
};

const CHANNEL_NAME: Record<TraceChannel, string> = {
  user: "you",
  reasoning: "reasoning",
  tool: "tool",
  answer: "answer",
  hint: "hint",
  error: "error"
};

interface TraceRow {
  key: string;
  channel: TraceChannel;
  ts: string;
  text?: string;
  data?: Record<string, unknown>;
  streaming?: boolean;
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleTimeString("zh-CN", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function parseTime(value: string): number | null {
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? null : time;
}

/** 耗时刻度：1s 内按毫秒、10s 内留一位小数、1m 起转 m/s 复合 */
function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) {
    return "";
  }
  if (ms < 1) {
    return "<1ms";
  }
  if (ms < 1000) {
    return `${Math.round(ms)}ms`;
  }
  if (ms < 10_000) {
    return `${(ms / 1000).toFixed(1)}s`;
  }
  const secs = Math.round(ms / 1000);
  if (secs < 60) {
    return `${secs}s`;
  }
  return `${Math.floor(secs / 60)}m${String(secs % 60).padStart(2, "0")}s`;
}

function getLastEventTime(events: MonitorMessage[], eventName?: string): number | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (!eventName || event.event === eventName) {
      return parseTime(event.timestamp);
    }
  }
  return null;
}

/** 本轮墙上时间：首事件 → task_result（或最后一个事件） */
function turnElapsed(turn: ChatTurn, now: number): number | null {
  const startedAt =
    (turn.events[0] ? parseTime(turn.events[0].timestamp) : null) ??
    parseTime(turn.timestamp);
  if (startedAt == null) {
    return null;
  }
  const finishedAt =
    getLastEventTime(turn.events, "task_result") ??
    (!turn.isRunning ? getLastEventTime(turn.events) : null) ??
    now;
  return Math.max(0, finishedAt - startedAt);
}

/** 事件名 → 轨迹通道 */
function eventChannel(event: string): TraceChannel {
  if (event === "assistant_call") {
    return "reasoning";
  }
  if (event === "tool_start") {
    return "tool";
  }
  if (event === "error") {
    return "error";
  }
  if (event === "task_cancelled") {
    return "error";
  }
  return "hint";
}

function eventStatus(event: string): string | null {
  if (event === "task_result") {
    return "完成";
  }
  if (event === "task_cancelled") {
    return "已停止";
  }
  if (event === "error") {
    return "失败";
  }
  if (event === "session_created") {
    return "已建会话";
  }
  return null;
}

function eventStatusClass(event: string): string {
  if (event === "task_result") {
    return "agent-status-ok";
  }
  if (event === "task_cancelled") {
    return "agent-status-idle";
  }
  return "agent-status-err";
}

function EventIcon({ event }: { event: string }) {
  if (event === "assistant_call") {
    return <BranchesOutlined aria-hidden />;
  }
  if (event === "tool_start") {
    return <ToolOutlined aria-hidden />;
  }
  if (event === "session_created") {
    return <FileSearchOutlined aria-hidden />;
  }
  if (event === "task_result") {
    return <CheckCircleOutlined aria-hidden />;
  }
  if (event === "task_cancelled") {
    return <StopOutlined aria-hidden />;
  }
  if (event === "error") {
    return <CloseCircleOutlined aria-hidden />;
  }
  return <ClockCircleOutlined aria-hidden />;
}

function FileIcon({ name }: { name: string }) {
  if (name.endsWith(".pdf")) {
    return <FilePdfOutlined aria-hidden />;
  }
  if (name.endsWith(".md")) {
    return <FileMarkdownOutlined aria-hidden />;
  }
  return <FileTextOutlined aria-hidden />;
}

/** 提示语气：正常 / 被截断 / 链路故障 / 未解析（降级） */
function noticeTone(notice: SubAgentNotice): string {
  if (!notice.parsed) {
    return "degraded";
  }
  if (notice.error) {
    return "failed";
  }
  if (notice.truncated_by_limit) {
    return "truncated";
  }
  return "normal";
}

function noticeText(notice: SubAgentNotice): string {
  if (!notice.parsed) {
    return "未给出返回契约代码块，已按正文内容降级理解";
  }
  if (notice.error) {
    return `检索链路故障：${notice.error}`;
  }
  if (notice.truncated_by_limit) {
    return `信息因上限被截断（来源 ${notice.sources} 条），结论可能不完整`;
  }
  return `返回正常（来源 ${notice.sources} 条）`;
}

/** 工具行/思考行/答复行：一律是 40px 左轴 + 元信息行 + 正文 */
function TraceRowItem({ row, showTs }: { row: TraceRow; showTs: boolean }) {
  const failed = row.channel === "error";

  return (
    <div
      className="agent-row"
      data-channel={row.channel}
      data-failed={failed}
      data-streaming={Boolean(row.streaming)}
    >
      <div className="agent-row-rail">
        <span className="agent-node">{GLYPH[row.channel]}</span>
      </div>
      <div className="agent-row-content">
        <div className="agent-meta-line">
          <span className="agent-channel">{CHANNEL_NAME[row.channel]}</span>
          {row.channel === "tool" && row.text ? (
            <span className="agent-tool-chip">{row.text}</span>
          ) : null}
          {row.channel === "hint" && row.text ? (
            <span className="agent-status-idle">{row.text}</span>
          ) : null}
          {row.ts && showTs ? <span className="agent-row-ts">{row.ts}</span> : null}
        </div>
        <TraceRowBody row={row} />
      </div>
    </div>
  );
}

function TraceRowBody({ row }: { row: TraceRow }) {
  if (row.channel === "tool" && row.data) {
    return <ToolBox data={row.data} />;
  }
  if (row.channel === "answer") {
    return (
      <div className="agent-answer-form">
        <MarkdownRenderer content={row.text ?? ""} />
      </div>
    );
  }
  if (row.channel === "error") {
    return <div className="agent-row-text">{row.text}</div>;
  }
  if (row.channel === "hint") {
    return null;
  }
  return <div className="agent-row-text">{row.text}</div>;
}

/** 工具块：一行参数摘要 + 展开看完整入参 */
function ToolBox({ data }: { data: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  const raw = JSON.stringify(data, null, 2);

  return (
    <div className="agent-toolbox">
      <button
        aria-expanded={open}
        className="agent-tool-summary"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <span className="agent-caret">{open ? "▾" : "▸"}</span>
        <span className="agent-tool-preview">{summarize(data)}</span>
      </button>
      {open ? <pre className="agent-pre">{raw}</pre> : null}
    </div>
  );
}

function summarize(data: Record<string, unknown>): string {
  const compact = JSON.stringify(data);
  if (!compact || compact === "{}") {
    return "（无参数）";
  }
  if (compact.length <= 96) {
    return compact;
  }
  const keys = Object.keys(data);
  return `对象 · ${keys.slice(0, 4).join(", ")}${keys.length > 4 ? "…" : ""}`;
}

/** 一轮的轨迹行：user 行 + 各事件的工具/思考/错误行 + answer 行 */
function buildTurnRows(turn: ChatTurn): TraceRow[] {
  const rows: TraceRow[] = [];

  rows.push({
    key: `u-${turn.id}`,
    channel: "user",
    ts: formatTime(turn.timestamp),
    text: turn.content
  });

  turn.events.forEach((event, index) => {
    const channel = eventChannel(event.event);
    const status = eventStatus(event.event);
    rows.push({
      key: `e-${turn.id}-${index}`,
      channel,
      ts: formatTime(event.timestamp),
      text: channel === "tool" ? event.message : event.message,
      data: channel === "tool" ? event.data : undefined,
      streaming: turn.isRunning && index === turn.events.length - 1 && channel !== "error"
    });
    // 终态事件额外把状态标挂在同一行（Ragent 的做法：状态与内容同行）
    if (status) {
      rows[rows.length - 1].text = event.message;
    }
  });

  if (turn.isRunning && turn.events.length === 0) {
    rows.push({
      key: `wait-${turn.id}`,
      channel: "hint",
      ts: "",
      text: "等待响应…",
      streaming: true
    });
  }

  if (turn.result) {
    rows.push({
      key: `a-${turn.id}`,
      channel: "answer",
      ts: "",
      text: turn.result
    });
  } else if (turn.isRunning && turn.events.length > 0) {
    rows.push({
      key: `a-pending-${turn.id}`,
      channel: "hint",
      ts: "",
      text: "正在汇总答复…",
      streaming: true
    });
  }

  return rows;
}

function NoticeList({ notices }: { notices: SubAgentNotice[] }) {
  return (
    <ul className="agent-notice-list">
      {notices.map((notice, index) => (
        <li
          className={`agent-notice agent-notice--${noticeTone(notice)}`}
          key={`${notice.subagent}-${index}`}
        >
          <strong>{notice.subagent}</strong>
          <span>{noticeText(notice)}</span>
        </li>
      ))}
    </ul>
  );
}

function FileList({ files }: { files: OutputFile[] }) {
  return (
    <div className="agent-notice-list">
      {files.map((file) => (
        <div className="agent-file" key={file.path}>
          <span className="agent-file-icon">
            <FileIcon name={file.name} />
          </span>
          <span className="agent-file-copy">
            <span className="agent-file-name" title={file.name}>
              {file.name}
            </span>
            <span className="agent-file-size">{formatBytes(file.size)}</span>
          </span>
          <a
            aria-label={`下载 ${file.name}`}
            className="agent-file-dl"
            href={getDownloadUrl(file.path)}
            rel="noreferrer"
            target="_blank"
          >
            <DownloadOutlined />
          </a>
        </div>
      ))}
    </div>
  );
}

/** 轮次卡：卡头 TURN N + 总耗时，卡内是轨迹行 */
function TurnCard({ turn, index }: { turn: ChatTurn; index: number }) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!turn.isRunning) {
      return;
    }
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [turn.isRunning]);

  const rows = buildTurnRows(turn);
  const headTs = rows[0]?.ts ?? "";
  const elapsedMs = turnElapsed(turn, now);
  // 流式中不显示总耗时 收尾实测后才亮
  const elapsed = turn.isRunning ? "" : elapsedMs != null ? formatDuration(elapsedMs) : "";

  return (
    <section className="agent-turn" id={`turn-${turn.id}`}>
      <header className="agent-turn-head">
        <span className="agent-turn-no">TURN {index + 1}</span>
        <span className="agent-turn-ts">
          {headTs}
          {elapsed ? <span className="agent-turn-dur"> · {elapsed}</span> : null}
        </span>
      </header>
      {rows.map((row, rowIndex) => (
        <TraceRowItem key={row.key} row={row} showTs={rowIndex > 0} />
      ))}

      {turn.imageUrls.length > 0 ? (
        <div className="agent-row" data-channel="hint">
          <div className="agent-row-rail">
            <span className="agent-node">·</span>
          </div>
          <div className="agent-row-content">
            <div className="agent-meta-line">
              <span className="agent-channel">images</span>
              <span className="agent-status-idle">
                知识库命中配图 {turn.imageUrls.length} 张
              </span>
            </div>
            <div className="agent-image-strip" aria-label="知识库命中配图">
              {turn.imageUrls.map((url) => (
                <KnowledgeImage alt="知识库命中配图" key={url} src={url} thumb />
              ))}
            </div>
          </div>
        </div>
      ) : null}

      {turn.notices.length > 0 || turn.files.length > 0 ? (
        <div className="agent-row" data-channel="hint">
          <div className="agent-row-rail">
            <span className="agent-node">·</span>
          </div>
          <div className="agent-row-content" style={{ paddingBottom: 8 }}>
            <div className="agent-meta-line">
              <span className="agent-channel">artifacts</span>
            </div>
            {turn.notices.length > 0 ? (
              <div style={{ marginBottom: turn.files.length > 0 ? 8 : 0 }}>
                <NoticeList notices={turn.notices} />
              </div>
            ) : null}
            {turn.files.length > 0 ? <FileList files={turn.files} /> : null}
          </div>
        </div>
      ) : null}
    </section>
  );
}

export function ConversationThread({ turns }: ConversationThreadProps) {
  return (
    <div className="agent-stream-rows" aria-label="聊天消息流">
      {turns.map((turn, index) => (
        <TurnCard index={index} key={turn.id} turn={turn} />
      ))}
    </div>
  );
}

/** 供外部复用的加载中字形（导入页等场景） */
export function SpinnerGlyph() {
  return <LoadingOutlined aria-hidden />;
}

/** 供外部复用的待执行字形 */
export function PendingGlyph() {
  return <MinusCircleOutlined aria-hidden />;
}

/** 供外部复用的信息提示字形 */
export function InfoGlyph() {
  return <InfoCircleOutlined aria-hidden />;
}
