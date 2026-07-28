"""场景抽象基类

每个场景定义:
- checklist:任务完成判据清单(user_agent 用它判断 react_agent 是否覆盖完整)
- user_agent_prompt:user_agent 的 system prompt(扮演什么角色、如何评估)
- react_agent_prompt:react_agent 的 system prompt
- enabled_tools:启用的工具白名单
- submit_tool_schema:submit_results 工具的参数定义(不同场景的 result 结构不同)

轻量配置:场景是 Python 类,注册到 SCENARIOS 字典,代码内 if-else 分发
"""
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Scenario(Protocol):
    """场景协议:定义场景必须实现的接口"""

    id: str
    name: str

    @property
    def checklist(self) -> list[dict[str, Any]]:
        """任务完成判据清单

        通用结构:[{"id": "xxx", "name": "xxx", "description": "xxx", "checklist": [...]}]
        场景可自由扩展额外字段(如安全场景加 cwe),user_agent 格式化时只取通用字段
        """
        ...

    @property
    def user_agent_prompt(self) -> str:
        """user_agent 的 system prompt

        定义 user_agent 扮演的角色、评估依据、输出格式等。
        prompt 中用 {checklist_text} 占位符,运行时替换为格式化后的 checklist 文本
        """
        ...

    @property
    def react_agent_prompt(self) -> str:
        """react_agent 的 system prompt"""
        ...

    @property
    def enabled_tools(self) -> list[str]:
        """启用的工具白名单(工具名列表,对应 TOOL_FUNCTIONS 的 key)"""
        ...

    @property
    def submit_tool_schema(self) -> dict[str, Any]:
        """submit_results 工具的参数定义

        不同场景的 result 结构不同,这里定义 LLM 调用 submit_results 时的参数 schema
        """
        ...

    def format_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把 LLM 提交的原始 result 转成数据库存储格式

        返回:{"title": str, "content": str, "metadata": dict}
        默认实现:直接透传 title/content,其余放 metadata
        """
        ...


# 场景注册表
SCENARIOS: dict[str, "Scenario"] = {}


def register_scenario(scenario: "Scenario") -> None:
    """注册场景"""
    SCENARIOS[scenario.id] = scenario


def get_scenario(scenario_id: str) -> "Scenario":
    """获取场景,不存在则报错"""
    if scenario_id not in SCENARIOS:
        raise ValueError(
            f"未知场景: {scenario_id},已注册: {list(SCENARIOS.keys())}"
        )
    return SCENARIOS[scenario_id]


def list_scenarios() -> list[dict[str, str]]:
    """列出所有场景(给前端展示用)"""
    return [{"id": s.id, "name": s.name} for s in SCENARIOS.values()]
