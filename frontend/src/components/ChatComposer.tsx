import {
  ArrowUpOutlined,
  CloseOutlined,
  LoadingOutlined,
  PaperClipOutlined,
  PlusOutlined,
  StopOutlined
} from "@ant-design/icons";
import { Upload } from "antd";
import type { UploadFile } from "antd";
import { useEffect, useRef, useState } from "react";
import type { UploadedItem } from "../types";

interface ChatComposerProps {
  isCancelling: boolean;
  isRunning: boolean;
  isUploading: boolean;
  onNewSession: () => void;
  onCancel: () => void;
  onQueryChange: (value: string) => void;
  onSubmit: () => void;
  /** 取消一个还没上传的暂存项 */
  onRemoveStaged: (uid: string) => void;
  /** 移除一个已上传到会话的附件 */
  onRemoveUploaded: (uid: string) => void;
  query: string;
  stagedItems: UploadedItem[];
  uploadedItems: UploadedItem[];
  onStagedItemsChange: (items: UploadedItem[]) => void;
}

function toUploadedItem(file: UploadFile): UploadedItem | null {
  if (!file.originFileObj) {
    return null;
  }

  return {
    uid: file.uid,
    name: file.name,
    size: file.size || 0,
    raw: file.originFileObj
  };
}

function uniqueUploadedItems(items: UploadedItem[]): UploadedItem[] {
  const names = new Set<string>();
  return items.filter((item) => {
    if (names.has(item.name)) {
      return false;
    }
    names.add(item.name);
    return true;
  });
}

export function ChatComposer({
  isCancelling,
  isRunning,
  isUploading,
  onCancel,
  onNewSession,
  onQueryChange,
  onRemoveStaged,
  onRemoveUploaded,
  onStagedItemsChange,
  onSubmit,
  query,
  stagedItems,
  uploadedItems
}: ChatComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const isComposingRef = useRef(false);
  const [value, setValue] = useState(query);
  const hasStagedFiles = stagedItems.length > 0;
  const canSubmit = value.trim().length > 0;

  // 外部（示例问句）写入 → 同步进框并聚焦
  useEffect(() => {
    setValue((previous) => (previous === query ? previous : query));
    if (query) {
      const node = textareaRef.current;
      if (node) {
        node.focus({ preventScroll: true });
      }
    }
  }, [query]);

  /** 随内容长高：上限 160px 与 CSS 的 max-height 一致 */
  useEffect(() => {
    const node = textareaRef.current;
    if (!node) {
      return;
    }
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, 160)}px`;
  }, [value]);

  /** 选中文件只进暂存区，不发起上传；真正的上传在提交任务时统一执行 */
  function handleAttachmentChange(fileList: UploadFile[]) {
    const picked = fileList
      .map(toUploadedItem)
      .filter((item): item is UploadedItem => Boolean(item));

    if (picked.length === 0) {
      return;
    }

    onStagedItemsChange(uniqueUploadedItems([...stagedItems, ...picked]));
  }

  function submit() {
    if (!canSubmit || isRunning || isUploading) {
      return;
    }
    onSubmit();
    setValue("");
  }

  return (
    <div className="agent-composer">
      {uploadedItems.length > 0 || hasStagedFiles ? (
        <div className="agent-composer-files" aria-label="当前会话附件">
          {uploadedItems.map((item) => (
            <span className="agent-file-pill" key={`u-${item.uid}-${item.name}`}>
              <PaperClipOutlined aria-hidden />
              <span>{item.name}</span>
              <button
                aria-label={`移除附件 ${item.name}`}
                className="agent-file-x"
                onClick={() => onRemoveUploaded(item.uid)}
                title="从会话移除"
                type="button"
              >
                <CloseOutlined />
              </button>
            </span>
          ))}
          {stagedItems.map((item) => (
            <span
              className="agent-file-pill agent-file-pill--pending"
              key={`s-${item.uid}`}
            >
              <PaperClipOutlined aria-hidden />
              <span>{item.name}</span>
              <button
                aria-label={`取消上传 ${item.name}`}
                className="agent-file-x"
                onClick={() => onRemoveStaged(item.uid)}
                title="取消上传"
                type="button"
              >
                <CloseOutlined />
              </button>
            </span>
          ))}
          {isUploading ? (
            <span className="agent-file-pill agent-file-pill--pending">附着中…</span>
          ) : null}
        </div>
      ) : null}

      <div className="agent-composer-box">
        <textarea
          aria-label="研搜任务"
          className="agent-composer-input"
          onChange={(event) => {
            setValue(event.target.value);
            onQueryChange(event.target.value);
          }}
          onCompositionEnd={() => {
            isComposingRef.current = false;
          }}
          onCompositionStart={() => {
            isComposingRef.current = true;
          }}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || event.shiftKey) {
              return;
            }
            const nativeEvent = event.nativeEvent as KeyboardEvent;
            if (
              nativeEvent.isComposing ||
              isComposingRef.current ||
              nativeEvent.keyCode === 229
            ) {
              return;
            }
            event.preventDefault();
            submit();
          }}
          placeholder="向 DeepSearch Agents 发送任务..."
          ref={textareaRef}
          rows={1}
          value={value}
        />

        <div className="agent-composer-tools">
          <button
            aria-label="新建会话"
            className="agent-composer-tool"
            onClick={onNewSession}
            title="新建会话"
            type="button"
          >
            <PlusOutlined />
          </button>
          <Upload
            beforeUpload={() => false}
            fileList={[]}
            multiple
            onChange={(info) => {
              handleAttachmentChange(info.fileList.length > 0 ? info.fileList : [info.file]);
            }}
            showUploadList={false}
          >
            <button
              aria-label="选择附件"
              className="agent-composer-tool"
              disabled={isUploading}
              title="选择附件"
              type="button"
            >
              <PaperClipOutlined />
            </button>
          </Upload>
        </div>

        {isRunning ? (
          <button
            aria-busy={isCancelling}
            aria-label={isCancelling ? "正在停止" : "停止生成"}
            className="agent-composer-btn"
            data-stop="true"
            data-stopping={isCancelling ? "true" : undefined}
            disabled={isCancelling}
            onClick={onCancel}
            title={isCancelling ? "正在停止…" : "停止生成"}
            type="button"
          >
            {isCancelling ? <LoadingOutlined /> : <StopOutlined />}
          </button>
        ) : (
          <button
            aria-label="发送"
            className="agent-composer-btn"
            disabled={!canSubmit || isUploading}
            onClick={submit}
            title={isUploading ? "附件上传中…" : "发送（Enter）"}
            type="button"
          >
            <ArrowUpOutlined />
          </button>
        )}
      </div>

      {/* 免责一行在框外：框里不摆第二行是因为没有真控件 这句有真职责 */}
      <p className="agent-composer-note" aria-live="polite">
        {isCancelling
          ? "正在停止并保存已生成内容…"
          : hasStagedFiles
            ? `待上传 ${stagedItems.length} 个附件，发送时一并上传`
            : "内容由 AI 生成，请仔细甄别"}
      </p>
    </div>
  );
}
