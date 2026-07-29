"""OpenAI function-calling 工具定义

阶段 4 重构:场景化工具配置
- 通用工具(clone/read/search/cve/semgrep)定义在此,场景通过白名单选择启用哪些
- submit_results 是内部工具,定义从场景取(不同场景的 result 结构不同)
- task_id 自动注入

并发安全:用 contextvars 替代全局变量,每个后台线程有独立上下文,
避免多任务并发执行时 task_id / scenario 互相覆盖导致工作区串台。
"""
import contextvars
from typing import Any

from app.scenarios.base import get_scenario
from app.tools import sandbox_tools
from app.tools.cve_tools import query_cve
from app.tools.skill_tool import list_available_skills, run_skill, set_current_scenario


# 当前任务的上下文(每个线程独立,避免并发任务互相覆盖)
_CURRENT_TASK_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_task_id", default=""
)


def set_current_task(task_id: str, scenario_id: str = "code_security_audit") -> None:
    """react_agent 调用工具前,设置当前任务 ID 和场景

    scenario_id 用于 skill 工具按场景过滤可用 skill。
    使用 ContextVar,每个后台线程的 set 只影响该线程自身。
    """
    _CURRENT_TASK_ID.set(task_id)
    set_current_scenario(scenario_id)


# 工具名 → 函数的映射(通用工具,所有场景共用池)
TOOL_FUNCTIONS: dict[str, Any] = {
    "clone_repo": sandbox_tools.clone_repo,
    "list_files": sandbox_tools.list_files,
    "read_file": sandbox_tools.read_file,
    "search_code": sandbox_tools.search_code,
    "run_semgrep": sandbox_tools.run_semgrep,
    "query_cve": query_cve,
    "list_skills": list_available_skills,
    "skill": run_skill,
}

# 通用工具的 OpenAI function-calling 定义
_ALL_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "clone_repo",
            "description": "克隆 GitHub 仓库到沙箱,返回沙箱内的路径。任务开始时若需要代码,先调用此工具",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_url": {
                        "type": "string",
                        "description": "GitHub 仓库 URL",
                    },
                    "branch": {
                        "type": "string",
                        "description": "分支名,可选",
                    },
                },
                "required": ["repo_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取仓库内文件内容,返回带行号的内容(cat -n 格式),支持分页。"
                "首次读前 200 行,需要看后续内容时用 offset 翻页。"
                "行号可用于在结果中精确引用位置。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "clone_repo 返回的 path",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "仓库内相对路径",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "本次最多返回行数,默认 200",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "从第几行开始读(1-based),默认 1。配合 max_lines 翻页",
                    },
                },
                "required": ["repo_path", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "列出仓库内某目录下的文件和子目录(单层,不递归)。"
                "clone 后先调用此工具查看仓库结构,再决定读哪些文件。"
                "看子目录后可再次调用进入子目录查看。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "clone_repo 返回的 path",
                    },
                    "subdir": {
                        "type": "string",
                        "description": "仓库内相对路径,默认根目录。如 'src'、'tests/unit'",
                    },
                    "max_entries": {
                        "type": "integer",
                        "description": "最大返回条目数,默认 200",
                    },
                },
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": (
                "在仓库内用正则搜索代码。支持三种输出模式: "
                "content(默认,返回匹配行+行号+可选上下文)、"
                "files_with_matches(只返回含匹配的文件路径)、"
                "count(返回每个文件的匹配数)。"
                "安全审计建议用 context_lines=3-5 看上下文,便于判断是否为漏洞。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "clone_repo 返回的 path"},
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "file_glob": {"type": "string", "description": "文件过滤 glob,如 '*.py'"},
                    "case_sensitive": {"type": "boolean", "description": "是否大小写敏感,默认 false"},
                    "max_matches": {"type": "integer", "description": "本次最多返回匹配数,默认 50"},
                    "context_lines": {
                        "type": "integer",
                        "description": "匹配行前后各显示 N 行上下文(仅 content 模式),默认 0",
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                        "description": "输出模式,默认 content",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "分页偏移,跳过前 N 个匹配,默认 0",
                    },
                },
                "required": ["repo_path", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_cve",
            "description": (
                "查询指定包+版本的已知 CVE 漏洞(用 OSV API)。"
                "对每个依赖调一次"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package_name": {"type": "string", "description": "包名"},
                    "version": {"type": "string", "description": "版本号"},
                    "ecosystem": {"type": "string", "description": "包管理系统,默认 python"},
                },
                "required": ["package_name", "version"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_semgrep",
            "description": "在沙箱里运行 Semgrep 静态分析。mock 模式不可用",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "clone_repo 返回的 path"},
                    "config": {"type": "string", "description": "semgrep 配置,默认 auto"},
                },
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": (
                "列出当前场景所有可用的 skill(技能)。"
                "skill 是预封装的多步审计操作指令,clone 完仓库后建议先调用此工具查看可用技能。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill",
            "description": (
                "获取指定 skill 的详细指令(SKILL.md 内容)。"
                "拿到指令后,按其指引自行调用 search_code/read_file/run_in_sandbox 等底层工具执行。"
                "先调 list_skills 查可用 skill 名称。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "skill 名称,如 check_sql_injection",
                    },
                },
                "required": ["skill_name"],
            },
        },
    },
]


def get_tools_for_scenario(scenario_id: str) -> list[dict[str, Any]]:
    """根据场景返回工具定义列表

    = 场景白名单启用的通用工具
    注意:submit_results 已移除。react_agent 只输出自然语言总结,
    user_agent 负责按场景 schema 整理结构化结果(通过 scenario.extract_results)
    """
    scenario = get_scenario(scenario_id)
    enabled = set(scenario.enabled_tools)

    # 按白名单过滤通用工具
    tools = [t for t in _ALL_TOOL_DEFINITIONS if t["function"]["name"] in enabled]
    return tools


def execute_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """执行通用工具调用"""
    if tool_name not in TOOL_FUNCTIONS:
        raise ValueError(f"未知工具: {tool_name}")
    func = TOOL_FUNCTIONS[tool_name]
    arguments["task_id"] = _CURRENT_TASK_ID.get()
    return func(**arguments)
