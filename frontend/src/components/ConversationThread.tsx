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
  /** 终态标记（完成 / 已停止 / 失败 / 已建会话），与内容同行显示 */
  status?: string;
  statusClass?: string;
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
          {row.channel === "hint" && row.text && !row.status ? (
            <span className="agent-status-idle">{row.text}</span>
          ) : null}
          {row.status ? (
            <span className={row.statusClass ?? "agent-status-idle"}>{row.status}</span>
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

/* ══ 过程区：工具按名归组、进度原地刷新 ═══════════════════════════════════ */

/** 进度事件：检索 / 导入链路的节点进度与流式增量（一律归入所属调用的组内） */
function isProgressEvent(event: string): boolean {
  return event.endsWith("_progress") || event.endsWith("_delta");
}

/** 子问题取值键：各工具入参里代表「这次问的是什么」的字段，按顺序取第一个非空项 */
const SUBJECT_KEYS = [
  "question",
  "query",
  "table_name",
  "filename",
  "instruction",
  "写入的文本内容"
];

interface CallInfo {
  key: string;
  /** 该次调用的子问题 */
  subject: string;
  /** 该次调用内的最后一条进度台账（原地刷新，不逐条追加） */
  progressText?: string;
  progressTs?: string;
}

interface ProcessGroup {
  kind: "group";
  key: string;
  toolName: string;
  label: string;
  code: string;
  calls: CallInfo[];
}

interface ProcessRowItem {
  kind: "row";
  key: string;
  row: TraceRow;
}

type ProcessItem = ProcessGroup | ProcessRowItem;

interface TurnProcess {
  items: ProcessItem[];
  totalCalls: number;
  groupCount: number;
  progressCount: number;
}

function readToolName(event: MonitorMessage): string {
  const raw = event.data?.tool_name;
  if (typeof raw === "string" && raw.trim()) {
    return raw.trim();
  }
  const prefix = "开始执行工具: ";
  return event.message.startsWith(prefix)
    ? event.message.slice(prefix.length).trim()
    : event.message.trim();
}

/** 工具入参：report_tool 把它放在 data.args 下，整包 data 只作兜底 */
function readToolArgs(event: MonitorMessage): Record<string, unknown> | undefined {
  const nested = event.data?.args;
  if (nested && typeof nested === "object") {
    return nested as Record<string, unknown>;
  }
  return event.data;
}

/** 工具名常见形如「本地知识库检索工具：local_rag_search」→ 中文名 + 代码名 */
function splitToolName(toolName: string): { label: string; code: string } {
  const index = toolName.indexOf("：");
  if (index <= 0) {
    return { label: toolName, code: "" };
  }
  return { label: toolName.slice(0, index), code: toolName.slice(index + 1) };
}

function callSubject(args: Record<string, unknown> | undefined): string {
  if (!args) {
    return "";
  }
  for (const key of SUBJECT_KEYS) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim().replace(/\s+/g, " ");
    }
  }
  return summarize(args);
}

/**
 * 把一轮的事件拆成过程区结构。
 *
 * 归组的边界是 `tool_start`：它之后、下一个 `tool_start` 之前的进度事件都算这一次调用的，
 * 且组内只保留最后一条（原地刷新）。同名工具的多次调用共用一行并标注次数，
 * 子问题作为展开项逐个列出——这正是「多角度检索」的本来面目，不必再平铺成多套流程。
 *
 * 子智能体契约提示由 `turn.notices` 承载（渲染在答复区的 artifacts 块），此处不再逐条成行，
 * 否则同一件事会在两处重复出现。
 */
function buildTurnProcess(turn: ChatTurn): TurnProcess {
  const items: ProcessItem[] = [];
  const groupByTool = new Map<string, ProcessGroup>();
  let currentCall: CallInfo | null = null;
  let looseProgressIndex = -1;
  let progressCount = 0;

  turn.events.forEach((event, index) => {
    if (event.event === "tool_start") {
      const toolName = readToolName(event);
      let group = groupByTool.get(toolName);
      if (!group) {
        const { label, code } = splitToolName(toolName);
        group = {
          kind: "group",
          key: `g-${turn.id}-${toolName}`,
          toolName,
          label,
          code,
          calls: []
        };
        groupByTool.set(toolName, group);
        items.push(group);
      }
      const call: CallInfo = {
        key: `c-${turn.id}-${index}`,
        subject: callSubject(readToolArgs(event))
      };
      group.calls.push(call);
      currentCall = call;
      looseProgressIndex = -1;
      return;
    }

    if (isProgressEvent(event.event)) {
      progressCount += 1;
      if (currentCall) {
        currentCall.progressText = event.message;
        currentCall.progressTs = event.timestamp;
        return;
      }
      // 没有归属调用（如会话级导入进度）：同样只留最新一条，不逐条追加
      const loose = looseProgressIndex >= 0 ? items[looseProgressIndex] : null;
      if (loose && loose.kind === "row") {
        loose.row.text = event.message;
        loose.row.ts = formatTime(event.timestamp);
        return;
      }
      const key = `p-${turn.id}-${index}`;
      items.push({
        kind: "row",
        key,
        row: { key, channel: "hint", ts: formatTime(event.timestamp), text: event.message }
      });
      looseProgressIndex = items.length - 1;
      return;
    }

    if (event.event === "subagent_report") {
      return;
    }

    const key = `e-${turn.id}-${index}`;
    items.push({
      kind: "row",
      key,
      row: {
        key,
        channel: eventChannel(event.event),
        ts: formatTime(event.timestamp),
        text: event.message,
        status: eventStatus(event.event) ?? undefined,
        statusClass: eventStatusClass(event.event)
      }
    });
  });

  const groups = items.filter((item): item is ProcessGroup => item.kind === "group");

  return {
    items,
    totalCalls: groups.reduce((sum, group) => sum + group.calls.length, 0),
    groupCount: groups.length,
    progressCount
  };
}

