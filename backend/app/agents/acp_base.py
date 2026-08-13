"""ACP 基础设施:共享的 ACP 客户端、bridge 管理、事件收集、凭证加载等

被 qoder_cli_agent.py 和 kimi_cli_agent.py 复用,避免重复实现。
所有函数均通过 agent_type 参数支持多种 CLI(qoder_cli / qoder_cli_cn / kimi_cli),
各 CLI 的差异(启动参数、认证方式、模型选择方式)由 registry 配置 + wrapper 层回调处理。

工作流程(通用):
1. 复用 sandbox_tools 的沙箱会话(orchestrator 已预 clone 仓库)
2. 查本任务缓存的 bridge + ACP session(命中则直接跳到 7;bridge/CLI 进程驻留
   沙箱,会话上下文随 session 在轮次/追问间自然延续)
3. 未命中:从 user_agent_configs 加载用户凭证(加密存储),经 registry 映射为环境变量
4. 将 acp_bridge.py 写入沙箱,后台启动(监听端口 ACP_BRIDGE_PORT),凭证经 envs 注入
5. 通过 get_endpoint(ACP_BRIDGE_PORT) 获取转发地址 + headers
6. ACP 客户端:initialize → session/new → [post_session_setup],随后写入缓存
7. session/prompt 流式接收 session/update 通知,翻译为 event_bus 事件
8. 收集最终 summary,提取 plan,返回 (results, summary, plan)

bridge 不在每轮结束时停止:进程随沙箱会话存活,供后续轮次/resume 追问复用
(省去 ensure_cli_env/start_bridge/initialize/new_session 合计 ~25s);
沙箱会话销毁(close_session)时经 stop_task_bridge 清缓存,容器销毁连带回收进程。

各 wrapper 的差异通过回调/参数注入:
- post_session_setup(client, session_id, task):session/new 之后、prompt 之前执行
  (kimi 用此回调调 set_config_option 设置 yolo 模式)
- test_acp_args:测试连接时额外的 CLI 参数
  (qoder 用 ["--model","Qwen3.6-Flash","--reasoning-effort","low"])
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import uuid
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from sqlalchemy.orm import Session

from app.agents.registry import get_agent_meta, get_sandbox_config
from app.config import settings
from app.event_bus import publish
from app.models.task import Conversation, Task
from app.models.user_agent_config import UserAgentConfig
from app.perf import perf_log
from app.security import decrypt_secret
from app.tools import sandbox_tools
from app.tools.schema import set_current_task
from app.user_interaction import request_command_confirm, wait_for_command_confirm

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

# ACP bridge 监听端口(沙箱内)
ACP_BRIDGE_PORT = 8088

# 沙箱内文件路径
BRIDGE_SCRIPT_PATH = "/home/user/.acp/acp_bridge.py"
BRIDGE_WORK_DIR = "/home/user"

# bridge 启动超时(秒):等待 CLI 进程就绪 + HTTP 服务监听
BRIDGE_STARTUP_TIMEOUT = 30
BRIDGE_HEALTH_INTERVAL = 1.0

# ACP 协议版本(数字 1,见 https://github.com/agentclientprotocol/agent-client-protocol
# "The current stable ACP protocol version is 1.")
ACP_PROTOCOL_VERSION = 1

# 本地 bridge 源文件路径(用于写入沙箱)
# 支持 per-agent 自定义 bridge:registry sandbox.bridge_script 指定使用哪个
_BRIDGE_SOURCES = {
    "acp_bridge": Path(__file__).parent / "acp_bridge.py",  # 通用 ACP stdio 桥接(hermes/kimi/qoder)
    "codex_bridge": Path(__file__).parent / "codex_bridge.py",  # Codex 专用(codex exec --json → ACP 翻译)
}
# 默认 bridge(acp_bridge.py,通用 ACP stdio 桥接)
_DEFAULT_BRIDGE = "acp_bridge"

# 默认 ACP 日志目录:backend/logs/acp/
_ACP_LOG_DIR = Path(__file__).resolve().parents[2] / "logs" / "acp"


# ============================================================
# ACP HTTP 客户端
# ============================================================


class ACPClient:
    """ACP HTTP 客户端:通过 HTTP/SSE 与沙箱内的 acp_bridge 通信

    桥接服务将 HTTP 请求转换为 CLI 的 stdio ACP(JSON-RPC over
    newline-delimited JSON),响应以 SSE 流式返回。

    使用方式:
        client = ACPClient(endpoint_url, endpoint_headers)
        client.initialize()
        session_id = client.new_session(cwd="/home/user/repo")
        result = client.prompt(session_id, [{"type":"text","text":"hi"}], on_event=callback)
        client.close()
    """

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str] | None = None,
        recorder: _ACPRecorder | None = None,
        permission_handler=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
        # 可选的原始响应记录器:在 _rpc 的 SSE 循环里记录每一行原文,
        # 任何解析/过滤之前落盘。None 表示不记录(如 test_credential 流程)。
        self.recorder = recorder
        # 可选的命令确认处理器:CLI 发来 request_permission 时(经 bridge 转为
        # permission_request SSE 事件),调用此 handler 让用户确认。
        # 签名:permission_handler(payload: dict) -> dict
        # 返回:{"outcome": "selected", "option_id": "allow_once"} 或 {"outcome": "rejected"}
        # None 表示不处理(默认拒绝,兼容 always_approve 模式下 CLI 仍开 yolo 不会发请求的场景)
        self.permission_handler = permission_handler
        # read timeout=None:session/prompt 可能长时间流式输出
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=10, read=None, write=30, pool=30),
            headers=self.headers,
        )
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _rpc(
        self,
        request: dict,
        on_event=None,
        timeout: httpx.Timeout | float | None = None,
    ) -> dict:
        """发送 JSON-RPC 请求,可选流式处理通知,返回最终响应 result

        桥接服务对所有 POST /rpc 返回 SSE(text/event-stream):
        - 通知(method 字段,无 id):中间事件,通过 on_event 回调处理
        - 最终响应(有 id 匹配):流结束标志,返回其 result

        on_event: 接收通知 dict 的回调函数。None 表示不处理中间事件
        (用于 initialize / session/new 等快速调用)。
        timeout: 本次请求的超时(秒或 httpx.Timeout)。None 用 client 默认
        (read=None 无限等待)。测试场景应传有限值,避免模型无响应时卡死。
        """
        request_id = request.get("id")
        method = request.get("method", "?")

        # 记录请求开始元信息(便于事后按 JSONL 边界定位每个 RPC 调用)
        if self.recorder:
            self.recorder.record_raw(
                f"--> {method} id={request_id} params={json.dumps(request.get('params', {}), ensure_ascii=False)}",
                kind="meta",
            )

        with self._client.stream(
            "POST",
            f"{self.base_url}/rpc",
            json=request,
            timeout=timeout,
        ) as response:
            if response.status_code != 200:
                response.read()
                body = response.text
                # 完整记录 HTTP 错误响应体(不截断)
                if self.recorder:
                    self.recorder.record_raw(
                        f"HTTP {response.status_code}\n{body}",
                        kind="http_error",
                    )
                raise RuntimeError(
                    f"ACP 请求失败: HTTP {response.status_code}, body={body[:500]}"
                )

            final_result: dict | None = None
            # 跟踪当前 SSE 事件类型(从 event: 行读取)
            # bridge 推 event: permission_request 时,后续 data: 行是 permission 载荷,
            # 不是 ACP 通知,需要走 permission_handler 路径
            current_event_type: str | None = None

            for line in response.iter_lines():
                # 最先记录原始行(不解析、不过滤、不截断),
                # 确保即使后续 JSONDecodeError 或分发逻辑跳过,
                # 原始 SSE 文本仍完整保留在 JSONL 中。
                if self.recorder:
                    self.recorder.record_raw(line, kind="line")

                line = line.strip()
                if not line:
                    # SSE 事件分隔符(空行),重置事件类型
                    current_event_type = None
                    continue

                # event: 行,记录事件类型(如 permission_request)
                if line.startswith("event:"):
                    current_event_type = line[6:].strip()
                    continue

                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue

                # permission_request 事件:CLI 检测到危险命令,经 bridge 转发,
                # 调 permission_handler 让用户确认,然后 POST /permission_response 给 bridge
                if current_event_type == "permission_request":
                    try:
                        perm_payload = json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning(f"[acp] permission_request 载荷非 JSON: {data[:100]}")
                        continue
                    self._handle_permission_request(perm_payload)
                    continue  # 不走 on_event,继续读后续 SSE

                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    logger.debug(f"[acp] 非 JSON SSE 行,跳过: {data[:100]}")
                    continue

                # 错误响应(JSON-RPC error)
                if "error" in msg and msg.get("id") == request_id:
                    err = msg["error"]
                    code = err.get("code")
                    message = err.get("message", "")
                    data = err.get("data")
                    # data 字段可能含详细错误信息(如 acp 库的 -32603 Internal error
                    # 会在 data 里附带原始异常 traceback 字符串)
                    parts = [f"ACP 错误 {code}: {message}"]
                    if data:
                        data_str = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
                        parts.append(f"详情: {data_str}")
                    raise RuntimeError("\n".join(parts))

                # 最终响应(id 匹配)
                if msg.get("id") == request_id:
                    final_result = msg.get("result") or {}
                    break

                # 通知(有 method,无 id 或 id 不匹配)
                if on_event and "method" in msg:
                    on_event(msg)

        if final_result is None:
            final_result = {}
        return final_result

    def _handle_permission_request(self, payload: dict) -> None:
        """处理 bridge 发来的 permission_request SSE 事件。

        CLI 检测到危险命令 → 经 bridge 转为 permission_request SSE 事件 →
        调 permission_handler 让用户确认 → POST /permission_response 提交结果给 bridge →
        bridge 把结果作为 JSON-RPC 响应写回 CLI stdin。

        payload 结构:{
            "id": "<perm_id>",
            "command": "rm -rf /",
            "description": "...",
            "options": [{"option_id": "allow_once", ...}, ...]
        }
        """
        perm_id = payload.get("id", "")
        if not perm_id:
            logger.warning("[acp] permission_request 载荷无 id,跳过")
            return

        if self.permission_handler is None:
            # 无 handler,默认拒绝(不应发生:always_approve 模式下 CLI 开 yolo 不会发请求)
            logger.warning(f"[acp] 收到 permission_request 但无 handler,默认拒绝(perm_id={perm_id})")
            outcome = {"outcome": "rejected"}
        else:
            try:
                outcome = self.permission_handler(payload)
            except Exception as e:
                logger.warning(f"[acp] permission_handler 异常: {e}", exc_info=True)
                outcome = {"outcome": "rejected"}

        # POST /permission_response 提交结果给 bridge
        try:
            self._client.post(
                f"{self.base_url}/permission_response",
                json={"id": perm_id, "outcome": outcome},
                timeout=30,
            )
        except Exception as e:
            logger.warning(f"[acp] 提交 permission_response 失败(perm_id={perm_id}): {e}")

    def initialize(self) -> dict:
        """ACP 握手:交换协议版本和能力

        返回的 result 可能含 authMethods(若 Agent 要求认证),
        此时客户端须先调 authenticate(methodId) 才能创建 session。
        见 https://agentclientprotocol.com/protocol/authentication
        """
        return self._rpc({
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "AgentPair", "version": "1.0.0"},
            },
            "id": self._next_id(),
        })

    def authenticate(
        self,
        method_id: str,
        timeout: httpx.Timeout | float | None = None,
    ) -> dict:
        """ACP 认证:用 initialize 返回的某个 authMethod id 完成认证

        Agent 在 initialize 响应中通过 authMethods 声明支持的认证方式,
        客户端选一个调本方法。凭证经环境变量注入到 bridge 进程,
        CLI 子进程继承后在此步骤完成服务端认证。

        认证成功后才能创建 session,否则会收到 -32000 Authentication required。
        timeout: 超时(秒),认证可能涉及网络往返验证,建议传有限值。
        """
        return self._rpc({
            "jsonrpc": "2.0",
            "method": "authenticate",
            "params": {"methodId": method_id},
            "id": self._next_id(),
        }, timeout=timeout)

    def new_session(self, cwd: str | None = None) -> str:
        """创建 ACP 会话,返回 session_id

        params 按 ACP 规范必须含 mcpServers(可为空数组),cwd 为可选工作目录。
        见 https://agentclientprotocol.com/protocol/session-setup
        """
        params: dict[str, Any] = {"mcpServers": []}
        if cwd:
            params["cwd"] = cwd
        result = self._rpc({
            "jsonrpc": "2.0",
            "method": "session/new",
            "params": params,
            "id": self._next_id(),
        })
        session_id = result.get("sessionId") or result.get("session_id") or ""
        if not session_id:
            raise RuntimeError(f"ACP session/new 未返回 sessionId: {result}")
        return session_id

    def set_config_option(
        self, session_id: str, config_id: str, value: str
    ) -> dict:
        """设置会话配置项(ACP session/set_config_option)

        用于运行时切换模型 / 思考强度 / 模式等,无需重启 CLI。
        常见 configId:
        - 'mode':值 'default' / 'plan' / 'auto' / 'yolo'
          (yolo = 跳过权限确认,等价 --yolo)
        - 'model':值 为模型别名(如 'kimi-for-coding')
        - 'thinking':值 'low' / 'medium' / 'high' / 'xhigh' / 'max' / 'off'

        kimi CLI 的 ACP 模式无 --yolo / --model 启动参数,
        通过本方法在 session/new 后设置。
        """
        return self._rpc({
            "jsonrpc": "2.0",
            "method": "session/set_config_option",
            "params": {
                "sessionId": session_id,
                "configId": config_id,
                "value": value,
            },
            "id": self._next_id(),
        })

    def prompt(
        self,
        session_id: str,
        prompt: list[dict],
        on_event=None,
        timeout: httpx.Timeout | float | None = None,
    ) -> dict:
        """发送 prompt,流式处理通知,返回最终结果

        prompt: ACP/MCP content 数组,如 [{"type":"text","text":"你好"}]
        (注意:不是 OpenAI 的 {"role","content"} 格式,ACP 用 MCP content 格式)
        on_event: 接收 session/update 通知的回调
        timeout: 本次请求超时(秒或 httpx.Timeout)。None 用 client 默认
        (read=None 无限等待)。测试场景应传有限值。
        """
        return self._rpc({
            "jsonrpc": "2.0",
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": prompt,
            },
            "id": self._next_id(),
        }, on_event=on_event, timeout=timeout)

    def cancel(self, session_id: str) -> None:
        """取消正在进行的 prompt"""
        try:
            self._rpc({
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": session_id},
                "id": self._next_id(),
            })
        except Exception as e:
            logger.warning(f"[acp] cancel 失败(忽略): {e}")

    def health(self) -> bool:
        """健康检查:bridge + CLI 是否存活"""
        try:
            resp = self._client.get(f"{self.base_url}/health", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("status") == "ok"
            return False
        except Exception:
            return False

    def close(self) -> None:
        self._client.close()


# ============================================================
# 凭证加载 + 环境变量映射
# ============================================================


def _load_credentials(db: Session, user_id, agent_type: str) -> dict[str, str]:
    """从 user_agent_configs 加载解密后的凭证 dict

    返回如 {"pat": "xxx"} 或 {"api_key": "sk-xxx", "model": "kimi-for-coding"}。
    未配置或解密失败时抛错(CLI executor 需要凭证才能认证)。
    agent_type: agent 类型标识(如 "qoder_cli" / "kimi_cli")。
    """
    if user_id is None:
        raise RuntimeError("外部 CLI 执行器需要登录用户(匿名任务不支持)")

    row = (
        db.query(UserAgentConfig)
        .filter(
            UserAgentConfig.user_id == user_id,
            UserAgentConfig.agent_type == agent_type,
        )
        .first()
    )
    if row is None or not row.credentials_encrypted:
        raise RuntimeError(
            f"未配置 {agent_type} 凭证。请在「智能体配置」中配置相应凭证。"
        )

    try:
        plaintext = decrypt_secret(row.credentials_encrypted)
        data = json.loads(plaintext)
        if not isinstance(data, dict):
            raise ValueError("凭证格式错误(非 JSON 对象)")
        return data
    except Exception as e:
        raise RuntimeError(f"凭证解密失败: {e}") from e


def _build_credential_envs(credentials: dict[str, str], agent_type: str) -> dict[str, str]:
    """将凭证 dict 映射为环境变量 dict(按 registry 的 credential_env)

    registry 中 credential_env 形如 {"pat": "QODER_PERSONAL_ACCESS_TOKEN"},
    即凭证 key → 环境变量名。只注入有值的凭证。

    另外,若 registry 配了 credential_env_defaults(形如
    {"KIMI_MODEL_NAME": "kimi-for-coding"}),则将这些默认值也注入,
    确保某些必须的环境变量即使用户未填也有默认值。
    """
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    cred_env_map: dict[str, str] = sandbox_cfg.get("credential_env", {})
    envs: dict[str, str] = {}
    for cred_key, env_name in cred_env_map.items():
        val = credentials.get(cred_key)
        if val:
            envs[env_name] = val

    # 注入默认值(仅当该环境变量尚未由凭证设置时)
    for env_name, default_val in (sandbox_cfg.get("credential_env_defaults") or {}).items():
        if env_name not in envs and default_val:
            envs[env_name] = default_val

    return envs


# ============================================================
# 沙箱环境准备:bridge 脚本 + CLI 可用性检查
# ============================================================


def _get_bin(agent_type: str) -> str:
    """从 settings 读取 CLI 可执行文件名(经 registry 的 config key)"""
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    config_key = sandbox_cfg.get("bin_config_key", "")
    if config_key:
        val = getattr(settings, config_key, None)
        if val:
            return val
    return sandbox_cfg.get("bin_default", "")


def _get_install_cmd(agent_type: str) -> str:
    """从 settings 读取 CLI 安装命令(经 registry 的 config key)"""
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    config_key = sandbox_cfg.get("install_cmd_config_key", "")
    if config_key:
        val = getattr(settings, config_key, None)
        if val:
            return val
    return sandbox_cfg.get("install_cmd_default", "")


def _get_acp_args(
    task: Task | None = None,
    agent_type: str = "",
    extra_args: list[str] | None = None,
) -> list[str]:
    """从 registry 读取 ACP 启动参数,按需注入 task.params 中的模型配置

    支持的 task.params 字段(均为可选):
        model:            模型名
        reasoning_effort: 思考强度(low/medium/high/xhigh/max)
        context_window:   上下文窗口
        _executor_command_confirm: 命令确认模式("always_approve"/"per_command")
            per_command 时移除 --yolo(Qoder),让 CLI 发 request_permission 给前端确认

    若 registry 的 sandbox.inject_cli_model_args 为 False(如 kimi CLI
    的 ACP 模式不支持 --model 等 CLI 参数),则不注入 task.params 模型配置
    —— 由 wrapper 层通过 set_config_option 在 session/new 后设置。

    extra_args: 额外追加的 CLI 参数(如测试时强制用特定模型),
    追加在 task.params 配置之后,会覆盖同名参数(CLI 以最后的为准)。
    """
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    args = list(sandbox_cfg.get("acp_args", []))

    # 仅当 registry 声明支持 CLI 模型参数时才注入(qoder=True, kimi=False)
    if sandbox_cfg.get("inject_cli_model_args", True) and task and task.params:
        model = task.params.get("model")
        if model:
            args.extend(["--model", str(model)])
        effort = task.params.get("reasoning_effort")
        if effort:
            args.extend(["--reasoning-effort", str(effort)])
        ctx = task.params.get("context_window")
        if ctx:
            args.extend(["--context-window", str(ctx)])

    # 命令确认模式:per_command 时移除 --yolo(Qoder 的 yolo 在 acp_args)
    # 让 CLI 进入 approval 模式,遇到危险命令发 request_permission 给前端确认
    # Kimi/Hermes 的 yolo 在 wrapper 层(post_session_setup / env)处理
    # Codex 用 --dangerously-bypass-approvals-and-sandbox,不支持 per_command,在 wrapper 降级
    if task and task.params:
        approval_mode = task.params.get("_executor_command_confirm", "always_approve")
        if approval_mode == "per_command":
            args = [a for a in args if a != "--yolo"]

    if extra_args:
        args.extend(extra_args)
    return args


def _write_bridge_script(session, agent_type: str = "") -> None:
    """将 bridge 脚本写入沙箱(从本地源文件读取)

    agent_type 决定使用哪个 bridge(从 registry sandbox.bridge_script 读取):
    - "acp_bridge"(默认):通用 ACP stdio 桥接,适用于原生支持 ACP 的 CLI(hermes/kimi/qoder)
    - "codex_bridge":Codex 专用,将 codex exec --json JSONL 翻译为 ACP 通知
    """
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    bridge_name = sandbox_cfg.get("bridge_script", _DEFAULT_BRIDGE)
    source = _BRIDGE_SOURCES.get(bridge_name)
    if source is None:
        raise RuntimeError(f"未知 bridge 脚本: {bridge_name}")
    if not source.exists():
        raise RuntimeError(f"bridge 源文件不存在: {source}")
    content = source.read_text(encoding="utf-8")
    session.write_file(BRIDGE_SCRIPT_PATH, content)


def _ensure_cli_env(session, agent_type: str) -> None:
    """准备沙箱内 CLI 运行环境

    1. 创建 bridge 脚本目录
    2. 写入 acp_bridge.py
    3. 检查 CLI 是否可用,不可用则尝试安装
    """
    # 创建脚本目录
    session.run_command(f"mkdir -p {Path(BRIDGE_SCRIPT_PATH).parent.as_posix()}")

    # 写入 bridge 脚本(per-agent,默认 acp_bridge.py)
    _write_bridge_script(session, agent_type)

    # 检查 CLI 是否可用
    cli_bin = _get_bin(agent_type)
    check_cmd = f"command -v {cli_bin} || which {cli_bin} 2>/dev/null"
    result = session.run_command(check_cmd, timeout=10)
    if not result.strip():
        # CLI 未安装,尝试安装
        install_cmd = _get_install_cmd(agent_type)
        if not install_cmd:
            raise RuntimeError(
                f"沙箱内未找到 {cli_bin},且安装命令为空。"
                f"请在沙箱镜像中预装 CLI,或在配置中设置安装命令。"
            )
        logger.info(f"[{agent_type}] {cli_bin} 未安装,执行: {install_cmd}")
        install_result = session.run_command(install_cmd, timeout=120, check=False)
        # 再次检查
        result = session.run_command(check_cmd, timeout=10)
        if not result.strip():
            raise RuntimeError(
                f"CLI 安装失败({install_cmd})。"
                f"安装日志: {install_result[:500]}"
            )

    logger.info(f"[{agent_type}] 环境就绪: {cli_bin} 可用,bridge 脚本已写入 {BRIDGE_SCRIPT_PATH}")


# ============================================================
# ACP bridge 生命周期管理
# ============================================================


def _start_acp_bridge(
    session,
    credential_envs: dict[str, str],
    task: Task | None = None,
    agent_type: str = "",
    extra_acp_args: list[str] | None = None,
) -> str:
    """后台启动 ACP bridge,返回 execution_id

    bridge 启动命令:
        python3 acp_bridge.py --port {port} --bin {bin} --args '{json}'
    凭证经 envs 注入到 bridge 进程,bridge 子进程(CLI)继承这些环境变量,
    实现凭证不在命令行明文出现。

    task 参数用于从 task.params 读取模型/思考强度/上下文窗口配置(见 _get_acp_args)。
    extra_acp_args: 额外 CLI 参数(如测试时强制特定模型),透传给 _get_acp_args。
    """
    cli_bin = _get_bin(agent_type)
    acp_args = _get_acp_args(task, agent_type, extra_acp_args)
    args_json = json.dumps(acp_args, ensure_ascii=False)

    # shell 中 JSON 数组含双引号,需单引号包裹
    cmd = (
        f"python3 {BRIDGE_SCRIPT_PATH}"
        f" --port {ACP_BRIDGE_PORT}"
        f" --bin {cli_bin}"
        f" --args '{args_json}'"
    )
    execution_id = session.run_command_background(
        cmd,
        envs=credential_envs,  # 凭证注入 bridge 进程,继承给 CLI
        work_dir=BRIDGE_WORK_DIR,
    )
    logger.info(f"[{agent_type}] ACP bridge 后台启动: execution_id={execution_id}")
    return execution_id


def _wait_for_bridge_ready(
    session, execution_id: str, endpoint_url: str, endpoint_headers: dict[str, str],
    agent_type: str = "",
) -> None:
    """等待 bridge HTTP 服务就绪(健康检查轮询)

    超时(BRIDGE_STARTUP_TIMEOUT 秒)未就绪时,读取 bridge 日志辅助排查并抛错。
    """
    deadline = time.time() + BRIDGE_STARTUP_TIMEOUT
    client = httpx.Client(headers=endpoint_headers, timeout=5)

    logger.info(
        f"[{agent_type}] 等待 bridge 就绪: endpoint={endpoint_url}, "
        f"timeout={BRIDGE_STARTUP_TIMEOUT}s"
    )

    last_logs_len = 0
    try:
        while time.time() < deadline:
            # 先检查后台进程是否还活着(避免 bridge 崩溃后空等)
            logs, _ = session.get_background_logs(execution_id)
            if "ACP CLI 启动失败" in (logs or ""):
                raise RuntimeError(
                    f"ACP bridge 启动失败:CLI 进程退出。日志:\n{logs[-1000:]}"
                )
            if logs and len(logs) > last_logs_len:
                new_part = logs[last_logs_len:]
                logger.debug(f"[{agent_type}] bridge 日志增量: {new_part.rstrip()}")
                last_logs_len = len(logs)

            # 健康检查
            try:
                resp = client.get(f"{endpoint_url}/health")
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "ok":
                        logger.info(f"[{agent_type}] ACP bridge 就绪")
                        return
                    else:
                        logger.debug(f"[{agent_type}] health 200 但状态非 ok: {data}")
                else:
                    body = resp.text[:300]
                    logger.debug(f"[{agent_type}] health 返回 HTTP {resp.status_code}: {body}")
            except Exception as e:
                logger.debug(f"[{agent_type}] health 请求失败: {type(e).__name__}: {e}")

            time.sleep(BRIDGE_HEALTH_INTERVAL)

        # 超时:读取日志辅助排查
        logs, _ = session.get_background_logs(execution_id)
        raise RuntimeError(
            f"ACP bridge 启动超时({BRIDGE_STARTUP_TIMEOUT}s)。"
            f"日志:\n{(logs or '')[-1000:]}"
        )
    finally:
        client.close()


def _stop_acp_bridge(session, execution_id: str, agent_type: str = "") -> None:
    """停止 ACP bridge(中断后台命令)"""
    try:
        session.interrupt_command(execution_id)
        logger.info(f"[{agent_type}] ACP bridge 已停止: {execution_id}")
    except Exception as e:
        logger.warning(f"[{agent_type}] 停止 bridge 失败(忽略): {e}")


def _extract_bridge_error(session, execution_id: str, agent_type: str = "") -> str:
    """从 bridge 后台日志中提取 CLI 的错误/异常信息(CLI stderr 经 bridge 转发)

    bridge 的 _pump_stderr 把 CLI 的 stderr 每行加 "[cli stderr] " 前缀后
    输出到 bridge stderr,这些被沙箱后台进程日志捕获。本函数扫描日志中的
    Python traceback / Error / Exception 行,返回最后一段 traceback。

    用于 ACP -32603 Internal error 时获取真实异常(否则只有泛化的 "Internal error")。
    """
    try:
        logs, _ = session.get_background_logs(execution_id)
    except Exception:
        return ""
    if not logs:
        return ""

    lines = logs.splitlines()
    # 收集 traceback 相关行:Python 异常 traceback、Error/Exception 关键词行
    # 以及 bridge 转发的 [cli stderr] 行
    error_patterns = (
        "Traceback (most recent call last)",
        "Error:",
        "Exception:",
        "AuthError:",
        "ValueError:",
        "ImportError:",
        "ModuleNotFoundError:",
        "raise ",
        "[cli stderr]",
    )
    relevant: list[str] = []
    last_traceback_start = -1
    for i, line in enumerate(lines):
        if "Traceback (most recent call last)" in line:
            last_traceback_start = i
        if any(p in line for p in error_patterns):
            relevant.append(line)

    # 优先返回最后一个完整 traceback(从 Traceback 行到末尾)
    if last_traceback_start >= 0:
        tb_lines = lines[last_traceback_start:]
        # 限制长度,避免过长
        tb_text = "\n".join(tb_lines[:50])
        return tb_text

    # 回退:返回最后 20 行包含错误关键词的行
    if relevant:
        return "\n".join(relevant[-20:])

    return ""


# ============================================================
# bridge/session 复用缓存(性能优化:省去每轮 ~25s 的 bridge 重建链路)
# ============================================================

# task_id -> bridge 状态。
# bridge(HTTP 服务)与 CLI 进程驻留沙箱内,ACP 会话状态在 CLI 进程内,
# 因此缓存 bridge_exec_id + acp_session_id 即可跨轮次/跨 resume 直接发 prompt,
# CLI 侧对话上下文随 session 自然延续。
_bridge_cache: dict[str, dict[str, Any]] = {}
_bridge_cache_lock = threading.Lock()


def _bridge_fingerprint(acp_args: list[str], credential_envs: dict[str, str]) -> str:
    """bridge 启动配置指纹:启动参数或凭证变化时缓存失效(需重建 bridge)

    不含 HERMES_YOLO_MODE 等由 wrapper 在 prompt/会话层动态处理的开关。
    """
    keys = sorted(k for k in credential_envs if k != "HERMES_YOLO_MODE")
    return json.dumps(
        [list(acp_args), [[k, credential_envs[k]] for k in keys]],
        ensure_ascii=False, sort_keys=True,
    )


def _bridge_alive(endpoint_url: str, endpoint_headers: dict[str, str]) -> bool:
    """健康检查:缓存的 bridge 及其 CLI 进程是否仍存活"""
    try:
        with httpx.Client(headers=endpoint_headers, timeout=5) as hc:
            resp = hc.get(f"{endpoint_url}/health")
            return (
                resp.status_code == 200
                and (resp.json() or {}).get("status") == "ok"
            )
    except Exception:
        return False


def _try_reuse_bridge(
    task_id: str, session, agent_type: str, fingerprint: str,
) -> dict[str, Any] | None:
    """尝试复用本任务缓存的 bridge + ACP session

    可复用须同时满足:同一沙箱会话对象(容器未重建)、agent 类型一致、
    启动配置指纹一致、/health 通过。任一不满足则清缓存返回 None(走全新链路)。
    返回:{"bridge_exec_id", "endpoint_url", "endpoint_headers", "acp_session_id"}
    """
    with _bridge_cache_lock:
        entry = _bridge_cache.get(task_id)
        if not entry:
            return None
        if entry["session"] is not session:
            # 沙箱会话已重建,旧 bridge 随旧容器消亡,仅清缓存
            _bridge_cache.pop(task_id, None)
            return None
        if entry["agent_type"] != agent_type or entry["fingerprint"] != fingerprint:
            # 同沙箱内切换 agent 类型或启动参数/凭证变化:
            # 停旧 bridge(避免端口冲突)后重建
            _stop_acp_bridge(entry["session"], entry["bridge_exec_id"], entry["agent_type"])
            _bridge_cache.pop(task_id, None)
            return None
        reused = dict(entry)

    if not _bridge_alive(reused["endpoint_url"], reused["endpoint_headers"]):
        with _bridge_cache_lock:
            _bridge_cache.pop(task_id, None)
        logger.info(f"[task={task_id}] 缓存的 ACP bridge 已失活,重新初始化")
        return None
    return reused


def stop_task_bridge(task_id: str) -> None:
    """停止任务的 bridge 并清缓存(沙箱会话关闭/任务删除时调用)"""
    with _bridge_cache_lock:
        entry = _bridge_cache.pop(task_id, None)
    if not entry:
        return
    _stop_acp_bridge(entry["session"], entry["bridge_exec_id"], entry["agent_type"])


# ============================================================
# ACP 响应记录:完整 JSONL 落盘(供事后分析/回放)
# ============================================================


class _ACPRecorder:
    """把 ACP 通信的原始响应以 JSONL 形式落盘(不解析、不过滤)

    每个 task + round 一份文件,路径:
        {backend}/logs/acp/{task_id}_r{round_idx}_{YYYYmmdd_HHMMSS}.jsonl

    记录层级:在 ACPClient._rpc 的 SSE 循环里,每读到一行就 record_raw,
    在任何解析/过滤之前落盘。因此能完整保留:
    - 所有 data: 行(不论是否合法 JSON)
    - 非 data: 行(SSE 注释/event: 等)
    - HTTP 错误响应体(非 200 时)
    - 元信息(请求开始/结束标记)

    每行 JSONL 结构:
        {"seq": 1, "ts": "ISO 时间(本地时区)",
         "kind": "line" | "http_error" | "meta",
         "raw": "<原始文本,不截断>"}

    写入失败不影响主流程(只记 logger.warning)。
    """

    def __init__(self, task_id: str, round_idx: int, log_dir: Path | None = None):
        self.task_id = task_id
        self.round_idx = round_idx
        self._dir = log_dir or _ACP_LOG_DIR
        self._path: Path | None = None
        self._fh = None
        self._seq = 0
        self._open()

    def _open(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = f"{self.task_id}_r{self.round_idx}_{ts}.jsonl"
            self._path = self._dir / fname
            # 用 append 模式:同 task+round+秒级时间戳重启时追加,不覆盖
            self._fh = self._path.open("a", encoding="utf-8")
            logger.info(f"[acp_recorder] 记录到: {self._path}")
        except Exception as e:
            logger.warning(f"[acp_recorder] 打开文件失败(忽略): {e}")
            self._fh = None
            self._path = None

    def record_raw(self, raw: str, kind: str = "line") -> None:
        """记录一行原始文本(不解析、不截断、不过滤)

        kind:
            "line"       - SSE 流中的一行(data: / event: / 注释 / 空行等)
            "http_error" - HTTP 非 200 时的响应体
            "meta"       - 元信息(请求方法、开始/结束标记等)
        """
        if self._fh is None:
            return
        try:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "kind": kind,
                "raw": raw,
            }
            self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._fh.flush()
        except Exception as e:
            logger.warning(f"[acp_recorder] 写入失败(忽略): {e}")

    def close(self) -> None:
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
            logger.info(
                f"[acp_recorder] 关闭: {self._path}, 共 {self._seq} 条事件"
            )


# ============================================================
# ACP 事件处理:翻译为 event_bus 事件 + 落库 Conversation
# ============================================================


class _ACPCollector:
    """收集 ACP 通知事件,翻译为 event_bus 事件并落库

    ACP session/update 通知结构(见 https://agentclientprotocol.com/protocol/prompt-turn):
        {
          "method": "session/update",
          "params": {
            "sessionId": "...",
            "update": {
              "sessionUpdate": "agent_message_chunk" | "plan" | "tool_call" | ...,
              "content": { "type": "text", "text": "..." },
              "entries": [...],  # plan
              "toolCallId": "...",  # tool_call / tool_call_update
              "status": "...",  # tool_call_update
              ...
            }
          }
        }

    字段映射(sessionUpdate → event_bus 事件):
    - agent_message_chunk → thinking_delta(phase=content)
    - thought_chunk       → thinking_delta(phase=reasoning)
    - tool_call           → conversation(type=tool_call)
    - tool_call_update    → conversation(type=tool_result) (status=completed 时)
    - plan                → event_bus plan 事件
    - error               → thinking_delta(phase=error)

    迭代切段(与 react_agent 对齐):
    ACP 一次 prompt 调用内部可能包含多次 ReAct 迭代
    (thought → message → tool_call → tool_result → thought → ...)。
    本类按 tool_call 切段:每遇到 tool_call 就结束当前思考迭代
    (推送 phase=end + 落库一条 thinking),并开启新迭代(新 conv_id),
    让前端以独立流式卡片展示每次思考。
    """

    def __init__(
        self,
        task: Task,
        db: Session,
        round_idx: int,
        *,
        agent_policy: dict[str, Any] | None = None,
        agent_type: str = "",
        checkpoint_callback=None,
    ):
        self.task = task
        self.db = db
        self.round_idx = round_idx
        # 全程累积(供调用方生成 summary / 提取 plan / 日志)
        self.reasoning_full = ""
        self.content_full = ""
        self.tool_call_count = 0
        # 当前迭代状态
        self.iteration = 0
        self.current_conv_id = str(uuid.uuid4())
        self.reasoning_buf = ""
        self.content_buf = ""
        self._iter_started = False
        # tool_call 状态追踪:toolCallId -> {title, kind, tool_name, raw_input, input_text}
        # Qoder CN 的 rawInput 在 tool_call 事件里一次性给出;
        # Kimi(如 Agent 子任务)/Hermes 的参数经 tool_call_update(in_progress)增量构建,需累积 input_text。
        self._pending_tool_calls: dict[str, dict] = {}
        # 检查点评估配置(CLI agent 的迭代边界轻量评估)
        self._agent_policy = agent_policy
        self._agent_type = agent_type
        self._checkpoint_callback = checkpoint_callback
        self._interrupt_count = 0
        # 最近工具调用快照(供检查点评估使用)
        self._last_tool_intent = "(无工具调用)"
        self._last_tool_result = "(无工具结果)"
        # [perf] 首个 ACP 事件到达时间(collector 创建 ≈ prompt 发送前一刻)
        self._perf_t0 = time.perf_counter()
        self._perf_first_logged = False

    def _ensure_iter_started(self) -> None:
        """懒启动:首个 delta 到达时推送 phase=start"""
        if self._iter_started:
            return
        publish(self.task.id, "thinking_delta", {
            "conv_id": self.current_conv_id,
            "round_idx": self.round_idx,
            "role": "react_agent",
            "phase": "start",
            "delta": "",
            "iteration": self.iteration,
        })
        self._iter_started = True

    def _flush_iteration(self) -> None:
        """结束当前迭代:推送 phase=end + 落库 thinking(若有内容)"""
        if not self._iter_started:
            return
        publish(self.task.id, "thinking_delta", {
            "conv_id": self.current_conv_id,
            "round_idx": self.round_idx,
            "role": "react_agent",
            "phase": "end",
            "delta": "",
            "iteration": self.iteration,
        })
        if self.content_buf or self.reasoning_buf:
            _add_conversation(
                self.db, self.task,
                round_idx=self.round_idx,
                role="react_agent", type="thinking",
                content=self.content_buf,
                reasoning=self.reasoning_buf,
                publish_event=False,
            )
        self._iter_started = False

    def _start_new_iteration(self) -> None:
        """开新迭代:iteration+1, 新 conv_id, 清空 buf(懒启动,不立即推 start)"""
        # 检查点评估:在新迭代开始前(即上一迭代结束时),若达到评估间隔则触发
        # iteration 在此处 +1 之前表示刚结束的迭代序号
        self._maybe_trigger_checkpoint()

        self.iteration += 1
        self.current_conv_id = str(uuid.uuid4())
        self.reasoning_buf = ""
        self.content_buf = ""
        self._iter_started = False

    def _maybe_trigger_checkpoint(self) -> None:
        """检查点评估触发:每 K 个迭代边界做轻量评估

        在 _start_new_iteration 开头调用(此时 iteration 还是刚结束的迭代序号)。
        只在 user_agent 启用、配置了 agent_policy 且 allow_interrupt=true 时触发。
        前 2 个迭代不评估(给 CLI agent 启动时间)。
        """
        if not self._agent_policy or not self._checkpoint_callback:
            return
        if not self._agent_policy.get("user_agent_enabled", True):
            return  # 检查点评估是 user_agent 的能力,单 agent 模式完全关闭
        if not self._agent_policy.get("allow_interrupt", True):
            return
        if self.iteration < 2:
            return

        from app.agent_checkpoint import get_effective_interval
        effective_k = get_effective_interval(self._agent_policy, self._agent_type)
        max_interrupts = self._agent_policy.get("max_interrupts_per_round", 2)

        # iteration 此时是刚结束的迭代序号(0-based 起步,实际是已完成的迭代数)
        # 检查是否达到评估间隔
        if self.iteration % effective_k != 0:
            return
        if self._interrupt_count >= max_interrupts:
            return

        # 构造快照
        snapshot = self._build_snapshot()
        try:
            self._checkpoint_callback(self.iteration, snapshot)
        except Exception as e:
            logger.warning(
                f"[task={self.task.id}] CLI 检查点评估失败(iteration={self.iteration}, 忽略): {e}"
            )

    def _build_snapshot(self) -> dict[str, Any]:
        """构造 react_agent 快照供检查点评估"""
        return {
            "thinking_summary": self.content_buf[:500] if self.content_buf else "",
            "tool_intent": self._last_tool_intent,
            "tool_result_summary": self._last_tool_result[:500] if self._last_tool_result else "",
            "plan_status": [],  # CLI agent 的 plan 由 content_full 提取,这里暂不传
        }

    def close(self) -> None:
        """prompt 调用结束:flush 最后一段迭代"""
        self._flush_iteration()

    def __call__(self, msg: dict) -> None:
        """处理一条 ACP 通知"""
        # [perf] 首个 ACP 事件 = CLI 侧首 token 到达(前端可见响应的起点)
        if not self._perf_first_logged:
            self._perf_first_logged = True
            perf_log(
                self.task.id, "acp_first_event",
                time.perf_counter() - self._perf_t0,
                round_idx=self.round_idx, agent_type=self._agent_type,
            )
        params_preview = msg.get("params") or {}
        update_preview = params_preview.get("update") or {}
        logger.debug(
            f"[acp] {msg.get('method', '')} / "
            f"{update_preview.get('sessionUpdate', '')}"
        )

        method = msg.get("method", "")
        if method != "session/update":
            return

        params = msg.get("params") or {}
        update = params.get("update") or {}
        update_type = update.get("sessionUpdate", "")
        content = update.get("content", "")

        text = _extract_text(content)

        if update_type in (
            "thought_chunk", "thinking", "reasoning",
            "agent_thought_chunk",
        ):
            self._handle_thinking(text)
        elif update_type == "agent_message_chunk":
            self._handle_text(text)
        elif update_type == "tool_call":
            self._flush_iteration()
            self._handle_tool_call(update)
            self._start_new_iteration()
        elif update_type == "tool_call_update":
            tool_call_id = update.get("toolCallId", "")
            status = update.get("status", "")
            if status == "in_progress":
                # Kimi 的工具参数经 in_progress 增量构建,累积到 pending 缓存
                self._accumulate_tool_input(tool_call_id, text)
            elif status == "completed":
                self._handle_tool_result(tool_call_id, update)
        elif update_type == "plan":
            self._handle_plan(update.get("entries", []))
        elif update_type == "error":
            self._handle_error(text)
        else:
            logger.debug(f"[acp] 未知 update 类型: {update_type}, update={str(update)[:100]}")

    def _handle_thinking(self, delta: str) -> None:
        if not delta:
            return
        self._ensure_iter_started()
        self.reasoning_full += delta
        self.reasoning_buf += delta
        publish(self.task.id, "thinking_delta", {
            "conv_id": self.current_conv_id,
            "round_idx": self.round_idx,
            "role": "react_agent",
            "phase": "reasoning",
            "delta": delta,
            "iteration": self.iteration,
        })

    def _handle_text(self, delta: str) -> None:
        if not delta:
            return
        self._ensure_iter_started()
        self.content_full += delta
        self.content_buf += delta
        publish(self.task.id, "thinking_delta", {
            "conv_id": self.current_conv_id,
            "round_idx": self.round_idx,
            "role": "react_agent",
            "phase": "content",
            "delta": delta,
            "iteration": self.iteration,
        })

    def _handle_tool_call(self, update: dict) -> None:
        """记录工具调用(落库 conversation,推送 SSE)

        提取工具名、参数(rawInput / 增量 input_text),生成:
        - intent:人类可读一句话,末尾带 [tool_name] 标签(供前端提取)
        - detail:完整参数 JSON / 命令文本(前端等宽显示)

        content 格式:`{intent}\n{detail}`(前端 toolCallParts 按首行拆分)
        """
        self.tool_call_count += 1
        tool_call_id = update.get("toolCallId", "")
        title = update.get("title", "") or tool_call_id
        kind = update.get("kind", "other")
        raw_input = update.get("rawInput")  # Qoder 有,Kimi 无

        # 解析人类可读的工具名(优先 _meta.qoder.toolName,其次 kind 推断)
        meta = update.get("_meta") or {}
        qoder_meta = meta.get("qoder") or {}
        tool_name = qoder_meta.get("toolName", "") or self._infer_tool_name(title, kind)

        # Hermes 风格事件:无 rawInput,目标信息在 title 前缀与 locations 里
        # (read: /path、terminal: cmd、search: pattern),归一化成标准 rawInput,
        # 让 _build_tool_intent_detail 生成可读 intent(也避免 read: 被误判成 Bash)
        if not raw_input:
            locations = update.get("locations") or []
            loc_path = (
                locations[0].get("path", "")
                if locations and isinstance(locations[0], dict) else ""
            )
            if title.startswith("read: "):
                tool_name = "Read"
                raw_input = {"file_path": loc_path or title[6:].strip()}
            elif title.startswith("terminal: "):
                tool_name = "Bash"
                raw_input = {"command": title[10:].strip()}
            elif title.startswith("search: "):
                tool_name = "Grep"
                raw_input = {"pattern": title[8:].strip()}

        # 缓存,等 tool_call_update 累积输入 / completed 拿输出
        self._pending_tool_calls[tool_call_id] = {
            "title": title,
            "kind": kind,
            "tool_name": tool_name,
            "raw_input": raw_input or {},
            "input_text": "",  # Kimi 增量累积
            "conv_id": None,  # 落库后填充,completed 时用于更新
        }

        # 生成 intent + detail(Kimi 此时 input_text 为空,detail 可能为空)
        intent, detail = self._build_tool_intent_detail(tool_name, raw_input, "")

        # content: intent + "\n" + detail(前端按首行拆分)
        content = f"{intent}\n{detail}" if detail else intent

        # 记录最近工具调用(供检查点评估快照使用)
        self._last_tool_intent = intent

        conv = _add_conversation(
            self.db, self.task,
            round_idx=self.round_idx,
            role="react_agent", type="tool_call",
            content=content,
        )
        self._pending_tool_calls[tool_call_id]["conv_id"] = conv.id

    def _accumulate_tool_input(self, tool_call_id: str, text: str) -> None:
        """记录 Kimi 的 tool_call_update(in_progress)累积参数

        Kimi 的工具参数不在 tool_call 事件的 rawInput 里(无此字段),
        而是通过 tool_call_update(status=in_progress)的 content 逐步构建。
        每次 in_progress 事件包含**完整累积文本**(非 delta),直接替换。

        节流推送 conversation_update,让前端实时看到参数构建过程
        (子智能体调用可能持续数分钟,否则用户只看到"启动子智能体"无详情)。
        """
        pending = self._pending_tool_calls.get(tool_call_id)
        if pending is None or not text:
            return
        # Kimi in_progress 每次是完整累积文本,直接替换(非 += 拼接)
        pending["input_text"] = text
        self._throttled_tool_call_update(tool_call_id, pending)

    def _throttled_tool_call_update(self, tool_call_id: str, pending: dict) -> None:
        """节流推送 tool_call conversation 更新(SSE only,不写 DB)

        Kimi 一次子智能体调用可能产生 100+ 个 in_progress 事件,
        全量推送会造成 SSE 风暴。按时间(0.5s)+ 长度增量(80 字符)节流。

        DB 不写:in_progress 期间内容是临时态,completed 时才落库最终内容。
        若用户在此期间刷新,从 DB 拿到的是上一版内容(可接受)。
        """
        conv_id = pending.get("conv_id")
        if not conv_id:
            return
        now = time.time()
        last_push = pending.get("last_push_time", 0.0)
        last_len = pending.get("last_push_len", 0)
        text_len = len(pending.get("input_text", ""))
        # 节流:距上次推送 < 0.5s 且长度增长 < 80 字符时跳过
        if now - last_push < 0.5 and text_len - last_len < 80:
            return
        pending["last_push_time"] = now
        pending["last_push_len"] = text_len

        intent, detail = self._build_tool_intent_detail(
            pending.get("tool_name", ""), None, pending.get("input_text", ""),
        )
        new_content = f"{intent}\n{detail}" if detail else intent
        publish(self.task.id, "conversation_update", {
            "id": str(conv_id),
            "content": new_content,
        })

    def _handle_tool_result(self, tool_call_id: str, update: dict) -> None:
        """记录工具结果(落库 conversation,推送 SSE)

        优先用 rawOutput(Qoder 和 Kimi 完成时都有,完整不截断),
        回退到 content 文本。

        对 Kimi(参数经 in_progress 增量构建):completed 时用累积的 input_text
        更新 tool_call conversation 的 content(补全 intent + detail),
        并推 SSE 让前端刷新显示。
        """
        raw_output = update.get("rawOutput", "")
        if not raw_output:
            raw_output = _extract_text(update.get("content"))
        if not raw_output:
            raw_output = "(无输出)"

        pending = self._pending_tool_calls.get(tool_call_id, {})
        tool_name = pending.get("tool_name", "工具")
        input_text = pending.get("input_text", "")
        conv_id = pending.get("conv_id")

        # Kimi:参数在 in_progress 增量构建,tool_call 落库时 detail 为空
        # 这里用累积的 input_text 补全 tool_call conversation 的 intent + detail
        if input_text and conv_id and not pending.get("raw_input"):
            intent, detail = self._build_tool_intent_detail(
                tool_name, None, input_text,
            )
            new_content = f"{intent}\n{detail}" if detail else intent
            # 更新已落库的 tool_call conversation
            self.db.query(Conversation).filter(
                Conversation.id == conv_id,
            ).update({"content": new_content})
            self.db.commit()
            # 推 SSE 让前端刷新(用 conversation_update 事件)
            publish(self.task.id, "conversation_update", {
                "id": str(conv_id),
                "content": new_content,
            })

        _add_conversation(
            self.db, self.task,
            round_idx=self.round_idx,
            role="react_agent", type="tool_result",
            content=raw_output,
        )

        # 记录最近工具结果(供检查点评估快照使用)
        self._last_tool_result = raw_output

    @staticmethod
    def _infer_tool_name(title: str, kind: str) -> str:
        """从 title/kind 推断工具名(无 _meta.qoder.toolName 时)"""
        if kind == "execute":
            return "Bash"
        if kind == "think":
            return "Agent"
        # title 本身就是工具名(如 "Agent")或命令文本
        if title and not title.startswith("/"):
            # 短 title 通常是工具名,长 title 通常是命令(取首词)
            if len(title) <= 30 and " " not in title:
                return title
            return "Bash"
        return "工具"

    @staticmethod
    def _build_tool_intent_detail(
        tool_name: str, raw_input: dict | None, input_text: str,
    ) -> tuple[str, str]:
        """生成人类可读的 intent + 完整参数 detail

        返回 (intent, detail):
        - intent:一句话描述,末尾带 [tool_name] 标签
        - detail:完整参数 JSON 或命令文本(前端等宽显示)

        各工具类型的 intent:
        - Agent(子智能体):"子任务: {description}" 或 "子任务: {prompt摘要}"
        - Bash(命令执行):"执行: {command摘要}"
        - Read/Grep/Glob(浏览型):"读取文件 {path}"/"搜索代码: {pattern}"/"查找文件: {pattern}"
        - 其他:"调用 {tool_name}"
        """
        raw_input = raw_input or {}
        detail = ""

        if tool_name == "Agent":
            # Qoder CN: rawInput 有 description / prompt / subagent_type
            desc = raw_input.get("description", "")
            prompt = raw_input.get("prompt", "")
            sub_type = raw_input.get("subagent_type", "")

            if desc:
                intent = f"子任务: {desc}"
            elif sub_type:
                intent = f"子任务: {sub_type}"
            elif prompt:
                intent = f"子任务: {prompt[:80]}"
            else:
                intent = "启动子智能体"

            # Kimi: 无 rawInput,参数在 input_text(JSON 字符串,可能不完整)
            if not raw_input and input_text:
                k_prompt = ""
                try:
                    params = json.loads(input_text)
                    k_prompt = params.get("prompt", "") or params.get("description", "")
                except json.JSONDecodeError:
                    # in_progress 期间 JSON 不完整(无闭合引号/大括号),
                    # 用 regex 提取 prompt 字段值,让前端实时看到正在构建的子任务描述
                    k_prompt = _extract_json_string_field(input_text, "prompt")
                if k_prompt:
                    intent = f"子任务: {k_prompt[:80]}"
                detail = input_text
            elif raw_input:
                detail = json.dumps(raw_input, ensure_ascii=False, indent=2)
            intent += " [Agent]"

        elif tool_name == "Bash":
            cmd = raw_input.get("command", "") or input_text
            desc = raw_input.get("description", "")
            if cmd:
                # intent 显示命令摘要(首行,最多 100 字符)
                cmd_first = cmd.split("\n")[0][:100]
                intent = f"执行: {cmd_first}" if not desc else f"执行: {desc}"
            else:
                intent = "执行命令"
            detail = cmd or (json.dumps(raw_input, ensure_ascii=False, indent=2) if raw_input else "")
            intent += " [Bash]"

        elif tool_name == "Read":
            fp = raw_input.get("file_path", "")
            intent = f"读取文件 {fp}" if fp else "读取文件"
            detail = json.dumps(raw_input, ensure_ascii=False, indent=2) if raw_input else input_text
            intent += " [Read]"

        elif tool_name in ("Grep", "Glob"):
            pattern = raw_input.get("pattern", "")
            verb = "搜索代码" if tool_name == "Grep" else "查找文件"
            intent = f"{verb}: {pattern[:60]}" if pattern else verb
            detail = json.dumps(raw_input, ensure_ascii=False, indent=2) if raw_input else input_text
            intent += f" [{tool_name}]"

        else:
            intent = f"调用 {tool_name}"
            if raw_input:
                detail = json.dumps(raw_input, ensure_ascii=False, indent=2)
            elif input_text:
                detail = input_text
            intent += f" [{tool_name}]"

        return intent, detail

    def _handle_plan(self, entries: list) -> None:
        """处理 plan 通知,推送 plan 事件"""
        if not entries:
            return
        steps = []
        for i, e in enumerate(entries, 1):
            if not isinstance(e, dict):
                continue
            steps.append({
                "id": i,
                "text": e.get("content", ""),
                "status": e.get("status", "pending"),
            })
        if steps:
            publish(self.task.id, "plan", {
                "round_idx": self.round_idx,
                "steps": steps,
            })

    def _handle_error(self, text: str) -> None:
        if not text:
            text = "(未知错误)"
        self._ensure_iter_started()
        publish(self.task.id, "thinking_delta", {
            "conv_id": self.current_conv_id,
            "round_idx": self.round_idx,
            "role": "react_agent",
            "phase": "error",
            "delta": text,
            "iteration": self.iteration,
        })


def _extract_text(content: Any) -> str:
    """从 ACP content 字段提取文本

    content 可能是:
    - str:直接返回
    - dict:取 text / content / value 字段(即使值为空字符串也返回,
      表示"有该字段但内容为空",调用方据此过滤无内容的 chunk)
    - list:遍历取每项的 text 字段拼接

    注意:dict 含 text 字段但值为空时返回空字符串(而非 json.dumps 整个 dict)。
    Kimi 的 agent_thought_chunk 经常发送 {"type":"text","text":""} 空片段,
    若 fallback 到 json.dumps 会把 JSON 原文当文本堆积到 reasoning 里。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "content", "value", "delta"):
            val = content.get(key)
            if isinstance(val, str):
                # 即使为空也返回:text 字段存在表示这是文本内容,空=无内容
                # (不要 fallback 到 json.dumps,否则空片段会变成 JSON 字符串堆积)
                return val
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts = []
        for item in content:
            text = _extract_text(item)
            if text:
                parts.append(text)
        return "".join(parts)
    return str(content)


