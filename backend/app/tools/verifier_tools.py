"""verifier_agent 专用工具:http_request

向任务配置的 test_env_url 发送 HTTP 请求,用于动态验证 react_agent 发现的安全问题。

设计:
- http_request 从后端进程执行(非沙箱),用 httpx 同步客户端
- URL = test_env_url(base, 来自 task.params._verifier)+ path(LLM 提供,相对路径)
  base 固定不可由 LLM 篡改,防止 SSRF
- 支持自定义 method/headers/body,满足 PoC 需求
- 超时 30s,响应体截断到 50000 字符(防 LLM 上下文爆炸)
- per_action 授权模式:每次调用前经 user_interaction 阻塞等用户确认

run_python_code 复用 sandbox_tools(与 react_agent 共享沙箱会话),
不在此文件重新声明,由 verifier_agent 直接从 sandbox_tools 导入。
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)

# HTTP 请求超时(秒)
_HTTP_TIMEOUT = 30.0
# 响应体最大字符数(截断防 LLM 上下文爆炸)
_MAX_BODY_CHARS = 50000


def http_request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    task_id: str = "",
    test_env_url: str = "",
) -> dict[str, Any]:
    """向测试环境发送 HTTP 请求(纯执行,授权由 verifier_agent 统一拦截)

    参数:
        method: HTTP 方法(GET / POST / PUT / PATCH / DELETE / HEAD / OPTIONS)
        path: 相对路径(拼到 test_env_url 后面),如 "/api/users" 或 "/login?next=/"
        headers: 自定义请求头(可选)
        body: 请求体字符串(可选;POST/PUT 常用)
        task_id: 当前任务 ID(自动注入)
        test_env_url: 测试环境基址(自动注入,来自 task.params._verifier)

    返回:
        {"status_code": int, "headers": dict, "body": str, "elapsed_ms": int, "truncated": bool}
    """
    method = method.upper().strip()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return {"status_code": 0, "body": f"[不支持的方法: {method}]"}

    if not test_env_url:
        return {"status_code": 0, "body": "[未配置 test_env_url,无法发送请求]"}

    # 拼接完整 URL(base 去尾斜杠,path 补头斜杠)
    base = test_env_url.rstrip("/")
    p = path if path.startswith("/") else "/" + path
    full_url = base + p

    # 执行 HTTP 请求
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = client.request(
                method,
                full_url,
                headers=headers,
                content=body.encode("utf-8") if body else None,
            )
        resp_body = resp.text or ""
        truncated = len(resp_body) > _MAX_BODY_CHARS
        if truncated:
            resp_body = resp_body[:_MAX_BODY_CHARS] + f"\n\n[响应体已截断,原始长度 {len(resp.text)} 字符]"
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": resp_body,
            "elapsed_ms": int(resp.elapsed.total_seconds() * 1000),
            "truncated": truncated,
        }
    except httpx.TimeoutException:
        return {
            "status_code": 0,
            "body": f"[请求超时({_HTTP_TIMEOUT}s)]",
            "elapsed_ms": int(_HTTP_TIMEOUT * 1000),
        }
    except Exception as e:
        logger.warning(f"[task={task_id}] HTTP {method} {full_url} 失败: {e}")
        return {"status_code": 0, "body": f"[请求失败: {e}]", "elapsed_ms": 0}


# ============================================================
# verifier_agent 工具定义(OpenAI function-calling 格式)
# ============================================================

VERIFIER_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "向测试环境发送 HTTP 请求,验证 react_agent 发现的安全问题是否真实可利用。"
                "URL = 任务配置的 test_env_url + path(相对路径),base 不可篡改。"
                "用于:发送 PoC payload 验证注入、访问受保护端点验证认证绕过、"
                "提交恶意输入验证 XSS/SSRF 等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                        "description": "HTTP 方法",
                    },
                    "path": {
                        "type": "string",
                        "description": "相对路径(拼到 test_env_url 后面),如 /api/users / /login?next=/",
                    },
                    "headers": {
                        "type": "object",
                        "description": "自定义请求头(可选),如 {\"Content-Type\": \"application/json\", \"Authorization\": \"Bearer ...\"}",
                    },
                    "body": {
                        "type": "string",
                        "description": "请求体字符串(可选;POST/PUT 常用),如 JSON payload 或表单数据",
                    },
                },
                "required": ["method", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python_code",
            "description": (
                "在沙箱里执行 Python 代码,返回 stdout/stderr/exit_code。"
                "用于构造复杂 PoC 脚本(如生成签名、编码 payload、解析响应)。"
                "复用 react_agent 的沙箱会话,可 read_file 仓库代码辅助构造 PoC。"
                "网络访问依赖沙箱配置(默认禁外网,HTTP 探测用 http_request)。单次超时 60s。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python 代码(字符串)"},
                    "timeout": {"type": "integer", "description": "超时秒数,默认 60,上限 120"},
                },
                "required": ["code"],
            },
        },
    },
]
