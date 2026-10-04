/**
 * 文档详情抽屉
 *
 * 两个页签：
 *  - 正文：MD 预览 / 编辑（保存、保存并重建）
 *  - 切片：只读浏览 Milvus 中的 chunk（不可改，chunk 级 upsert 不可达）
 *
 * 飞行中状态（processing / reindexing）下所有写操作禁用，与后端每 doc 单飞一致。
 */
import {
  AlertOutlined,
  EditOutlined,
  EyeOutlined,
  ReloadOutlined,
  SaveOutlined,
  ThunderboltOutlined
} from "@ant-design/icons";
import { App as AntApp, Button, Drawer, Empty, Segmented, Space, Tag, Tooltip } from "antd";
import { useCallback, useEffect, useState } from "react";

import { MarkdownRenderer } from "../MarkdownRenderer";
import type { KbChunk, KbDocument } from "../../types";
import { KB_DOC_BUSY_STATUSES } from "../../types";

interface KbDocDrawerProps {
  doc: KbDocument | null;
  open: boolean;
  onClose: () => void;
  onLoadContent: (docId: string) => Promise<{ content: string; edited: boolean } | null>;
  onLoadChunks: (docId: string) => Promise<KbChunk[]>;
  onSave: (docId: string, content: string, reindex: boolean) => Promise<void>;
  onReindex: (docId: string) => Promise<void>;
}

type TabKey = "content" | "chunks";

export function KbDocDrawer({
  doc,
  open,
  onClose,
  onLoadContent,
  onLoadChunks,
  onSave,
  onReindex
}: KbDocDrawerProps) {
  const { message } = AntApp.useApp();
  const [tab, setTab] = useState<TabKey>("content");
  const [content, setContent] = useState("");
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const [edited, setEdited] = useState(false);
  const [chunks, setChunks] = useState<KbChunk[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const docId = doc?.doc_id ?? "";
  const isFlying = doc ? KB_DOC_BUSY_STATUSES.includes(doc.status) : false;

  const loadContent = useCallback(async () => {
    if (!docId) {
      return;
    }
    setLoading(true);
    try {
      const result = await onLoadContent(docId);
      setContent(result?.content ?? "");
      setDraft(result?.content ?? "");
      setEdited(Boolean(result?.edited));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "读取正文失败");
    } finally {
      setLoading(false);
    }
  }, [docId, message, onLoadContent]);

  const loadChunks = useCallback(async () => {
    if (!docId) {
      return;
    }
    setLoading(true);
    try {
      setChunks(await onLoadChunks(docId));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "读取切片失败");
    } finally {
      setLoading(false);
    }
  }, [docId, message, onLoadChunks]);

  useEffect(() => {
    if (!open || !docId) {
      return;
    }
    setTab("content");
    setEditing(false);
    setChunks([]);
    void loadContent();
  }, [open, docId, loadContent]);

  useEffect(() => {
    if (open && tab === "chunks" && docId) {
      void loadChunks();
    }
  }, [open, tab, docId, loadChunks]);

  async function handleSave(reindex: boolean) {
    if (!docId || busy) {
      return;
    }
    setBusy(true);
    try {
      await onSave(docId, draft, reindex);
      setContent(draft);
      setEdited(false);
      setEditing(false);
      message.success(reindex ? "已保存并触发重建" : "已保存");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function handleReindex() {
    if (!docId || busy) {
      return;
    }
    setBusy(true);
    try {
      await onReindex(docId);
      message.success("已触发重建");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "重建失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Drawer
      className="kb-drawer"
      destroyOnClose
      onClose={onClose}
      open={open}
      title={doc ? doc.file_title : "文档详情"}
      width={720}
    >
      {doc ? (
        <div className="kb-drawer-body">
          <div className="kb-drawer-meta">
            <Tag className="kb-status-tag">{doc.status}</Tag>
            <span className="kb-meta-item">item_name：{doc.item_name || "—"}</span>
            <span className="kb-meta-item">切片数：{doc.chunk_count}</span>
            {edited ? (
              <Tag className="kb-tag-warn" icon={<AlertOutlined />}>
                已编辑未重建
              </Tag>
            ) : null}
            {isFlying ? <Tag className="kb-tag-info">处理中，暂不可编辑</Tag> : null}
          </div>

          <Segmented
            className="kb-drawer-tabs"
            onChange={(value) => setTab(value as TabKey)}
            options={[
              { label: "正文", value: "content", icon: <EyeOutlined /> },
              { label: "切片", value: "chunks", icon: <ThunderboltOutlined /> }
            ]}
            value={tab}
          />

          {tab === "content" ? (
            <div className="kb-content-pane">
              <div className="kb-content-toolbar">
                <Space>
                  <Button
                    disabled={isFlying || loading}
                    icon={editing ? <EyeOutlined /> : <EditOutlined />}
                    onClick={() => setEditing((value) => !value)}
                    size="small"
                  >
                    {editing ? "预览" : "编辑"}
                  </Button>
                  <Tooltip title={isFlying ? "处理中，暂不可保存" : ""}>
                    <Button
                      disabled={!editing || isFlying || busy}
                      icon={<SaveOutlined />}
                      onClick={() => handleSave(false)}
                      size="small"
                      type="primary"
                    >
                      保存
                    </Button>
                  </Tooltip>
                  <Tooltip title={isFlying ? "处理中，暂不可重建" : ""}>
                    <Button
                      disabled={!editing || isFlying || busy}
                      icon={<ReloadOutlined />}
                      onClick={() => handleSave(true)}
                      size="small"
                    >
                      保存并重建
                    </Button>
                  </Tooltip>
                  <Button
                    danger
                    disabled={isFlying || busy}
                    icon={<ReloadOutlined />}
                    onClick={handleReindex}
                    size="small"
                  >
                    重建
                  </Button>
                </Space>
              </div>

              {editing ? (
                <textarea
                  className="kb-md-editor"
                  onChange={(event) => setDraft(event.target.value)}
                  spellCheck={false}
                  value={draft}
                />
              ) : (
                <div className="kb-md-preview">
                  {content ? (
                    <MarkdownRenderer content={content} />
                  ) : (
                    <Empty description="正文为空或不可读" />
                  )}
                </div>
              )}
            </div>
          ) : (
            <div className="kb-chunks-pane">
              {chunks.length === 0 ? (
                <Empty description="暂无切片（可能未入库或已停用）" />
              ) : (
                <ol className="kb-chunk-list">
                  {chunks.map((chunk, index) => (
                    <li className="kb-chunk-item" key={chunk.chunk_id ?? index}>
                      <div className="kb-chunk-head">
                        <span className="kb-chunk-index">#{index + 1}</span>
                        <span className="kb-chunk-title">{chunk.title || "（无标题）"}</span>
                        {chunk.part ? <span className="kb-chunk-part">{chunk.part}</span> : null}
                      </div>
                      <p className="kb-chunk-text">{chunk.content || ""}</p>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          )}
        </div>
      ) : null}
    </Drawer>
  );
}
