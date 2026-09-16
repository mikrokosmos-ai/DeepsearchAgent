/**
 * 入库节点进度轨
 *
 * 展示 10 个流水线步骤的「已完成 / 进行中 / 待执行」三态。
 * 节点名与后端 `_NODE_NAME_TO_CN` 逐字对应（见 lib/nodes.ts 的契约说明）。
 */
import {
  CheckCircleFilled,
  LoadingOutlined,
  MinusCircleOutlined
} from "@ant-design/icons";

import { IMPORT_STEPS } from "../../lib/nodes";

interface ImportStepRailProps {
  doneList: string[];
  runningList: string[];
}

export function ImportStepRail({ doneList, runningList }: ImportStepRailProps) {
  const done = new Set(doneList || []);
  const running = new Set(runningList || []);

  return (
    <ol className="import-step-rail" aria-label="入库节点进度">
      {IMPORT_STEPS.map((step) => {
        const isDone = done.has(step.key);
        const isRunning = !isDone && running.has(step.key);
        const state = isDone ? "done" : isRunning ? "running" : "pending";
        return (
          <li key={step.key} className={`import-step import-step--${state}`}>
            <span className="import-step-icon" aria-hidden>
              {isDone ? (
                <CheckCircleFilled />
              ) : isRunning ? (
                <LoadingOutlined />
              ) : (
                <MinusCircleOutlined />
              )}
            </span>
            <span className="import-step-label">{step.label}</span>
          </li>
        );
      })}
    </ol>
  );
}
