/**
 * 知识库管理页（第三视图）
 *
 * 以文档为管理单位（MD 是唯一事实源）：列表 → 详情（正文预览/编辑 + 只读切片）。
 * chunk 级只读，因为三存储均为 delete-by-item_name 语义（auto_id=True，无外部主键）。
 *
 * 版式走 Ragent 管理侧语言：admin-page-header + admin-stat-grid + ui-card 包表格。
 */
import {
  ClockCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  EyeOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  ReloadOutlined,
  SyncOutlined
} from "@ant-design/icons";
import {
  App as AntApp,
  Button,
  Empty,
  Input,
  Popconfirm,
  Table,
  Tooltip
} from "antd";
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

const STATUS_DOT: Record<string, string> = {
  active: "admin-dot--ok",
  inactive: "admin-dot--pending",
  processing: "admin-dot--running",
  reindexing: "admin-dot--running",
  failed: "admin-dot--failed",
  deleted: "admin-dot--pending"
};

function formatTime(value: number): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "—";
  }
  return date.toLocaleString("zh-CN", {
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

export function KbManagerPage() {
  const { message } = AntApp.useApp();
  const kb = useKbDocs();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [keyword, setKeyword] = useState("");

  // 从列表实时派生：重建/启停后 status 与「已编辑未重建」标记自动刷新
  const selected = kb.docs.find((doc) => doc.doc_id === selectedId) ?? null;

  const trimmed = keyword.trim().toLowerCase();
  const shown = trimmed
    ? kb.docs.filter(
        (doc) =>
          doc.file_title.toLowerCase().includes(trimmed) ||
          doc.item_name.toLowerCase().includes(trimmed)
      )
    : kb.docs;

  function openDoc(doc: KbDocument) {
    setSelectedId(doc.doc_id);
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
        <div className="admin-doc-cell">
          <span className="admin-doc-icon" aria-hidden>
            <FileTextOutlined />
          </span>
          <span className="admin-doc-copy">
            <button
              className="admin-doc-name"
              onClick={() => openDoc(doc)}
              title={doc.file_title}
              type="button"
            >
              {doc.file_title}
            </button>
            <span className="admin-doc-sub" title={doc.item_name}>
              {doc.item_name || "—"} · {doc.chunk_count} 片
            </span>
          </span>
          {doc.edited_not_reindexed ? <span className="admin-chip">已编辑</span> : null}
        </div>
      )
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 140,
      render: (status: KbDocStatus) => (
        <span className="admin-status-cell">
          <span className={`admin-dot ${STATUS_DOT[status] ?? ""}`} aria-hidden />
          {STATUS_LABEL[status] ?? status}
        </span>
      )
    },
    {
      title: "启用",
      key: "enabled",
      width: 80,
      render: (_value, doc) => {
        const flying = KB_DOC_BUSY_STATUSES.includes(doc.status);
        const on = doc.status === "active";
        return (
          <Tooltip title={flying ? "处理中，暂不可启停" : on ? "点击停用" : "点击启用"}>
            <button
              aria-checked={on}
              aria-label={on ? "停用" : "启用"}
              className="admin-switch"
              data-on={on}
              disabled={flying}
              onClick={() => handleToggle(doc)}
              role="switch"
              type="button"
            >
              <i />
            </button>
          </Tooltip>
        );
      }
    },
    {
      title: "更新时间",
      dataIndex: "ts",
      key: "ts",
      width: 170,
      render: (value: number) => (
        <span style={{ fontVariantNumeric: "tabular-nums", color: "#64748b" }}>
          {formatTime(value)}
        </span>
      )
    },
    {
      title: "操作",
      key: "actions",
      width: 190,
      render: (_value, doc) => {
        const flying = KB_DOC_BUSY_STATUSES.includes(doc.status);
        return (
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Button icon={<EyeOutlined />} onClick={() => openDoc(doc)} size="small">
              详情
            </Button>
            <Button
              className="admin-primary-gradient"
              disabled={flying}
              icon={<ReloadOutlined />}
              onClick={() => handleReindex(doc)}
              size="small"
            >
              重建
            </Button>
            <Popconfirm
              cancelText="取消"
              description="软删除：清三存储并标记 deleted，MD 与登记保留（可逆）"
              disabled={flying}
              okText="删除"
              onConfirm={() => handleDelete(doc)}
              title="确认删除该文档？"
            >
              <Button danger disabled={flying} icon={<DeleteOutlined />} size="small" />
            </Popconfirm>
          </div>
        );
      }
    }
  ];

  const activeCount = kb.docs.filter((doc) => doc.status === "active").length;
  const pendingCount = kb.docs.filter((doc) => doc.edited_not_reindexed).length;
  const busyCount = kb.docs.filter((doc) => KB_DOC_BUSY_STATUSES.includes(doc.status)).length;

  return (
    <div className="admin-page">
      <header className="admin-page-header">
        <div>
          <h2 className="admin-page-title">知识库管理</h2>
          <p className="admin-page-subtitle">
            文档级管理 · MD 为唯一事实源 · 三存储 delete-by-item_name 重建
          </p>
        </div>
        <div className="admin-page-actions">
          <Input
            allowClear
            onChange={(event) => setKeyword(event.target.value)}
            placeholder="搜索文档名 / item_name"
            style={{ width: 220 }}
            value={keyword}
          />
          <Button icon={<SyncOutlined />} onClick={handleSync}>
            存量迁移
          </Button>
        </div>
      </header>

      <div className="admin-stat-grid" aria-label="知识库统计">
        <div className="admin-stat-card">
          <div className="admin-stat-value-wrap">
            <div className="admin-stat-label">文档总数</div>
            <div className="admin-stat-value">{kb.total}</div>
          </div>
          <span className="admin-stat-icon" aria-hidden>
            <FolderOpenOutlined />
          </span>
        </div>
        <div className="admin-stat-card">
          <div className="admin-stat-value-wrap">
            <div className="admin-stat-label">已启用</div>
            <div className="admin-stat-value">{activeCount}</div>
          </div>
          <span className="admin-stat-icon admin-stat-icon--ok" aria-hidden>
            <FileTextOutlined />
          </span>
        </div>
        <div className="admin-stat-card">
          <div className="admin-stat-value-wrap">
            <div className="admin-stat-label">待重建</div>
            <div className="admin-stat-value">{pendingCount}</div>
          </div>
          <span className="admin-stat-icon admin-stat-icon--warn" aria-hidden>
            <EditOutlined />
          </span>
        </div>
        <div className="admin-stat-card">
          <div className="admin-stat-value-wrap">
            <div className="admin-stat-label">处理中</div>
            <div className="admin-stat-value">{busyCount}</div>
          </div>
          <span className="admin-stat-icon" aria-hidden>
            <ClockCircleOutlined />
          </span>
        </div>
      </div>

      <div className="ui-card">
        <div className="admin-table-head">
          <div>
            <h3 className="ui-card-title">文档列表</h3>
            <p className="ui-card-description">
              共 {kb.docs.length} 条{trimmed ? ` · 匹配 ${shown.length} 条` : ""}
            </p>
          </div>
        </div>
        <div className="ui-card-body" style={{ paddingTop: 0 }}>
          {kb.docs.length === 0 && !kb.isLoading ? (
            <Empty
              description={
                <span>
                  暂无知识库文档。可先在「知识导入」上传 PDF，或点击右上角「存量迁移」登记已有 MD。
                  <br />
                  提示：存量迁移只登记，不重建向量。
                </span>
              }
            />
          ) : (
            <div className="admin-table-wrap">
              <Table
                columns={columns}
                dataSource={shown}
                loading={kb.isLoading}
                pagination={{ pageSize: 10, size: "small", showSizeChanger: false }}
                rowKey="doc_id"
              />
            </div>
          )}
        </div>
      </div>

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
