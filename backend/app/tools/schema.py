"""OpenAI function-calling 工具定义

阶段 2:切换到沙箱版工具
- sandbox_tools.py 在沙箱里执行(mock 模式下走本地文件系统模拟)
- 工具实现需要 task_id 用于沙箱会话复用,通过 _TASK_CONTEXT 注入

设计要点:
- 工具签名对 LLM 透明:LLM 看到的工具定义不含 task_id
- task_id 由 react_agent 在调用 execute_tool 前注入
"""
from typing import Any

from app.tools import sandbox_tools


# 当前任务的上下文(线程本地存储更合适,但阶段 2 单进程同步执行够用)
# key: tool_call_id(str)  value: task_id(str)
# 简化处理:react_agent 在每个工具调用前 set 当前 task_id
_CURRENT_TASK_ID: str = ""


def set_current_task(task_id: str) -> None:
    """react_agent 调用工具前,设置当前任务 ID"""
    global _CURRENT_TASK_ID
    _CURRENT_TASK_ID = task_id


# 工具名 → 函数的映射
TOOL_FUNCTIONS: dict[str, Any] = {
    "clone_repo": sandbox_tools.clone_repo,
    "read_file": sandbox_tools.read_file,
    "search_code": sandbox_tools.search_code,
}

# OpenAI function-calling 工具规范
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "clone_repo",
            "description": "克隆 GitHub 仓库到沙箱,返回沙箱内的路径。审计任务开始时必须先调用此工具",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_url": {
                        "type": "string",
                        "description": "GitHub 仓库 URL,如 https://github.com/owner/repo",
                    },
                    "branch": {
                        "type": "string",
                        "description": "分支名,可选。不传则克隆默认分支",
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
            "description": "读取仓库内某个文件的内容。用于查看具体代码实现",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "clone_repo 返回的 path",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "仓库内相对路径,如 'src/main.py'",
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
            "name": "search_code",
            "description": (
                "在仓库内用正则搜索代码,返回匹配的文件:行号:内容。"
                "用于查找危险模式(如 eval/exec、SQL 拼接、硬编码密钥等)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "clone_repo 返回的 path",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "正则表达式,如 'eval\\(|exec\\(' 或 'password\\s*=\\s*[\"\\']'",
                    },
                    "file_glob": {
                        "type": "string",
                        "description": "文件过滤 glob,如 '*.py'。不传则搜所有文本文件",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "是否大小写敏感,默认 false",
                    },
                },
                "required": ["repo_path", "pattern"],
            },
        },
    },
]


def execute_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """执行工具调用

    阶段 2:走沙箱实现,自动注入 task_id
    """
    if tool_name not in TOOL_FUNCTIONS:
        raise ValueError(f"未知工具: {tool_name}")
    func = TOOL_FUNCTIONS[tool_name]
    # 注入 task_id(react_agent 调用前 set 过)
    arguments["task_id"] = _CURRENT_TASK_ID
    return func(**arguments)
