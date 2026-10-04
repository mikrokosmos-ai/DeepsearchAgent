/**
 * 知识库管理页（第三视图）
 *
 * 以文档为管理单位（MD 是唯一事实源）：列表 → 详情（正文预览/编辑 + 只读切片）。
 * chunk 级只读，因为三存储均为 delete-by-item_name 语义（auto_id=True，无外部主键）。
 */
import {
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  SyncOutlined
} from "@ant-design/icons";
import { Alert, App as AntApp, Button, Empty, Popconfirm, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useState } from "react";

import { KbDocDrawer } from "./KbDocDrawer";
import { useKbDocs } from "../../hooks/useKbDocs";
import type { KbDocStatus, KbDocument } from "../../types";
import { KB_DOC_BUSY_STATUSES } from "../../types";

const STATUS_LABEL: Record<string, string> = {
  active: "已启用",
  inactive: "已停用",
  processing: "处理中",
  reindexing: "重建中",
  failed: "失败",
  deleted: "已删除"
};

const STATUS_CLASS: Record<string, string> = {
  active: "kb-status--active",
  inactive: "kb-status--inactive",
  processing: "kb-status--busy",
  reindexing: "kb-status--busy",
  failed: "kb-status--failed",
  deleted: "kb-status--deleted"
};

export function KbManagerPage() {
  const { message } = AntApp.useApp();
  const kb = useKbDocs();
  const [selected, setSelected] = useState<KbDocument | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  function openDoc(doc: KbDocument) {
    setSelected(doc);
    setDrawerOpen(true);
  }

  async function handleSync() {
    try {
      const result = await kb.sync();
      message.success(`存量迁移完成：新增 ${result.registered}，待重建 ${result.inactive}`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "存量迁移失败");
    }
  }

  async function handleToggle(doc: KbDocument) {
    const enable = doc.status !== "active";
    try {
      await kb.setEnabled(doc.doc_id, enable);
      message.success(enable ? "已启用（重建中）" : "已停用");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "操作失败");
    }
  }

  async function handleDelete(doc: KbDocument) {
    try {
      await kb.remove(doc.doc_id);
      message.success("已删除（软删，保留 MD）");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除失败");
    }
  }

  async function handleReindex(doc: KbDocument) {
    try {
      await kb.reindex(doc.doc_id);
      message.success("已触发重建");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "重建失败");
    }
  }

  const columns: ColumnsType<KbDocument> = [
    {
      title: "文档",
      dataIndex: "file_title",
      key: "file_title",
      render: (_value, doc) => (
        <button className="kb-title-button" onClick={() => openDoc(doc)} type="button">
          {doc.file_title}
        </button>
      )
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 130,
      render: (status: KbDocStatus, doc) => (
        <div className="kb-status-cell">
          <span className={`kb-status-dot ${STATUS_CLASS[status] ?? ""}`} aria-hidden />
          <span>{STATUS_LABEL[status] ?? status}</span>
          {doc.edited_not_reindexed ? (
            <Tag className="kb-tag-warn">待重建</Tag>
          ) : null}
        </div>
      )
    },
    {
      title: "切片",
      dataIndex: "chunk_count",
      key: "chunk_count",
      width: 80
    },
    {
      title: "item_name",
      dataIndex: "item_name",
      key: "item_name",
      ellipsis: true,
      render: (value: string) => <code className="kb-code">{value || "—"}</code>
    },
    {
      title: "操作",
      key: "actions",
      width: 220,
      render: (_value, doc) => {
        const flying = KB_DOC_BUSY_STATUSES.includes(doc.status);
        return (
          <div className="kb-row-actions">
            <Tooltip title="查看详情">
              <Button icon={<EyeOutlined />} onClick={() => openDoc(doc)} size="small" type="text" />
            </Tooltip>
            <Tooltip title={flying ? "处理中，暂不可重建" : "重建三存储"}>
              <Button
                disabled={flying}
                icon={<ReloadOutlined />}
                onClick={() => handleReindex(doc)}
                size="small"
                type="text"
              />
            </Tooltip>
            <Tooltip title={flying ? "处理中，暂不可启停" : doc.status === "active" ? "停用" : "启用"}>
              <Button
                disabled={flying}
                icon={<PoweroffOutlined />}
                onClick={() => handleToggle(doc)}
                size="small"
                type="text"
              />
            </Tooltip>
            <Popconfirm
              cancelText="取消"
              description="软删除：清三存储并标记 deleted，MD 与登记保留（可逆）"
              disabled={flying}
              okText="删除"
              onConfirm={() => handleDelete(doc)}
              title="确认删除该文档？"
            >
              <Tooltip title={flying ? "处理中，暂不可删除" : "删除"}>
                <Button danger disabled={flying} icon={<DeleteOutlined />} size="small" type="text" />
              </Tooltip>
            </Popconfirm>
          </div>
        );
      }
    }
  ];

  return (
    <div className="kb-manager-page">
      <header className="import-page-head">
        <div>
          <span className="panel-kicker">KNOWLEDGE BASE</span>
          <h2>知识库管理</h2>
          <p>文档级管理 · MD 为唯一事实源 · 三存储 delete-by-item_name 重建</p>
        </div>
        <Button icon={<SyncOutlined />} onClick={handleSync}>
          存量迁移
        </Button>
      </header>

      {kb.lastError ? (
        <Alert className="chat-alert" message={kb.lastError} showIcon type="error" />
      ) : null}

      <div className="import-summary" aria-label="知识库统计">
        <div className="import-stat">
          <span>文档总数</span>
          <strong>{kb.total}</strong>
        </div>
        <div className="import-stat">
          <span>已启用</span>
          <strong>{kb.docs.filter((doc) => doc.status === "active").length}</strong>
        </div>
        <div className="import-stat">
          <span>待重建</span>
          <strong>{kb.docs.filter((doc) => doc.edited_not_reindexed).length}</strong>
        </div>
        <div className="import-stat">
          <span>处理中</span>
          <strong>{kb.docs.filter((doc) => KB_DOC_BUSY_STATUSES.includes(doc.status)).length}</strong>
        </div>
      </div>

      {kb.docs.length === 0 && !kb.isLoading ? (
        <Empty
          className="import-empty"
          description={
            <span>
              暂无知识库文档。可先在「知识导入」上传 PDF，或点击右上角「存量迁移」登记已有 MD。
              <br />
              <EditOutlined /> 提示：存量迁移只登记，不重建向量。
            </span>
          }
        />
      ) : (
        <Table
          className="kb-doc-table"
          columns={columns}
          dataSource={kb.docs}
          loading={kb.isLoading}
          pagination={false}
          rowKey="doc_id"
          size="small"
        />
      )}

      <KbDocDrawer
        doc={selected}
        onClose={() => setDrawerOpen(false)}
        onLoadChunks={kb.loadChunks}
        onLoadContent={kb.loadContent}
        onReindex={kb.reindex}
        onSave={kb.saveContent}
        open={drawerOpen}
      />
    </div>
  );
}