# 从(可能不完整的)JSON 文本中提取字符串字段值
# 用于 Kimi in_progress 期间的增量参数,JSON 可能未闭合(无结束引号/大括号)
_INCOMPLETE_JSON_FIELD_RE = re.compile(
    r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)'
)


def _extract_json_string_field(text: str, field: str) -> str:
    """从(可能不完整的)JSON 文本中提取指定字符串字段的值

    Kimi 的 tool_call_update(in_progress)每次包含完整累积文本,但 JSON 可能
    尚未闭合(如 `{"prompt": "审计 /home/user/...`)。本函数用 regex 提取
    指定字段的值,无需完整 JSON 解析。

    返回解码后的字符串值,未找到返回空字符串。
    """
    for m in _INCOMPLETE_JSON_FIELD_RE.finditer(text):
        if m.group(1) == field:
            raw_val = m.group(2)
            # 处理常见转义序列(\\n \\" \\\\ 等)
            return raw_val.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
    return ""


# ============================================================
# 对话落库辅助
# ============================================================


def _add_conversation(
    db: Session, task: Task, *, round_idx: int, role: str, type: str,
    content: str, reasoning: str | None = None,
    publish_event: bool = True,
) -> Conversation:
    """记录一条对话,可选推送 SSE 事件

    - thinking 不推 SSE(流式卡片已展示,避免重复)
    - tool_call / tool_result 推 SSE(前端对话列表实时追加)

    返回创建的 Conversation 对象(供调用方后续更新,如 Kimi 增量参数补全)。
    """
    conv = Conversation(
        task_id=task.id,
        round_idx=round_idx,
        role=role,
        type=type,
        content=content,
        reasoning=reasoning,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    if publish_event:
        publish(task.id, "conversation", {
            "id": str(conv.id),
            "round_idx": conv.round_idx,
            "role": conv.role,
            "type": conv.type,
            "content": conv.content,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
        })
    return conv


# ============================================================
# Plan 提取(复用 <plan> 格式)
# ============================================================


_PLAN_BLOCK_RE = re.compile(r"<plan>\s*(.*?)\s*</plan>", re.DOTALL)
_PLAN_LINE_RE = re.compile(
    r"^\s*(?:\d+[.、)]\s*)?(?:\[([\w_]+)\]\s*)?(.+)$"
)


def _extract_plan(content: str) -> list[dict] | None:
    """从 content 提取 <plan>...</plan> 计划清单

    无 plan 块时返回 None。
    """
    m = _PLAN_BLOCK_RE.search(content)
    if not m:
        return None
    block = m.group(1)
    steps: list[dict] = []
    for i, line in enumerate(block.split("\n"), 1):
        line = line.strip()
        if not line:
            continue
        lm = _PLAN_LINE_RE.match(line)
        if not lm:
            continue
        status = lm.group(1) or "pending"
        text = lm.group(2).strip()
        if status not in ("pending", "in_progress", "done"):
            status = "pending"
        steps.append({"id": i, "text": text, "status": status})
    return steps if steps else None


def _format_plan_reminder(plan_steps: list[dict]) -> str:
    """格式化 plan 状态,注入 prompt 让 CLI 续接进度"""
    if not plan_steps:
        return ""
    lines = ["[系统提醒] 当前计划清单状态(已完成的请标记 [done]):"]
    sym = {"pending": "○", "in_progress": "◌", "done": "✓"}
    for s in plan_steps:
        lines.append(f"{sym.get(s['status'], '○')} [{s['status']}] {s['text']}")
    return "\n".join(lines)


# ============================================================
# 项目记忆精简版加载(注入 CLI prompt)
# ============================================================


def _load_project_memory_summary(db: Session, task: Task) -> str:
    """按 task.user_id + repo_url 查 Project,返回 memory_summary(精简版,注入 prompt 用)。

    无 Project / 无 repo_url / 匿名任务 / 查询异常 → 返回 ""(不注入)。
    完整记忆已由 orchestrator 在 clone 后写入沙箱文件,这里只取精简版注入 prompt。
    """
    try:
        if task.user_id is None:
            return ""
        params = task.params or {}
        repo_url = params.get("repo_url")
        if not repo_url:
            return ""
        from app.models.project import Project
        from app.services.repo_url import normalize_repo_url

        norm = normalize_repo_url(repo_url)
        if not norm:
            return ""
        proj = (
            db.query(Project)
            .filter(
                Project.user_id == task.user_id,
                Project.repo_url_normalized == norm,
            )
            .first()
        )
        if proj is None:
            return ""
        return proj.memory_summary or ""
    except Exception as e:
        logger.warning(f"[task={task.id}] 加载项目记忆精简版失败(忽略): {e}")
        return ""


def _load_global_memory(db: Session, task: Task) -> str:
    """加载全局长期记忆段(跨项目通用经验,注入 CLI prompt)。

    委托 build_global_memory_section(与 react_agent 共用同一注入逻辑)。
    匿名任务 / 无全局记忆 / 查询异常 → 返回 ""(不注入)。
    """
    try:
        if task.user_id is None:
            return ""
        from app.services.memory_injection import build_global_memory_section

        return build_global_memory_section(db, task.user_id)
    except Exception as e:
        logger.warning(f"[task={task.id}] 加载全局记忆失败(忽略): {e}")
        return ""


# ============================================================
# Prompt 消息构造
# ============================================================


def _build_prompt_message(
    task: Task,
    round_idx: int,
    followup_query: str | None,
    repo_context: str | None,
    repo_path: str,
    previous_plan: list[dict] | None,
    memory_summary: str = "",
    global_memory: str = "",
) -> str:
    """构造发给 CLI 的完整 prompt 消息(纯指令 + 记忆注入段)

    落库展示用 _build_base_prompt(纯指令);实际发送用本函数(含记忆段)。
    """
    return _build_base_prompt(
        task, round_idx, followup_query, repo_context, repo_path, previous_plan,
    ) + _build_memory_section(memory_summary, global_memory)


def _build_base_prompt(
    task: Task,
    round_idx: int,
    followup_query: str | None,
    repo_context: str | None,
    repo_path: str,
    previous_plan: list[dict] | None,
) -> str:
    """构造发给 CLI 的纯指令部分(不含记忆注入段)

    - 第 1 轮:task.user_input + 仓库信息 + repo_context(已 clone 提示)
    - 追问轮:基于已有仓库继续,注入跨轮记忆(plan 续接)

    这部分落库到 Conversation(前端展示),记忆段单独拼接只进发送内容。
    """
    if followup_query is None:
        msg = task.user_input
        params = task.params or {}
        if params.get("repo_url"):
            msg += f"\n仓库地址: {params['repo_url']}"
        if params.get("branch"):
            msg += f"\n分支: {params['branch']}"

        if repo_context:
            msg += (
                "\n\n[仓库已预先 clone,无需你再调用 clone_repo]\n"
                + repo_context
                + "\n\n请直接基于上述仓库路径开始审计。"
            )
        elif repo_path:
            msg += f"\n仓库路径: {repo_path}"
    else:
        msg = (
            f"基于之前的审计结果,现在请针对以下问题继续检查(不需要重新 clone 仓库):\n"
        )
        if repo_path:
            msg += f"仓库路径(已 clone): {repo_path}\n\n"
        msg += f"[本轮追问]\n{followup_query}"

    if previous_plan:
        reminder = _format_plan_reminder(previous_plan)
        if reminder:
            msg += f"\n\n{reminder}"

    return msg


def _build_memory_section(memory_summary: str = "", global_memory: str = "") -> str:
    """构造记忆注入段(拼在发送给 CLI 的 prompt 末尾,不落库不展示)

    - 项目记忆精简版 + 完整记忆文件路径提示(供 CLI read_file 查阅)
    - 全局长期记忆段(跨项目通用经验)+ 完整记忆文件路径提示

    两部分都为空时返回空串。
    """
    section = ""

    # 项目记忆精简版 + 完整记忆文件路径提示(每轮注入,与 react_agent system prompt 行为一致)
    summary = (memory_summary or "").strip()
    if summary:
        section += (
            "\n\n[项目记忆摘要]\n"
            + summary
            + "\n\n完整项目记忆可 read_file /home/user/.agent_memory/project_memory.md 查阅"
        )

    # 全局长期记忆(跨项目通用经验,影响执行方式;与 react_agent system prompt 行为一致)
    # 完整文件在任务启动时已写入沙箱,超截断上限时 CLI 可 read_file 查全量
    gmem = (global_memory or "").strip()
    if gmem:
        section += (
            "\n\n" + gmem
            + "\n\n完整全局记忆可 read_file /home/user/.agent_memory/global_memory.md 查阅"
        )

    return section


# ============================================================
# 主入口:通用 ACP agent 运行流程
# ============================================================


def run_acp_agent(
    task: Task,
    db: Session,
    round_idx: int = 1,
    followup_query: str | None = None,
    repo_context: str | None = None,
    previous_plan: list[dict[str, Any]] | None = None,
    agent_type: str = "",
    agent_policy: dict[str, Any] | None = None,
    *,
    post_session_setup: Callable[[ACPClient, str, Task], None] | None = None,
    credential_env_builder: Callable[[dict[str, str], Task | None], dict[str, str]] | None = None,
    pre_bridge_hook: Callable[[Any, dict[str, str], str, Task | None], None] | None = None,
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """跑一轮 ACP CLI 执行器(通用流程)

    与 run_react_agent 签名对齐(不含 client 参数,外部 CLI 自带模型配置)。
    agent_type 决定使用哪个 CLI(qoder_cli / qoder_cli_cn / kimi_cli / hermes_cli)。

    post_session_setup: session/new 之后、prompt 之前执行的回调
        (client, session_id, task) -> None。
        kimi 用此回调调 set_config_option 设置 yolo 模式 / 模型 / 思考强度;
        qoder 不需要(启动参数已含 --yolo)。

    credential_env_builder: 动态构建凭证环境变量的回调
        (credentials: dict[str, str], task: Task | None) -> dict[str, str]。
        若提供,替代默认的 _build_credential_envs 静态映射。
        hermes 用此回调按 provider 选择动态映射 API key 环境变量名
        (如 OPENROUTER_API_KEY / ANTHROPIC_API_KEY 等),
        并按 task.params._executor_command_confirm 决定是否注入 HERMES_YOLO_MODE。

    pre_bridge_hook: bridge 启动前的沙箱准备回调
        (session, credentials, agent_type, task) -> None。
        在 _ensure_cli_env 之后、_start_acp_bridge 之前执行,
        用于向沙箱写入 CLI 需要的配置文件(如 ~/.hermes/config.yaml)。
        task 为 None 时(测试连接场景)默认 always_approve。

    返回:(results, summary, final_plan)
        results: 始终为空 list(结构化结果由 user_agent 在 done 时提取)
        summary: 本轮自然语言总结(CLI 的最终文本输出)
        final_plan: 本轮结束时的 plan 状态(从 content 提取 <plan>)
    """
    task_id_str = str(task.id)
    set_current_task(task_id_str, task.scenario)

    # [perf] CLI 执行器进入锚点
    perf_log(
        task.id, "acp_enter",
        agent_type=agent_type, round_idx=round_idx,
        followup=followup_query is not None,
    )

    # ---- 校验 agent 类型已注册 ----
    if get_agent_meta(agent_type) is None:
        raise RuntimeError(f"agent 类型未注册: {agent_type}")

    # ---- 检查沙箱模式 ----
    if settings.SANDBOX_MODE == "local":
        raise RuntimeError(
            "外部 CLI 执行器需要沙箱模式(SANDBOX_MODE=sandbox),"
            "local 模式不支持(沙箱内无 CLI)。"
        )

    # ---- 加载凭证 + 映射为环境变量 ----
    credentials = _load_credentials(db, task.user_id, agent_type)
    if credential_env_builder:
        # 动态构建(如 hermes 按 provider 选择映射不同的 API key 环境变量名)
        # 同时注入 task.params._executor_command_confirm 决定的环境变量(如 HERMES_YOLO_MODE)
        credential_envs = credential_env_builder(credentials, task)
    else:
        credential_envs = _build_credential_envs(credentials, agent_type)
    if not credential_envs:
        raise RuntimeError("凭证映射为空,无法注入环境变量(请检查 registry 配置)")

    # ---- 获取/创建沙箱会话 ----
    ctx = sandbox_tools._get_or_create_session(task_id_str)
    session = ctx["session"]
    repo_path = ctx.get("repo_path", "")

    # ---- 加载项目记忆精简版(注入 CLI prompt,完整记忆已在沙箱文件中) ----
    memory_summary = _load_project_memory_summary(db, task)
    # ---- 加载全局长期记忆(跨项目通用经验,影响执行方式) ----
    global_memory = _load_global_memory(db, task)

    # ---- 尝试复用缓存的 bridge + ACP session ----
    # 命中时跳过 ensure_cli_env/pre_bridge_hook/start_bridge/initialize/new_session
    # 合计 ~25s 的重建链路,直接发 prompt;CLI 侧会话上下文随 session 延续。
    fingerprint = _bridge_fingerprint(
        _get_acp_args(task, agent_type), credential_envs
    )
    _t0 = time.perf_counter()
    reused = _try_reuse_bridge(task_id_str, session, agent_type, fingerprint)
    perf_log(
        task.id, "acp_bridge_reuse", time.perf_counter() - _t0,
        agent_type=agent_type, hit=reused is not None,
    )

    if reused is None:
        # ---- 全新链路:准备 CLI 环境 ----
        _t0 = time.perf_counter()
        _ensure_cli_env(session, agent_type)
        perf_log(task.id, "acp_ensure_cli_env", time.perf_counter() - _t0, agent_type=agent_type)

        # ---- wrapper 层钩子:bridge 启动前的沙箱文件准备 ----
        # hermes 用此回调写入 ~/.hermes/config.yaml(模型/provider/base_url 配置)
        # codex 用此回调写入 ~/.codex/config.toml(按 task.params 决定 approval_policy)
        if pre_bridge_hook:
            _t0 = time.perf_counter()
            pre_bridge_hook(session, credentials, agent_type, task)
            perf_log(task.id, "acp_pre_bridge_hook", time.perf_counter() - _t0, agent_type=agent_type)

        # ---- 启动 ACP bridge ----
        _t0 = time.perf_counter()
        bridge_exec_id = _start_acp_bridge(session, credential_envs, task, agent_type=agent_type)
        perf_log(task.id, "acp_start_bridge", time.perf_counter() - _t0, agent_type=agent_type)
    else:
        bridge_exec_id = reused["bridge_exec_id"]

    if reused is not None:
        # 复用路径:缓存的 endpoint 已被 _bridge_alive 健康检查验证可用,
        # 跳过 get_endpoint(SDK 端口转发调用,resume 场景实测最长 ~70s)
        endpoint_url, endpoint_headers = reused["endpoint_url"], reused["endpoint_headers"]
    else:
        _t0 = time.perf_counter()
        endpoint_url, endpoint_headers = session.get_endpoint(ACP_BRIDGE_PORT)
        perf_log(task.id, "acp_get_endpoint", time.perf_counter() - _t0, agent_type=agent_type)

    try:
        if reused is None:
            _t0 = time.perf_counter()
            _wait_for_bridge_ready(
                session, bridge_exec_id, endpoint_url, endpoint_headers, agent_type
            )
            perf_log(task.id, "acp_wait_bridge_ready", time.perf_counter() - _t0, agent_type=agent_type)

        # ---- ACP 通信 ----
        recorder = _ACPRecorder(task.id, round_idx)

        # permission_handler:CLI 发来 request_permission 时(危险命令确认),
        # 推 SSE 给前端 CommandConfirmDialog,阻塞等待用户确认。
        # 仅在 CLI 关闭 yolo 模式时才会被调用(always_approve 模式下 CLI 开 yolo 不会发请求)。
        def _permission_handler(payload: dict) -> dict:
            """处理 CLI 的 request_permission:推前端确认弹窗,阻塞等用户决议"""
            command = payload.get("command", "")
            description = payload.get("description", "") or command
            perm_id = payload.get("id", "")
            tool_call_kind = payload.get("kind", "")

            # 构造 command_confirm 事件载荷(与 local 模式一致的字段结构)
            command_desc = {
                "command_id": f"acp_{perm_id}",
                "command": command,
                "tool": f"cli:{agent_type}" + (f":{tool_call_kind}" if tool_call_kind else ""),
                "reason": description,
            }
            request_command_confirm(task.id, command_desc)
            approved = wait_for_command_confirm(task.id, command_desc["command_id"])
            if approved:
                # 用户同意:返回 allow_once(不记忆,下次再问)
                return {"outcome": "selected", "option_id": "allow_once"}
            else:
                return {"outcome": "rejected"}

        client = ACPClient(endpoint_url, endpoint_headers, recorder=recorder,
                           permission_handler=_permission_handler)
        try:
            if reused is not None:
                # 复用路径:bridge/CLI 已完成 initialize,session 存于 CLI 进程内,
                # 直接用缓存的 session_id 发 prompt(对话上下文随 session 延续)
                acp_session_id = reused["acp_session_id"]
            else:
                # 握手
                _t0 = time.perf_counter()
                init_result = client.initialize()
                perf_log(task.id, "acp_initialize", time.perf_counter() - _t0, agent_type=agent_type)

                # 跳过 authenticate,直接 session/new
                # 凭证经环境变量注入后内部已自动认证,session/new 可直接成功。
                # (authenticate 在沙箱无 TTY 环境下会静默挂起)
                auth_methods = init_result.get("authMethods", []) or []
                if auth_methods:
                    logger.info(
                        f"[{agent_type}] 跳过 authenticate(凭证经环境变量自动认证),"
                        f"authMethods={[m.get('id') for m in auth_methods]}"
                    )

                # 创建会话(cwd 设为仓库路径)
                cwd = repo_path or BRIDGE_WORK_DIR
                _t0 = time.perf_counter()
                try:
                    acp_session_id = client.new_session(cwd=cwd)
                except RuntimeError as e:
                    # session/new 失败时提取 bridge 日志中的 CLI stderr(含 Python traceback),
                    # -32603 Internal error 时 JSON-RPC 响应只有泛化消息,
                    # 真实异常在 CLI 的 stderr 里(经 bridge _pump_stderr 转发)
                    bridge_detail = _extract_bridge_error(
                        session, bridge_exec_id, agent_type
                    )
                    if bridge_detail:
                        raise RuntimeError(f"{e}\n\n[CLI 日志]\n{bridge_detail}") from e
                    raise
                perf_log(task.id, "acp_new_session", time.perf_counter() - _t0, agent_type=agent_type)

                # ---- wrapper 层钩子:session/new 后的自定义设置 ----
                # kimi 在此调 set_config_option(mode=yolo) 等
                if post_session_setup:
                    _t0 = time.perf_counter()
                    post_session_setup(client, acp_session_id, task)
                    perf_log(task.id, "acp_post_session_setup", time.perf_counter() - _t0, agent_type=agent_type)

                # ---- 写入复用缓存(bridge 不再每轮停止,供后续轮次/resume 复用) ----
                with _bridge_cache_lock:
                    _bridge_cache[task_id_str] = {
                        "session": session,
                        "agent_type": agent_type,
                        "bridge_exec_id": bridge_exec_id,
                        "endpoint_url": endpoint_url,
                        "endpoint_headers": endpoint_headers,
                        "acp_session_id": acp_session_id,
                        "fingerprint": fingerprint,
                    }

            # ---- 构造 prompt 消息 ----
            # 落库只存纯指令(前端展示不含记忆注入段);实际发送拼上记忆段
            base_msg = _build_base_prompt(
                task, round_idx, followup_query, repo_context, repo_path, previous_plan,
            )

            _add_conversation(
                db, task, round_idx=round_idx,
                role="user", type="question",
                content=base_msg,
            )

            user_msg = base_msg + _build_memory_section(memory_summary, global_memory)

            # ---- 检查点评估回调(CLI agent 的迭代边界轻量评估) ----
            # 在 _ACPCollector 的 _start_new_iteration 中被调用,
            # 评估结果若 interrupt=true 会写入中断队列,当前 prompt 结束后检查
            def _checkpoint_callback(iteration: int, snapshot: dict[str, Any]) -> None:
                if not agent_policy or not agent_policy.get("user_agent_enabled", True):
                    return  # user_agent 已禁用(单 agent 模式),不做检查点评估
                if not agent_policy.get("allow_interrupt", True):
                    return
                from app.agent_checkpoint import run_user_agent_checkpoint
                from app.agent_interrupt import (
                    get_interrupt_count,
                    increment_interrupt_count,
                    push_interrupt,
                )
                max_interrupts = agent_policy.get("max_interrupts_per_round", 2)
                current_count = get_interrupt_count(task.id, round_idx)
                if current_count >= max_interrupts:
                    return
                try:
                    checkpoint_result = run_user_agent_checkpoint(
                        task, db, round_idx, iteration, snapshot, None,
                    )
                    if checkpoint_result.get("interrupt"):
                        push_interrupt(
                            task.id,
                            query=checkpoint_result["query"],
                            reason=checkpoint_result["reason"],
                            iteration=iteration,
                        )
                        increment_interrupt_count(task.id, round_idx)
                        logger.info(
                            f"[task={task.id}] CLI 检查点评估打断(iteration={iteration}): "
                            f"{checkpoint_result.get('reason', '')[:100]}"
                        )
                except Exception as e:
                    logger.warning(
                        f"[task={task.id}] CLI 检查点评估回调失败(iteration={iteration}, 忽略): {e}"
                    )

            # ---- 流式发送 prompt(支持软中断:当前 prompt 结束后检查中断队列) ----
            collector = _ACPCollector(
                task, db, round_idx,
                agent_policy=agent_policy,
                agent_type=agent_type,
                checkpoint_callback=_checkpoint_callback if agent_policy else None,
            )

            try:
                # 软中断循环:当前 prompt 结束后检查中断队列,
                # 若有中断则用追问指令发起新 prompt(同 session,CLI 保留对话历史)
                current_msg = user_msg
                while True:
                    # [perf] prompt 发送锚点(CLI 侧 TTFT 由 acp_first_event 记录)
                    perf_log(task.id, "acp_prompt_send", round_idx=round_idx, msg_chars=len(current_msg))
                    result = client.prompt(
                        acp_session_id,
                        [{"type": "text", "text": current_msg}],
                        on_event=collector,
                    )

                    # 检查中断队列(软中断:不取消当前 prompt,等它结束后再追问)
                    from app.agent_interrupt import drain_interrupts
                    pending_interrupts = drain_interrupts(task.id)
                    if not pending_interrupts:
                        break  # 无中断,正常结束

                    # 有中断:构造追问 prompt,继续下一轮 prompt
                    interrupt_parts = []
                    for it in pending_interrupts:
                        query = (it.get("query") or "").strip()
                        if query:
                            reason = (it.get("reason") or "").strip()
                            it_text = f"[方向纠正:{reason}]\n{query}" if reason else query
                            interrupt_parts.append(it_text)

                    if not interrupt_parts:
                        break  # 中断内容为空,正常结束

                    interrupt_msg = (
                        "[user_agent 检查点评估:方向纠正]\n"
                        "user_agent 在观察你的执行过程后,认为当前方向需要调整。"
                        "请把以下纠正指令纳入当前任务,调整检查方向继续执行:\n\n"
                        + "\n\n".join(interrupt_parts)
                    )
                    logger.info(
                        f"[task={task.id}] CLI 软中断:用追问指令发起新 prompt "
                        f"({len(pending_interrupts)} 条中断)"
                    )
                    _add_conversation(
                        db, task, round_idx=round_idx,
                        role="user_agent", type="evaluation",
                        content=f"[检查点中断] {interrupt_msg[:200]}",
                    )
                    current_msg = interrupt_msg

            except Exception as e:
                logger.exception(f"[task={task.id}] ACP prompt 失败 ({agent_type})")
                # 连接层失败(bridge/CLI 已死)时清缓存,下次走全新链路;
                # ACP 业务错误保留缓存(session 仍有效)。
                if isinstance(e, (httpx.HTTPError, ConnectionError)):
                    with _bridge_cache_lock:
                        _bridge_cache.pop(task_id_str, None)
                publish(task.id, "thinking_delta", {
                    "conv_id": collector.current_conv_id,
                    "round_idx": round_idx,
                    "role": "react_agent",
                    "phase": "error",
                    "delta": f"[CLI 调用失败: {e}]",
                    "iteration": collector.iteration,
                })
                raise
            finally:
                recorder.close()
                collector.close()

        finally:
            client.close()

    finally:
        # bridge 保持运行(写入/已在 _bridge_cache),供后续轮次/resume 复用;
        # 仅在初始化阶段失败且未入缓存时停掉,避免残留坏进程。
        with _bridge_cache_lock:
            _cached = _bridge_cache.get(task_id_str) is not None
        if not _cached:
            _stop_acp_bridge(session, bridge_exec_id, agent_type)

    # ---- 提取 summary 和 plan ----
    summary = collector.content_full or ""
    if not summary:
        summary = f"第 {round_idx} 轮完成({agent_type},{collector.tool_call_count} 次工具调用)"

    current_plan: list[dict] = [dict(s) for s in (previous_plan or [])]
    extracted = _extract_plan(collector.content_full)
    if extracted:
        current_plan = extracted
        publish(task.id, "plan", {
            "round_idx": round_idx,
            "steps": current_plan,
        })

    logger.info(
        f"[task={task.id}] {agent_type} 第 {round_idx} 轮完成: "
        f"content={len(collector.content_full)}字符, "
        f"reasoning={len(collector.reasoning_full)}字符, "
        f"tool_calls={collector.tool_call_count}"
    )

    return [], summary, current_plan


# ============================================================
# 通用流式凭证测试
# ============================================================


def test_credential_streaming(
    db: Session,
    user_id,
    agent_type: str,
    *,
    post_session_setup: Callable[[ACPClient, str, Task | None], None] | None = None,
    test_acp_args: list[str] | None = None,
    credential_env_builder: Callable[[dict[str, str], Task | None], dict[str, str]] | None = None,
    pre_bridge_hook: Callable[[Any, dict[str, str], str, Task | None], None] | None = None,
) -> Generator[dict, None, None]:
    """流式版测试凭证:yield SSE 事件 dict(供路由层格式化为 SSE)

    与各 wrapper 的 test_credential 验证流程一致,但把各阶段进度、思考增量、
    回答增量实时 yield 出去,前端可流式显示。

    事件类型(yield 的 dict):
        {"type": "stage",    "data": {"stage": "...", "message": "..."}}
        {"type": "thinking", "data": {"delta": "思考片段"}}
        {"type": "content",  "data": {"delta": "回答片段"}}
        {"type": "done",     "data": {"ok": bool, "message": "..."}}
        {"type": "error",    "data": {"ok": False, "message": "..."}}

    post_session_setup: 测试场景的 session/new 后回调(kimi 用此设置 yolo 模式)
    test_acp_args: 测试时额外的 CLI 参数(qoder 用 ["--model","Qwen3.6-Flash",...])
    credential_env_builder: 动态构建凭证环境变量的回调(hermes 用此按 provider 映射);
        task=None(测试场景),wrapper 应默认 always_approve。
    pre_bridge_hook: bridge 启动前的沙箱准备回调(hermes 用此写 ~/.hermes/config.yaml);
        task=None(测试场景),wrapper 应默认 always_approve。

    done/error 为终止事件,生成器在此后结束。
    """
    def stage(stage_id: str, message: str) -> dict:
        return {"type": "stage", "data": {"stage": stage_id, "message": message}}

    def done(ok: bool, message: str) -> dict:
        return {"type": "done", "data": {"ok": ok, "message": message}}

    # ---- 加载凭证 ----
    try:
        credentials = _load_credentials(db, user_id, agent_type)
    except RuntimeError as e:
        yield done(False, f"凭证加载失败: {e}")
        return

    if credential_env_builder:
        credential_envs = credential_env_builder(credentials, None)
    else:
        credential_envs = _build_credential_envs(credentials, agent_type)
    if not credential_envs:
        yield done(False, "凭证映射为空(请检查 registry 配置)")
        return

    if settings.SANDBOX_MODE == "local":
        yield done(False, "测试连接需要 SANDBOX_MODE=sandbox(local 模式无 CLI)")
        return

    # ---- 创建临时沙箱 ----
    from app.sandbox.client import create_sandbox

    yield stage("creating_sandbox", "创建临时沙箱...")
    logger.info(f"[{agent_type}_test] 开始流式测试:创建临时沙箱")
    session = create_sandbox()
    bridge_exec_id: str | None = None

    try:
        # ---- 准备 CLI 环境 ----
        yield stage("cli_env", "准备 CLI 环境(写入 bridge 脚本 + 检查 CLI)...")
        try:
            _ensure_cli_env(session, agent_type)
            logger.info(f"[{agent_type}_test] CLI 环境就绪")
        except RuntimeError as e:
            yield done(False, f"CLI 环境准备失败: {e}")
            return

        # 清理可能残留的旧登录态
        session.run_command("rm -rf ~/.qoder ~/.qoder-cn ~/.kimi-code ~/.hermes ~/.codex 2>/dev/null", timeout=5)

        # ---- wrapper 层钩子:bridge 启动前的沙箱文件准备 ----
        # hermes 用此回调写入 ~/.hermes/config.yaml(模型/provider/base_url 配置)
        # codex 用此回调写入 ~/.codex/config.toml(模型/审批策略配置)
        # task=None(测试场景):wrapper 默认 always_approve
        if pre_bridge_hook:
            try:
                pre_bridge_hook(session, credentials, agent_type, None)
            except Exception as e:
                yield done(False, f"沙箱配置文件准备失败: {e}")
                return

        # ---- 启动 ACP bridge ----
        yield stage("bridge_start", "启动 ACP bridge(注入凭证,等待就绪)...")
        try:
            bridge_exec_id = _start_acp_bridge(
                session, credential_envs,
                agent_type=agent_type,
                extra_acp_args=test_acp_args,
            )
            endpoint_url, endpoint_headers = session.get_endpoint(ACP_BRIDGE_PORT)
            _wait_for_bridge_ready(
                session, bridge_exec_id, endpoint_url, endpoint_headers, agent_type
            )
            yield stage("bridge_ready", "ACP bridge 就绪")
        except RuntimeError as e:
            err_msg = str(e)
            if any(kw in err_msg.lower() for kw in ("auth", "unauthorized", "token", "401", "credential")):
                yield done(False, f"凭证认证失败: {err_msg}")
            else:
                yield done(False, f"ACP bridge 启动失败: {err_msg}")
            return

        # ---- ACP 握手 ----
        yield stage("acp_init", "ACP 握手(initialize)...")
        client = ACPClient(endpoint_url, endpoint_headers)
        try:
            result = client.initialize()
            protocol_version = result.get("protocolVersion", "?")
            auth_methods = result.get("authMethods", []) or []
            logger.info(
                f"[{agent_type}_test] ACP 握手成功: protocolVersion={protocol_version}, "
                f"authMethods={[m.get('id') for m in auth_methods]}"
            )
            if auth_methods:
                logger.info(
                    f"[{agent_type}_test] 跳过 authenticate(凭证经环境变量自动认证),"
                    f"authMethods={[m.get('id') for m in auth_methods]}"
                )

            # ---- 创建会话 ----
            yield stage("session_new", "创建 ACP 会话...")
            try:
                acp_session_id = client.new_session(cwd="/tmp")
                logger.info(
                    f"[{agent_type}_test] session/new 返回: sessionId={acp_session_id}"
                )
            except httpx.TimeoutException:
                yield done(False, "创建会话超时(请检查网络或凭证有效性)")
                return
            except RuntimeError as e:
                err_msg = str(e)
                low = err_msg.lower()
                # 提取 bridge 日志中的 CLI stderr(含 Python traceback),
                # -32603 Internal error 时 JSON-RPC 响应只有泛化消息,
                # 真实异常在 CLI 的 stderr 里(经 bridge _pump_stderr 转发)
                bridge_detail = ""
                if bridge_exec_id:
                    bridge_detail = _extract_bridge_error(
                        session, bridge_exec_id, agent_type
                    )
                if bridge_detail:
                    err_msg = f"{err_msg}\n\n[CLI 日志]\n{bridge_detail}"

                if "auth" in low or "authentication required" in low:
                    yield done(False, f"session/new 失败:CLI 要求 authenticate 但不支持非交互式认证。错误: {err_msg}")
                elif any(kw in low for kw in ("unauthorized", "token", "401")):
                    yield done(False, f"凭证认证失败: {err_msg}")
                else:
                    yield done(False, f"创建会话失败: {err_msg}")
                return

            # ---- wrapper 层钩子:session/new 后的自定义设置 ----
            if post_session_setup:
                try:
                    post_session_setup(client, acp_session_id, None)
                except Exception as e:
                    yield done(False, f"会话配置失败: {e}")
                    return

            # ---- 发送测试 prompt + 流式接收 ----
            yield stage("prompt", "发送测试 prompt「你好」,等待模型响应(流式)...")
            test_prompt = [{"type": "text", "text": "你好"}]
            logger.info(
                f"[{agent_type}_test] 发送 session/prompt: "
                f"sessionId={acp_session_id}, prompt={json.dumps(test_prompt, ensure_ascii=False)}"
            )

            content_full: list[str] = []
            # MiniMax 等厂商 thinking=only / reasoningSplit 时,模型回复全部走
            # reasoning_content,Kimi CLI 转成 thought_chunk/reasoning 通知发出,
            # 永远不发 agent_message_chunk。这里累积 thinking,reply 为空时回退使用,
            # 避免被误判为"模型未响应"。
            reasoning_full: list[str] = []
            event_q: queue.Queue = queue.Queue()
            _SENTINEL = object()

            def _streaming_on_event(msg: dict) -> None:
                """on_event 回调:把 ACP 通知增量放入 queue 供生成器消费"""
                if msg.get("method") != "session/update":
                    return
                params = msg.get("params") or {}
                update = params.get("update") or {}
                update_type = update.get("sessionUpdate", "")
                content = update.get("content", "")
                text = _extract_text(content)
                if not text:
                    return
                if update_type in ("thought_chunk", "thinking", "reasoning"):
                    event_q.put(("thinking", text))
                    reasoning_full.append(text)
                elif update_type == "agent_message_chunk":
                    event_q.put(("content", text))
                    content_full.append(text)
                elif update_type == "error":
                    event_q.put(("error", text))

            prompt_error: list = []

            def _run_prompt() -> None:
                try:
                    client.prompt(
                        acp_session_id,
                        test_prompt,
                        on_event=_streaming_on_event,
                        timeout=60,
                    )
                except Exception as e:
                    prompt_error.append(e)
                finally:
                    event_q.put(_SENTINEL)

            prompt_thread = threading.Thread(
                target=_run_prompt, name=f"acp-test-{agent_type}", daemon=True
            )
            prompt_thread.start()

            while True:
                try:
                    item = event_q.get(timeout=120)
                except queue.Empty:
                    yield done(False, "模型响应超时(120s,请检查网络或配额)")
                    return
                if item is _SENTINEL:
                    break
                evt_type, text = item
                if evt_type == "thinking":
                    yield {"type": "thinking", "data": {"delta": text}}
                elif evt_type == "content":
                    yield {"type": "content", "data": {"delta": text}}
                elif evt_type == "error":
                    yield done(False, f"模型返回错误: {text}")
                    return

            if prompt_error:
                e = prompt_error[0]
                err_msg = str(e)
                low = err_msg.lower()
                if any(kw in low for kw in ("quota", "credit", "limit", "余额", "配额",
                                            "pricing", "pricingurl")):
                    yield done(False, f"账户配额不足,请前往充值后重试。错误详情: {err_msg}")
                elif any(kw in low for kw in ("auth", "unauthorized", "token", "401")):
                    yield done(False, f"凭证认证失败: {err_msg}")
                elif "timeout" in low:
                    yield done(False, "模型响应超时(60s,请检查网络或配额)")
                else:
                    yield done(False, f"模型响应测试失败: {err_msg}")
                return

            # 自动拆分 <think>...</think>:某些模型/端点(Kimi CLI openai provider
            # 转发的 MiniMax、DeepSeek-R1 开源版等)把思考内嵌在 content 里,
            # 而非 reasoning_content 字段。Kimi CLI 不解析该标签,原样转发为
            # agent_message_chunk。这里在最终回复里拆分:标签内 → reasoning,
            # 标签外 → content,让前端看到干净的回复而非带标签的原文。
            content_joined = "".join(content_full)
            think_matches = re.findall(r"<think>(.*?)</think>", content_joined, re.DOTALL)
            if think_matches:
                extracted_reasoning = "".join(think_matches).strip()
                extracted_content = re.sub(
                    r"<think>.*?</think>", "", content_joined, flags=re.DOTALL
                ).strip()
                if extracted_reasoning:
                    reasoning_full.append(extracted_reasoning)
                reply = extracted_content
            else:
                reply = content_joined.strip()

            reasoning_text = "".join(reasoning_full).strip()

            if reply:
                # 有正式回复:正常成功
                preview = reply[:80] + ("..." if len(reply) > 80 else "")
                logger.info(f"[{agent_type}_test] 模型响应: {preview}")
                yield done(
                    True,
                    f"连接成功(ACP 协议版本 {protocol_version}),模型响应: {preview}",
                )
            elif reasoning_text:
                # 无正式回复但有思考内容:模型确实响应了(思考已在流式过程展示),
                # 判为成功,但 reply 为空,不把思考混入回复。
                logger.info(f"[{agent_type}_test] 模型仅返回思考内容(reply 为空)")
                yield done(
                    True,
                    f"连接成功(ACP 协议版本 {protocol_version}),模型仅返回思考内容,"
                    "未给出正式回复(思考过程已在上方展示)。",
                )
            else:
                # 既无回复也无思考:真正未响应
                # Kimi CLI 默认用 kimi provider 类型(Moonshot 专用协议,发送顶层 thinking 参数),
                # 非 Moonshot 端点(MiniMax/DeepSeek/DashScope 等)可能不识别该参数而拒绝或静默无响应。
                # 提示用户在「智能体配置」中将「供应商协议类型」改为 openai 后重试。
                yield done(
                    False,
                    "session/new 成功,但模型未响应(请检查配额或网络)。"
                    "若使用 MiniMax/DeepSeek/阿里云等非 Moonshot 端点,"
                    "请在「智能体配置」中将「供应商协议类型」改为 openai 后重试。"
                )

        except RuntimeError as e:
            err_msg = str(e)
            if any(kw in err_msg.lower() for kw in ("auth", "unauthorized", "token", "401")):
                yield done(False, f"凭证认证失败: {err_msg}")
            else:
                yield done(False, f"ACP 握手失败: {err_msg}")
            return
        finally:
            client.close()

    except Exception as e:
        logger.exception(f"[{agent_type}_test] 流式测试过程异常")
        yield {"type": "error", "data": {"ok": False, "message": f"测试异常: {e}"}}
    finally:
        if bridge_exec_id:
            _stop_acp_bridge(session, bridge_exec_id, agent_type)
        try:
            session.close()
        except Exception as e:
            logger.info(f"[{agent_type}_test] 关闭沙箱失败(忽略): {e}")
