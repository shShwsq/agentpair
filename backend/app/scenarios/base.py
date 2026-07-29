"""场景抽象基类

每个场景定义:
- checklist:任务完成判据清单(user_agent 用它判断 react_agent 是否覆盖完整)
- user_agent_prompt:user_agent 的 system prompt(扮演什么角色、如何评估、如何整理结果)
- react_agent_prompt:react_agent 的 system prompt
- enabled_tools:启用的工具白名单
- structured_result_schema:user_agent done 时输出的结构化结果 schema(分场景不同)
- extract_results:从 user_agent 最终输出提取结构化结果(分场景不同)

前端声明(场景无关 UI 驱动,阶段 7 新增):
- form_fields:提交任务表单字段定义,前端按此动态渲染
- result_grouping:结果清单分组维度声明,前端按此分组展示
- result_meta_fields:结果项 metadata 字段展示声明,前端按此渲染标签
- coverage:覆盖度看板声明,前端按此渲染维度状态

职责划分(阶段 7+ 调整):
- react_agent:执行审计/任务,输出自然语言总结(含发现、位置、建议)
- user_agent:评估覆盖度,决定追问;done=true 时按场景 schema 整理结构化结果

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
    def structured_result_schema(self) -> dict[str, Any]:
        """user_agent done=true 时输出的结构化结果 schema

        不同场景的结构化结果不同(如安全场景是漏洞清单,文档场景可能是章节摘要)。
        user_agent 在 done=true 时,按此 schema 在 JSON 输出的 results 字段里
        提供结构化数据,orchestrator 调 extract_results 落库。
        """
        ...

    def extract_results(self, ua_output: dict[str, Any]) -> list[dict[str, Any]]:
        """从 user_agent 的最终输出提取结构化结果列表

        参数:ua_output 是 user_agent 解析后的 JSON dict,含 covered/missing/
        reasoning/followup_query/done,以及场景特定的 results 字段

        返回:[{"title": str, "content": str, "metadata": dict}, ...]
        每个元素对应一条 Result 记录,由 orchestrator 落库
        """
        ...

    def format_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把单条原始 result 转成数据库存储格式(兜底用,extract_results 内部可调用)

        返回:{"title": str, "content": str, "metadata": dict}
        默认实现:直接透传 title/content,其余放 metadata
        """
        ...

    # ---------- 前端声明(场景无关 UI 驱动) ----------

    @property
    def form_fields(self) -> list[dict[str, Any]]:
        """提交任务表单字段定义,前端按此动态渲染

        每个字段:
        - name: 字段名(提交时作为 params 的 key)
        - type: text / url / textarea / select / number
        - label: 显示标签
        - required: 是否必填
        - placeholder: 占位提示(可选)
        - default: 默认值(可选)
        - description: 字段说明(可选)
        - options: type=select 时的选项列表 [{"value":..., "label":...}](可选)
        """
        ...

    @property
    def result_grouping(self) -> dict[str, Any] | None:
        """结果清单分组维度声明,前端按此分组展示。None 表示不分组(平铺)

        结构:
        - field: 从 result.metadata 取该字段分组
        - type: "ordered"(固定枚举+顺序) | "dynamic"(按值动态分组)
        - values: ordered 时提供 [{"value":..., "label":..., "color":..., "order":...}]
        - default_label: 元数据缺失该字段时的分组名
        - default_color: 默认分组颜色 key
        """
        ...

    @property
    def result_meta_fields(self) -> list[dict[str, Any]]:
        """结果项 metadata 字段展示声明,前端按此渲染标签

        每个字段:
        - name: metadata 中的 key
        - label: 显示标签
        - type: "text" / "file"(file 类型可点击跳转源码位置)
        """
        ...

    @property
    def coverage(self) -> dict[str, Any] | None:
        """覆盖度看板声明,前端按此渲染维度状态。None 表示不显示看板

        结构:
        - dimensions: 维度列表 [{"id":..., "name":..., "description":...}]
          通常派生自 checklist
        - 数据来源固定:从 user_agent type=evaluation 的 reasoning 解析 covered/missing
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


def list_scenarios() -> list[dict[str, Any]]:
    """列出所有场景的完整声明(给前端动态渲染用)

    返回每个场景的 id/name + 四项前端声明(form_fields/result_grouping/
    result_meta_fields/coverage)。声明缺失时兜底为空值,保证前端兼容。
    """
    result: list[dict[str, Any]] = []
    for s in SCENARIOS.values():
        result.append({
            "id": s.id,
            "name": s.name,
            "form_fields": _get_prop(s, "form_fields", []),
            "result_grouping": _get_prop(s, "result_grouping", None),
            "result_meta_fields": _get_prop(s, "result_meta_fields", []),
            "coverage": _get_prop(s, "coverage", None),
        })
    return result


def _get_prop(scenario: "Scenario", name: str, default: Any) -> Any:
    """安全获取场景的可选属性,缺失时返回默认值"""
    try:
        val = getattr(scenario, name)
        return val if val is not None else default
    except (AttributeError, NotImplementedError):
        return default
