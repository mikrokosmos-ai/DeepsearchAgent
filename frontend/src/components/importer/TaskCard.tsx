/**
 * 导入任务卡
 *
 * 单条任务的可视化：文件名 / 大小 / 状态徽标 / 进度条 / 节点进度轨 / 错误信息。
 * 进行中的任务额外提供「取消」入口（后端为协作式取消，见 app/core/cancel.py）。
 */
import { CloseOutlined, FileTextOutlined, StopOutlined } from "@ant-design/icons";
import { Alert, Button, Progress } from "antd";

import { formatFileSize } from "../../lib/nodes";
import type { KbImportTaskItem } from "../../hooks/useKbImport";
import type { KbTaskPhase } from "../../types";
import { ImportStepRail } from "./ImportStepRail";

const PHASE_LABEL: Record<KbTaskPhase, string> = {
  uploading: "上传中",
  processing: "解析中",
  completed: "已完成",
  failed: "失败"
};

const PROGRESS_STATUS: Record<KbTaskPhase, "active" | "success" | "exception"> = {
  uploading: "active",
  processing: "active",
  completed: "success",
  failed: "exception"
};

interface TaskCardProps {
  task: KbImportTaskItem;
  onRemove: (fileId: string) => void;
  onCancel: (fileId: string) => void;
}

export function TaskCard({ task, onRemove, onCancel }: TaskCardProps) {
  // 只有已经拿到 task_id 的进行中任务才可取消（上传阶段后端还没登记任务）
  const canCancel = task.phase === "processing" && Boolean(task.task_id);

  return (
    <article className={`console-panel task-card task-card--${task.phase}`}>
      <header className="task-card-head">
        <div className="task-card-title">
          <FileTextOutlined aria-hidden />
          <strong>{task.file_name}</strong>
          <span className="task-card-size">{formatFileSize(task.file_size)}</span>
        </div>

        <div className="task-card-actions">
          <span className={`task-badge task-badge--${task.phase}`}>
            {PHASE_LABEL[task.phase]}
          </span>
          {canCancel ? (
            <Button
              aria-label={`取消 ${task.file_name}`}
              icon={<StopOutlined />}
              onClick={() => onCancel(task.fileId)}
              size="small"
              type="text"
            >
              取消
            </Button>
          ) : null}
          <Button
            aria-label={`移除 ${task.file_name}`}
            icon={<CloseOutlined />}
            onClick={() => onRemove(task.fileId)}
            size="small"
            type="text"
          />
        </div>
      </header>

      <Progress
        percent={task.progress}
        showInfo
        size="small"
        status={PROGRESS_STATUS[task.phase]}
      />

      {task.error ? (
        <Alert className="task-card-error" message={task.error} showIcon type="error" />
      ) : null}

      <ImportStepRail doneList={task.done_list} runningList={task.running_list} />
    </article>
  );
}
