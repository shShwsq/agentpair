"""OpenAI function-calling 工具定义

阶段 2:切换到沙箱版工具
- sandbox_tools.py 在沙箱里执行(mock 模式下走本地文件系统模拟)
- 工具实现需要 task_id 用于沙箱会话复用,通过 _TASK_CONTEXT 注入

阶段 3:新增 CVE 查询(OSV API)和 Semgrep(沙箱静态分析)
- CVE 查询纯 HTTP 调用,mock 和 sandbox 都可用
- Semgrep 需要沙箱环境,mock 模式返回提示

设计要点:
- 工具签名对 LLM 透明:LLM 看到的工具定义不含 task_id
- task_id 由 react_agent 在调用 execute_tool 前注入
"""
from typing import Any

from app.tools import sandbox_tools
from app.tools.cve_tools import query_cve


# 当前任务的上下文(线程本地存储更合适,但阶段 2 单进程同步执行够用)
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
    "run_semgrep": sandbox_tools.run_semgrep,
    "query_cve": query_cve,
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
    {
        "type": "function",
        "function": {
            "name": "query_cve",
            "description": (
                "查询指定包+版本的已知 CVE 漏洞(用 OSV API)。"
                "审计完依赖清单后,对每个依赖调一次本工具查已知漏洞。"
                "返回漏洞列表,含 CVE id、严重程度、修复版本"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package_name": {
                        "type": "string",
                        "description": "包名,大小写与依赖清单一致,如 'flask'、'requests'",
                    },
                    "version": {
                        "type": "string",
                        "description": "版本号,如 '2.0.0'",
                    },
                    "ecosystem": {
                        "type": "string",
                        "description": "包管理系统,默认 python(PyPI)。可选:npm/go/java/php/ruby/rust/csharp",
                    },
                },
                "required": ["package_name", "version"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_semgrep",
            "description": (
                "在沙箱里运行 Semgrep 静态分析,自动扫描代码漏洞。"
                "适合作为补充检查,与手动 search_code 配合。"
                "注意:需要沙箱环境(sandbox 模式),mock 模式不可用"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "clone_repo 返回的 path",
                    },
                    "config": {
                        "type": "string",
                        "description": "semgrep 配置,默认 'auto' 自动选规则集。也可指定 'p/python'、'p/javascript' 等",
                    },
                },
                "required": ["repo_path"],
            },
        },
    },
]


def execute_tool(tool_name: str, arguments: dict[str, Any]) -> Any:
    """执行工具调用

    阶段 3:走沙箱实现 + CVE 查询,自动注入 task_id
    """
    if tool_name not in TOOL_FUNCTIONS:
        raise ValueError(f"未知工具: {tool_name}")
    func = TOOL_FUNCTIONS[tool_name]
    # 注入 task_id(react_agent 调用前 set 过)
    arguments["task_id"] = _CURRENT_TASK_ID
    return func(**arguments)
