/**
 * 知识导入页
 *
 * 上传 PDF / Markdown → 观察 10 个入库节点的实时进度 → 完成后可切到对话页提问。
 * 进度既可通过 `useKbImport` 轮询获得，也会收到 WebSocket 的 `kb_progress` 事件。
 *
 * 版式走 Ragent 管理侧语言：admin-page-header + admin-stat-grid + 任务卡列表。
 */
import {
  BarChartOutlined,
  FolderOpenOutlined,
  ReloadOutlined,
  SyncOutlined,
  WarningOutlined
} from "@ant-design/icons";
import { Alert, App as AntApp, Button, Empty } from "antd";

import { useKbImport } from "../../hooks/useKbImport";
import { TaskCard } from "./TaskCard";
import { UploadDropzone } from "./UploadDropzone";

interface ImportPageProps {
  /** 当前会话 ID：导入进度事件按它推送到对应 WebSocket */
  threadId: string;
}

export function ImportPage({ threadId }: ImportPageProps) {
  const { message } = AntApp.useApp();
  const { tasks, isUploading, lastError, uploadFiles, removeTask, cancelTask, refreshFromServer } =
    useKbImport(threadId);

  const runningCount = tasks.filter(
    (task) => task.phase === "uploading" || task.phase === "processing"
  ).length;
  const completedCount = tasks.filter((task) => task.phase === "completed").length;
  const failedCount = tasks.filter((task) => task.phase === "failed").length;

  async function handleFiles(files: File[]) {
    try {
      const outcome = await uploadFiles(files);
      if (outcome.accepted > 0) {
        message.success(`已提交 ${outcome.accepted} 个导入任务`);
      }
      outcome.rejected.forEach((item) => message.warning(`${item.name}：${item.reason}`));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "上传失败");
    }
  }

  function handleRefresh() {
    refreshFromServer().catch(() => undefined);
  }

  async function handleCancel(fileId: string) {
    await cancelTask(fileId);
    message.info("已请求取消，最多等当前节点执行完毕后停止");
  }

  async function handleRemove(fileId: string) {
    try {
      await removeTask(fileId);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "移除任务失败，已保留该任务");
    }
  }

  return (
    <div className="admin-page">
      <header className="admin-page-header">
        <div>
          <h2 className="admin-page-title">知识导入</h2>
          <p className="admin-page-subtitle">
            MinerU 解析 · BGE-M3 向量化 · Milvus 入库 · Neo4j 知识图谱
          </p>
        </div>
        <div className="admin-page-actions">
          <Button className="admin-primary-gradient" icon={<SyncOutlined />} onClick={handleRefresh}>
            刷新进度
          </Button>
        </div>
      </header>

      {lastError ? <Alert message={lastError} showIcon type="error" /> : null}

      <div className="admin-stat-grid" aria-label="导入任务统计">
        <div className="admin-stat-card">
          <div className="admin-stat-value-wrap">
            <div className="admin-stat-label">任务总数</div>
            <div className="admin-stat-value">{tasks.length}</div>
          </div>
          <span className="admin-stat-icon" aria-hidden="true">
            <BarChartOutlined />
          </span>
        </div>
        <div className="admin-stat-card">
          <div className="admin-stat-value-wrap">
            <div className="admin-stat-label">进行中</div>
            <div className="admin-stat-value">{runningCount}</div>
          </div>
          <span className="admin-stat-icon" aria-hidden="true">
            <ReloadOutlined />
          </span>
        </div>
        <div className="admin-stat-card">
          <div className="admin-stat-value-wrap">
            <div className="admin-stat-label">已完成</div>
            <div className="admin-stat-value">{completedCount}</div>
          </div>
          <span className="admin-stat-icon admin-stat-icon--ok" aria-hidden="true">
            <FolderOpenOutlined />
          </span>
        </div>
        <div className="admin-stat-card">
          <div className="admin-stat-value-wrap">
            <div className="admin-stat-label">失败</div>
            <div className="admin-stat-value">{failedCount}</div>
          </div>
          <span className="admin-stat-icon admin-stat-icon--error" aria-hidden="true">
            <WarningOutlined />
          </span>
        </div>
      </div>

      <UploadDropzone disabled={isUploading} onFiles={handleFiles} />

      <div className="ui-card">
        <div className="admin-table-head">
          <div>
            <h3 className="ui-card-title">导入任务</h3>
            <p className="ui-card-description">上传后可见 10 个入库节点的实时进度</p>
          </div>
        </div>
        <div className="ui-card-body" style={{ paddingTop: 0 }}>
          {tasks.length === 0 ? (
            <Empty description="暂无导入任务，上传 PDF / Markdown 即可开始" />
          ) : (
            <div className="admin-task-list">
              {tasks.map((task) => (
                <TaskCard
                  key={task.fileId}
                  onCancel={handleCancel}
                  onRemove={handleRemove}
                  task={task}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
