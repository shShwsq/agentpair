"""OpenAI function-calling 工具定义

把本地工具包装成 OpenAI tools 规范,供 react_agent 调用
"""
from typing import Any


# 工具名 → Python 函数的映射(供 agent 分发调用)
from app.tools.local_tools import clone_repo, read_file, search_code

TOOL_FUNCTIONS: dict[str, Any] = {
    "clone_repo": clone_repo,
    "read_file": read_file,
    "search_code": search_code,
}

# OpenAI function-calling 工具规范
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "clone_repo",
            "description": "克隆 GitHub 仓库到本地,返回本地路径。审计任务开始时必须先调用此工具",
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

    阶段 1:直接调用本地 Python 函数
    阶段 2 起会改为沙箱执行
    """
    if tool_name not in TOOL_FUNCTIONS:
        raise ValueError(f"未知工具: {tool_name}")
    func = TOOL_FUNCTIONS[tool_name]
    return func(**arguments)
