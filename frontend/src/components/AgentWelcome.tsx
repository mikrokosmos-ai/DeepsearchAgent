import {
  BranchesOutlined,
  CloudServerOutlined,
  DatabaseOutlined,
  FileMarkdownOutlined,
  FileSearchOutlined,
  FileTextOutlined
} from "@ant-design/icons";
import type { ReactNode } from "react";

interface CaseItem {
  tool: string;
  title: string;
  prompt: string;
  icon: ReactNode;
}

/** 案例：工具名 + 一句话场景 + 可点的完整问句 */
const CASES: CaseItem[] = [
  {
    tool: "网络搜索",
    title: "检索行业公开信息",
    prompt:
      "请使用网络搜索工具，检索 2026 年跨境电商 AI 客服趋势，列出 5 条关键变化，并附上来源链接。",
    icon: <CloudServerOutlined aria-hidden />
  },
  {
    tool: "数据库查询",
    title: "按条件筛选业务数据",
    prompt:
      "请使用数据库查询工具，查询库存大于 100 的药品，按库存量升序列出药品名称、批次号、仓库位置和过期日期。",
    icon: <DatabaseOutlined aria-hidden />
  },
  {
    tool: "本地知识库",
    title: "问答内部沉淀文档",
    prompt:
      "请使用内部知识库助手，查询公司内部白皮书中关于品类策略的内容，并整理成三条可执行建议。",
    icon: <FileSearchOutlined aria-hidden />
  },
  {
    tool: "文件读取",
    title: "提炼上传文件要点",
    prompt:
      "请使用文件读取工具，读取我上传的文件，提炼核心观点、风险点和待补充信息，并给出下一步分析计划。",
    icon: <FileTextOutlined aria-hidden />
  },
  {
    tool: "文档生成",
    title: "产出 Markdown / PDF",
    prompt:
      "请使用 Markdown 文档生成工具和 Markdown 转 PDF 工具，基于本次调研结果生成一份 Markdown 报告，并转换成 PDF 保存到当前工作目录。",
    icon: <FileMarkdownOutlined aria-hidden />
  },
  {
    tool: "多路协同",
    title: "交叉验证多来源",
    prompt:
      "请同时使用网络搜索工具和本地知识库助手，对比「品类策略」在公开资料与内部白皮书中的说法差异，列出冲突点并给出结论。",
    icon: <BranchesOutlined aria-hidden />
  }
];

interface AgentWelcomeProps {
  /** 输入条由外层注入：空态时它要落在标题与案例之间 */
  composer: ReactNode;
  onUseCase: (prompt: string) => void;
}

/**
 * 待机首屏：正中央标题 → 对话框 → 案例，三段垂直居中。
 * 案例点击只填进输入框，发不发由用户决定。
 */
export function AgentWelcome({ composer, onUseCase }: AgentWelcomeProps) {
  return (
    <div className="agent-welcome">
      <header className="agent-welcome-head">
        <h1 className="agent-welcome-title">今天想研搜什么？</h1>
        <p className="agent-welcome-sub">
          网络搜索 · 数据库查询 · 本地知识库 · 文档生成，四路能力由主智能体统一调度
        </p>
      </header>

      {composer}

      <section className="agent-welcome-cases">
        <span className="agent-welcome-cases-label">试试这些</span>
        <div className="agent-welcome-grid">
          {CASES.map((item) => (
            <button
              className="agent-case"
              key={item.tool}
              onClick={() => onUseCase(item.prompt)}
              title={item.prompt}
              type="button"
            >
              <span className="agent-case-head">
                <span className="agent-case-icon">{item.icon}</span>
                <span className="agent-case-tool">{item.tool}</span>
              </span>
              <span className="agent-case-title">{item.title}</span>
              <span className="agent-case-text">{item.prompt}</span>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