function ToolGroupRow({ group, expanded }: { group: ProcessGroup; expanded: boolean }) {
  return (
    <div className="agent-row agent-tool-group" data-channel="tool" data-tool={group.toolName}>
      <div className="agent-row-rail">
        <span className="agent-node">{GLYPH.tool}</span>
      </div>
      <div className="agent-row-content">
        <div className="agent-meta-line">
          <span className="agent-channel">{CHANNEL_NAME.tool}</span>
          <span className="agent-tool-chip">{group.label}</span>
          {group.code ? <span className="agent-tool-code">{group.code}</span> : null}
          {group.calls.length > 1 ? (
            <span className="agent-row-count">{`×${group.calls.length}`}</span>
          ) : null}
        </div>
        {expanded ? (
          <ol className="agent-tool-calls">
            {group.calls.map((call) => (
              <li className="agent-tool-call" key={call.key}>
                <span className="agent-call-subject">{call.subject || "（未提供子问题）"}</span>
                {call.progressText ? (
                  <span className="agent-call-progress">
                    <span className="agent-call-progress-ts">
                      {call.progressTs ? formatTime(call.progressTs) : ""}
                    </span>
                    <span className="agent-call-progress-text">{call.progressText}</span>
                  </span>
                ) : null}
              </li>
            ))}
          </ol>
        ) : null}
      </div>
    </div>
  );
}

/**
 * 过程区：默认折叠为「一行摘要 + 每种工具一行分组」。
 *
 * 分组行常驻（它本身就是折叠态的摘要），只有非工具的细节行与子问题清单随展开出现；
 * 错误行例外——失败必须一眼可见，不能藏在折叠里。
 */
function ProcessZone({ turn }: { turn: ChatTurn }) {
  const [open, setOpen] = useState(false);
  const process = buildTurnProcess(turn);

  if (process.items.length === 0) {
    return null;
  }

  const brief: string[] = [];
  if (process.groupCount > 0) {
    brief.push(`工具 ${process.groupCount} 种 · ${process.totalCalls} 次调用`);
  }
  if (process.progressCount > 0) {
    brief.push(`进度 ${process.progressCount} 条`);
  }

  return (
    <div className="agent-process-zone" data-collapsed={!open}>
      <button
        aria-expanded={open}
        className="agent-process-toggle"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <span className="agent-caret">{open ? "▾" : "▸"}</span>
        <span className="agent-process-title">执行过程</span>
        {brief.length > 0 ? <span className="agent-process-brief">{brief.join(" · ")}</span> : null}
      </button>

      <div className="agent-process-body">
        {process.items.map((item) => {
          if (item.kind === "group") {
            return <ToolGroupRow expanded={open} group={item} key={item.key} />;
          }
          if (!open && item.row.channel !== "error") {
            return null;
          }
          return <TraceRowItem key={item.key} row={item.row} showTs />;
        })}
      </div>
    </div>
  );
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

/* ══ 答复区：本轮产出（答案 + 配图 + 产物）═══════════════════════════════ */

function buildPromptRow(turn: ChatTurn): TraceRow {
  return {
    key: `u-${turn.id}`,
    channel: "user",
    ts: formatTime(turn.timestamp),
    text: turn.content
  };
}

/** 答复行：有答案给答案，流式中给占位；既没答案也不在跑则不出行 */
function buildAnswerRow(turn: ChatTurn): TraceRow | null {
  if (turn.result) {
    return { key: `a-${turn.id}`, channel: "answer", ts: "", text: turn.result };
  }
  if (turn.isRunning) {
    return {
      key: `a-pending-${turn.id}`,
      channel: "hint",
      ts: "",
      text: turn.events.length > 0 ? "正在汇总答复…" : "等待响应…",
      streaming: true
    };
  }
  return null;
}

function AnswerZone({ turn }: { turn: ChatTurn }) {
  const answerRow = buildAnswerRow(turn);

  return (
    <div className="agent-answer-zone">
      {answerRow ? <TraceRowItem row={answerRow} showTs={false} /> : null}

      {turn.imageUrls.length > 0 ? (
        <div className="agent-row agent-block-images" data-channel="hint">
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
        <div className="agent-row agent-block-artifacts" data-channel="hint">
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
    </div>
  );
}

/* ══ 轮次卡 ═══════════════════════════════════════════════════════════════ */

function TurnCard({ turn, index }: { turn: ChatTurn; index: number }) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!turn.isRunning) {
      return;
    }
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [turn.isRunning]);

  const promptRow = buildPromptRow(turn);
  const elapsedMs = turnElapsed(turn, now);
  // 流式中不显示总耗时 收尾实测后才亮
  const elapsed = turn.isRunning ? "" : elapsedMs != null ? formatDuration(elapsedMs) : "";

  return (
    <section className="agent-turn" id={`turn-${turn.id}`}>
      <header className="agent-turn-head">
        <span className="agent-turn-no">TURN {index + 1}</span>
        <span className="agent-turn-ts">
          {promptRow.ts}
          {elapsed ? <span className="agent-turn-dur"> · {elapsed}</span> : null}
        </span>
      </header>

      {/* 顺序固定：提问 → 答复区（答案 + 配图 + 产物）→ 过程区（默认折叠） */}
      <TraceRowItem row={promptRow} showTs={false} />
      <AnswerZone turn={turn} />
      <ProcessZone turn={turn} />
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
