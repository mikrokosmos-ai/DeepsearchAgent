/**
 * 知识导入页
 *
 * 上传 PDF / Markdown → 观察 10 个入库节点的实时进度 → 完成后可切到对话页提问。
 * 进度既可通过 `useKbImport` 轮询获得，也会收到 WebSocket 的 `kb_progress` 事件。
 */
import { ReloadOutlined } from "@ant-design/icons";
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
  const { tasks, isUploading, lastError, uploadFiles, removeTask, refreshFromServer } =
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

  return (
    <div className="import-page">
      <header className="import-page-head">
        <div>
          <span className="panel-kicker">KNOWLEDGE INGESTION</span>
          <h2>知识导入</h2>
          <p>MinerU 解析 · BGE-M3 向量化 · Milvus 入库 · Neo4j 知识图谱</p>
        </div>
        <Button icon={<ReloadOutlined />} onClick={handleRefresh}>
          刷新
        </Button>
      </header>

      {lastError ? (
        <Alert className="chat-alert" message={lastError} showIcon type="error" />
      ) : null}

      <UploadDropzone disabled={isUploading} onFiles={handleFiles} />

      <div className="import-summary" aria-label="导入任务统计">
        <div className="import-stat">
          <span>任务总数</span>
          <strong>{tasks.length}</strong>
        </div>
        <div className="import-stat">
          <span>进行中</span>
          <strong>{runningCount}</strong>
        </div>
        <div className="import-stat">
          <span>已完成</span>
          <strong>{completedCount}</strong>
        </div>
        <div className={failedCount > 0 ? "import-stat import-stat--error" : "import-stat"}>
          <span>失败</span>
          <strong>{failedCount}</strong>
        </div>
      </div>

      {tasks.length === 0 ? (
        <Empty
          className="import-empty"
          description="暂无导入任务，上传 PDF / Markdown 即可开始"
        />
      ) : (
        <div className="import-task-list">
          {tasks.map((task) => (
            <TaskCard key={task.fileId} onRemove={removeTask} task={task} />
          ))}
        </div>
      )}
    </div>
  );
}
