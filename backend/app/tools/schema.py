"""OpenAI function-calling 工具定义

阶段 4 重构:场景化工具配置
- 通用工具(clone/read/search/cve/semgrep)定义在此,场景通过白名单选择启用哪些
- submit_results 是内部工具,定义从场景取(不同场景的 result 结构不同)
- task_id 自动注入
"""
from typing import Any

from app.scenarios.base import get_scenario
from app.tools import sandbox_tools
from app.tools.cve_tools import query_cve


# 当前任务的上下文
_CURRENT_TASK_ID: str = ""


def set_current_task(task_id: str) -> None:
    """react_agent 调用工具前,设置当前任务 ID"""
    global _CURRENT_TASK_ID
    _CURRENT_TASK_ID = task_id


# 工具名 → 函数的映射(通用工具,所有场景共用池)
TOOL_FUNCTIONS: dict[str, Any] = {
    "clone_repo": sandbox_tools.clone_repo,
    "list_files": sandbox_tools.list_files,
    "read_file": sandbox_tools.read_file,
    "search_code": sandbox_tools.search_code,
    "run_semgrep": sandbox_tools.run_semgrep,
    "query_cve": query_cve,
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
            "description": "读取仓库内某个文件的内容",
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
                        "description": "最大返回行数,默认 500",
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
            "description": "在仓库内用正则搜索代码,返回匹配的文件:行号:内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "clone_repo 返回的 path"},
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "file_glob": {"type": "string", "description": "文件过滤 glob,如 '*.py'"},
                    "case_sensitive": {"type": "boolean", "description": "是否大小写敏感,默认 false"},
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
]


def get_tools_for_scenario(scenario_id: str) -> list[dict[str, Any]]:
    """根据场景返回工具定义列表

    = 场景白名单启用的通用工具 + submit_results(内部工具)
    """
    scenario = get_scenario(scenario_id)
    enabled = set(scenario.enabled_tools)

    # 按白名单过滤通用工具
    tools = [t for t in _ALL_TOOL_DEFINITIONS if t["function"]["name"] in enabled]
    # 加 submit_results
    tools.append(scenario.submit_tool_schema)
    return tools


def execute_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """执行通用工具调用(submit_results 不走这里,由 react_agent 内部处理)"""
    if tool_name not in TOOL_FUNCTIONS:
        raise ValueError(f"未知工具: {tool_name}")
    func = TOOL_FUNCTIONS[tool_name]
    arguments["task_id"] = _CURRENT_TASK_ID
    return func(**arguments)
