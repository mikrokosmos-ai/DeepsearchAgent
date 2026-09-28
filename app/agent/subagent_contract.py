"""
子智能体的统一返回契约
"""

import json
import re

from pydantic import BaseModel, Field, ValidationError, field_validator

# 字段名是本契约的一部分：父层提示词、子层提示词与回归守卫都按这四个名字对齐，
# 改名属于破坏性变更（守卫会拦住）。
REPORT_FIELDS = ("result", "sources", "truncated_by_limit", "error")

# 框架级强制（response_format）在本项目端点上是否可用。守卫据此断言子智能体**不得**配置它。
FRAMEWORK_RESPONSE_FORMAT_USABLE = False


class SubAgentReport(BaseModel):
    """检索子智能体返回给主智能体的结构化报告（字段形状即契约）。"""

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

    @field_validator("error", mode="before")
    @classmethod
    def _normalize_error(cls, value):
        """
        把"空故障"归一成 None。

        真模型实测：即使提示词写明"没有故障时必须为 null"，它仍会写成 `""` 或 `"null"`。
        若原样保留，父层会以为"有故障信息"从而触发失败降级 —— 属于必须挡住的假信号。
        """
        if value is None:
            return None
        text = str(value).strip()
        if text == "" or text.lower() in {"null", "none", "无", "n/a", "na"}:
            return None
        return text


# 匹配 ```json {...} ``` 代码块；也接受没有语言标记的 ``` {...} ```
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_report_from_text(text: str) -> SubAgentReport | None:
    """
    从子智能体的回复文本里解析出契约对象。

    容错顺序：① 最后一个 ```json 代码块 → ② 文中最后一个裸 JSON 对象。
    解析或校验失败时返回 None（调用方应退化为"按正文理解"，**不要**因此判定任务失败）。

    :param text: 子智能体的回复文本
    :return: SubAgentReport 或 None
    """
    if not isinstance(text, str) or not text.strip():
        return None

    candidates: list[str] = []
    blocks = _FENCED_JSON_RE.findall(text)
    if blocks:
        candidates.append(blocks[-1])  # 取最后一个：模型可能先给示例再给结论

    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])

    for raw in candidates:
        try:
            return SubAgentReport.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError):
            continue
    return None


if __name__ == "__main__":
    # 离线自检：字段名不漂移、解析器对正/反样例的行为正确
    assert tuple(SubAgentReport.model_fields.keys()) == REPORT_FIELDS, "字段漂移"
    assert FRAMEWORK_RESPONSE_FORMAT_USABLE is False, "端点已支持框架级强制？请重新评估本模块结论"

    ok = [
        '结论如下。\n```json\n{"result":"A","sources":["drugs"],"truncated_by_limit":false,"error":null}\n```',
        '{"result":"B","sources":[],"truncated_by_limit":true,"error":null}',
        '先给一个示例 {"result":"x"}，再给结论：\n```json\n{"result":"C","sources":["a.pdf"],"truncated_by_limit":false,"error":"链路故障"}\n```',
    ]
    expect = ["A", "B", "C"]  # 第三个必须取**最后**一个块（C），不能取示例 x
    for text, want in zip(ok, expect):
        got = parse_report_from_text(text)
        assert got is not None and got.result == want, f"解析失败：期望 {want}，得到 {got}"
        print(f"  OK  -> {want}")

    for bad in ["", "没有 JSON", "```json\n{不是合法JSON}\n```", "```json\n{\"result\":123}\n```"]:
        got = parse_report_from_text(bad)
        assert got is None, f"非法输入不应解析成功：{bad!r} -> {got}"
        print(f"  OK  -> 正确拒绝 {bad!r}")

    # 空故障归一化：模型实测会写 "" / "null"，不能让它被当成真故障
    for empty in ["", "  ", "null", "None", "无"]:
        r = SubAgentReport(result="x", error=empty)
        assert r.error is None, f"空故障未归一化：{empty!r} -> {r.error!r}"
    assert SubAgentReport(result="x", error="查询出现异常：连接超时").error is not None
    print("  OK  -> 空故障归一化为 None，真实故障保留")

    print("OK")
