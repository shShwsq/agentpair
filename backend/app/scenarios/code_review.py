"""代码审查场景模板(降级后:仅提供预设提示词 + 推荐 skill)

不再定义 checklist/prompt/工具白名单/结果 schema。
"""
from app.scenarios.base import register_scenario


class CodeReviewTemplate:
    """通用代码审查场景模板"""

    id = "code_review"
    name = "代码审查"
    description = "审查代码质量:可读性、正确性、性能、测试覆盖、架构合理性"

    @property
    def preset_prompt(self) -> str:
        return (
            "请审查这个仓库的代码质量,关注可读性(命名/复杂度/注释/重复)、"
            "正确性(边界条件/异常处理/资源泄漏/并发问题)、"
            "性能(N+1 查询/O(n²) 循环/不必要 IO)、"
            "测试覆盖(单测缺失/边界未覆盖)、"
            "架构合理性(分层/耦合/职责划分),给出具体位置和改进建议。"
        )

    @property
    def recommended_skills(self) -> list[str]:
        return []  # 代码审查无特别推荐的 skill


register_scenario(CodeReviewTemplate())
