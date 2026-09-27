"""
子智能体的统一返回契约
"""

from langchain.agents.structured_output import ToolStrategy
from pydantic import BaseModel, Field

# 字段名是本契约的一部分：父层提示词、子层提示词与回归守卫都按这四个名字对齐，
# 改名属于破坏性变更（守卫会拦住）。
REPORT_FIELDS = ("result", "sources", "truncated_by_limit", "error")


class SubAgentReport(BaseModel):
    """检索子智能体返回给主智能体的结构化报告。"""

    result: str = Field(
        description=(
            "结论正文（Markdown）。直接回答被指派的问题，保留关键数字与口径；"
            "不要写前言、客套话或任务描述里已交代过的背景。"
        )
    )
    sources: list[str] = Field(
        default_factory=list,
        description=(
            "来源标注列表：数据库表名 / 知识库文件名 / 网页 URL。"
            "确实无法标注来源时给空列表，不要编造。"
        ),
    )
    truncated_by_limit: bool = Field(
        default=False,
        description=(
            "是否因为达到调用次数上限或结果行数上限而提前结束。"
            "工具返回里出现 truncated=true，或你因次数上限而中止检索时置 true。"
        ),
    )
    error: str | None = Field(
        default=None,
        description=(
            "检索链路**技术故障**的原文（工具返回的失败信息，如以「检索失败/不可用」开头）。"
            "没有技术故障时必须为 null。"
            "注意：「知识库/网络中查不到该资料」属于正常结果，写在 result 里，不要填进 error。"
        ),
    )


def make_subagent_response_format() -> ToolStrategy:
    """
    构造一个新的 `ToolStrategy` 实例。

    每次调用返回新实例（而不是共用一个模块级单例）：三个子智能体各自持有一份，
    避免将来框架在策略对象上挂载 per-agent 状态时互相干扰。
    """
    return ToolStrategy(SubAgentReport)


if __name__ == "__main__":
    # 离线自检：schema 字段名与契约常量一致，且能被序列化成 JSON
    import json

    names = tuple(SubAgentReport.model_fields.keys())
    print("契约字段 =", names)
    assert names == REPORT_FIELDS, f"字段漂移：{names} != {REPORT_FIELDS}"
    print("示例序列化 =", SubAgentReport(result="结论", sources=["drugs"], error=None).model_dump_json(ensure_ascii=False))
    print("OK")
    _ = json  # 仅用于说明可用标准库校验，无需额外依赖
