/**
 * 入库链路的节点元数据与进度换算
 *
 * 键名契约（重要）：`IMPORT_STEPS[].key` 必须与后端
 * `app/utils/task_utils.py::_NODE_NAME_TO_CN` 的中文展示名**逐字一致** ——
 * 前端拿到的 `done_list` / `running_list` 已经是中文名，名称对不上就会退化成
 * 「未知节点」而无法点亮进度轨。新增 LangGraph 节点时需同步本文件。
 *
 * 步骤顺序与 `app/pipelines/import_pipeline/graph.py` 一致：
 *   检查文件 → PDF转Markdown → Markdown图片处理 → 文档切分
 *          → 主体名称识别 → 向量生成 → 导入向量库 → 导入知识图谱 → 处理完成
 * （`开始上传文件` 是前端本地阶段，后端受理后即视为完成。）
 */

export interface ImportStep {
  key: string;
  label: string;
  /** 权重之和为 100，用于把 done_list 换算成进度百分比 */
  weight: number;
}

export const IMPORT_STEPS: ImportStep[] = [
  { key: "开始上传文件", label: "开始上传文件", weight: 5 },
  { key: "检查文件", label: "检查文件", weight: 5 },
  { key: "PDF转Markdown", label: "PDF转Markdown", weight: 22 },
  { key: "Markdown图片处理", label: "Markdown图片处理", weight: 25 },
  { key: "文档切分", label: "文档切分", weight: 10 },
  { key: "主体名称识别", label: "主体名称识别", weight: 5 },
  { key: "向量生成", label: "向量生成", weight: 10 },
  { key: "导入向量库", label: "导入向量库", weight: 8 },
  { key: "导入知识图谱", label: "导入知识图谱", weight: 8 },
  { key: "处理完成", label: "处理完成", weight: 2 }
];

/**
 * 把已完成节点列表换算成进度百分比（0-100）
 *
 * Markdown 输入不会经过 `PDF转Markdown`，因此该步权重会计入分母但不计入分子；
 * 进度由「处理完成」兜底到 100，不会长时间卡在中间值。
 */
export function importProgress(doneList: string[]): number {
  if (!doneList || doneList.length === 0) {
    return 0;
  }
  if (doneList.includes("处理完成")) {
    return 100;
  }
  const done = new Set(doneList);
  let total = 0;
  let accumulated = 0;
  for (const step of IMPORT_STEPS) {
    total += step.weight;
    if (done.has(step.key)) {
      accumulated += step.weight;
    }
  }
  return total > 0 ? Math.min(99, Math.round((accumulated / total) * 100)) : 0;
}

/** 文件大小人性化展示（供任务卡使用） */
export function formatFileSize(bytes: number): string {
  if (!bytes || bytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  const value = bytes / Math.pow(1024, index);
  return `${value >= 100 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
}
