"""trae_cli_agent:基于 TRAE CLI + ACP 协议的执行智能体

在沙箱内启动 TRAE CLI 的 ACP(Agent Client Protocol)服务,通过 HTTP 桥接
(acp_bridge.py)与后端通信。模型配置在沙箱内的 trae_cli.yaml 中指定,
后端不直接管理 LLM 调用 —— TRAE CLI 内部自主完成 ReAct 循环(思考→工具→观察)。

工作流程:
1. 复用 sandbox_tools 的沙箱会话(orchestrator 已预 clone 仓库)
2. 加载用户 PAT,生成 trae_cli.yaml(认证 + 模型配置),写入沙箱
3. 将 acp_bridge.py 写入沙箱
4. 后台启动 acp_bridge.py(监听端口 ACP_BRIDGE_PORT)
5. 通过 get_endpoint(ACP_BRIDGE_PORT) 获取转发地址 + headers
6. ACP 客户端:initialize → session/new → session/prompt
7. 流式接收 session/update 通知,翻译为 event_bus 事件(thinking_delta /
   conversation),推送前端,实现与内置 react_agent 一致的流式体验
8. 收集最终 summary,提取 plan,返回 (results, summary, plan)

与内置 react_agent 的差异:
- TRAE CLI 自主管理 ReAct 循环(工具调用/观察),后端只发一个 prompt
- 模型配置在 trae_cli.yaml 中,不使用 task.llm_config_id
- PAT 认证:用户在"账户设置"配置 TRAE CLI PAT,加密存储,运行时注入沙箱
"""
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.event_bus import publish
from app.models.task import Conversation, Task
from app.models.user import User
from app.security import decrypt_secret
from app.tools import sandbox_tools
from app.tools.schema import set_current_task

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

# ACP bridge 监听端口(沙箱内)
ACP_BRIDGE_PORT = 8088

# 沙箱内文件路径
BRIDGE_SCRIPT_PATH = "/home/user/.trae/acp_bridge.py"
TRAE_CLI_CONFIG_PATH = "/home/user/.trae/trae_cli.yaml"
TRAE_CLI_WORK_DIR = "/home/user"

# bridge 启动超时(秒):等待 traecli 进程就绪 + HTTP 服务监听
BRIDGE_STARTUP_TIMEOUT = 30
BRIDGE_HEALTH_INTERVAL = 1.0

# ACP 协议版本
ACP_PROTOCOL_VERSION = "0.1"

# 本地 acp_bridge.py 源文件路径(用于写入沙箱)
_BRIDGE_SOURCE = Path(__file__).parent / "acp_bridge.py"


# ============================================================
# ACP HTTP 客户端
# ============================================================


class ACPClient:
    """ACP HTTP 客户端:通过 HTTP/SSE 与沙箱内的 acp_bridge 通信

    桥接服务将 HTTP 请求转换为 traecli 的 stdio ACP(JSON-RPC over
    newline-delimited JSON),响应以 SSE 流式返回。

    使用方式:
        client = ACPClient(endpoint_url, endpoint_headers)
        client.initialize()
        session_id = client.new_session(cwd="/home/user/repo")
        result = client.prompt(session_id, messages, on_event=callback)
        client.close()
    """

    def __init__(self, base_url: str, headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}
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
    ) -> dict:
        """发送 JSON-RPC 请求,可选流式处理通知,返回最终响应 result

        桥接服务对所有 POST /rpc 返回 SSE(text/event-stream):
        - 通知(method 字段,无 id):中间事件,通过 on_event 回调处理
        - 最终响应(有 id 匹配):流结束标志,返回其 result

        on_event: 接收通知 dict 的回调函数。None 表示不处理中间事件
        (用于 initialize / session/new 等快速调用)。
        """
        request_id = request.get("id")

        with self._client.stream(
            "POST",
            f"{self.base_url}/rpc",
            json=request,
        ) as response:
            if response.status_code != 200:
                response.read()
                body = response.text[:500]
                raise RuntimeError(
                    f"ACP 请求失败: HTTP {response.status_code}, body={body}"
                )

            final_result: dict | None = None

            for line in response.iter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue

                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    logger.debug(f"[acp] 非 JSON SSE 行,跳过: {data[:100]}")
                    continue

                # 错误响应(JSON-RPC error)
                if "error" in msg and msg.get("id") == request_id:
                    err = msg["error"]
                    raise RuntimeError(
                        f"ACP 错误 {err.get('code')}: {err.get('message')}"
                    )

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

    def initialize(self) -> dict:
        """ACP 握手:交换协议版本和能力"""
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

    def new_session(self, cwd: str | None = None) -> str:
        """创建 ACP 会话,返回 session_id"""
        params: dict[str, Any] = {}
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

    def prompt(
        self,
        session_id: str,
        messages: list[dict],
        on_event=None,
    ) -> dict:
        """发送 prompt,流式处理通知,返回最终结果

        on_event: 接收 session/update 通知的回调
        """
        return self._rpc({
            "jsonrpc": "2.0",
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "messages": messages,
            },
            "id": self._next_id(),
        }, on_event=on_event)

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
        """健康检查:bridge + traecli 是否存活"""
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
# 沙箱环境准备:trae_cli.yaml + bridge 脚本
# ============================================================


def _load_trae_cli_pat(db: Session, user_id) -> str:
    """从 User 表加载解密后的 TRAE CLI PAT

    空串表示未配置。trae_cli executor 需要 PAT 才能认证,未配置时抛错。
    """
    if user_id is None:
        raise RuntimeError("TRAE CLI 执行器需要登录用户(匿名任务不支持)")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise RuntimeError(f"用户不存在: {user_id}")
    if not user.trae_cli_pat:
        raise RuntimeError(
            "未配置 TRAE CLI PAT。请在「账户设置」中配置 TRAE CLI 个人访问令牌。"
        )
    try:
        return decrypt_secret(user.trae_cli_pat)
    except Exception as e:
        raise RuntimeError(f"TRAE CLI PAT 解密失败: {e}") from e


def _generate_trae_cli_config(pat: str) -> str:
    """生成 trae_cli.yaml 配置内容

    包含 PAT 认证信息。具体配置项可能因 TRAE CLI 版本而异,
    这里生成一个通用配置,同时通过环境变量 TRAE_CLI_PAT 注入(双保险)。
    """
    return f"""# Auto-generated by AgentPair — DO NOT EDIT
# TRAE CLI configuration (trae_cli.yaml)
# Generated at: {time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}

auth:
  pat: "{pat}"
"""


def _write_bridge_script(session) -> None:
    """将 acp_bridge.py 写入沙箱(从本地源文件读取)"""
    if not _BRIDGE_SOURCE.exists():
        raise RuntimeError(f"acp_bridge.py 源文件不存在: {_BRIDGE_SOURCE}")
    content = _BRIDGE_SOURCE.read_text(encoding="utf-8")
    session.write_file(BRIDGE_SCRIPT_PATH, content)


def _ensure_trae_cli_env(session, pat: str) -> None:
    """准备沙箱内 TRAE CLI 运行环境

    1. 创建 ~/.trae 目录
    2. 写入 trae_cli.yaml(PAT 注入)
    3. 写入 acp_bridge.py
    4. 检查 traecli 是否可用,不可用则尝试安装
    """
    # 创建配置目录
    session.run_command(f"mkdir -p {Path(TRAE_CLI_CONFIG_PATH).parent.as_posix()}")

    # 写入配置文件
    config_content = _generate_trae_cli_config(pat)
    session.write_file(TRAE_CLI_CONFIG_PATH, config_content)

    # 写入 bridge 脚本
    _write_bridge_script(session)

    # 检查 traecli 是否可用
    traecli_bin = settings.TRAE_CLI_BIN
    check_cmd = f"command -v {traecli_bin} || which {traecli_bin} 2>/dev/null"
    result = session.run_command(check_cmd, timeout=10)
    if not result.strip():
        # traecli 未安装,尝试安装
        install_cmd = settings.TRAE_CLI_INSTALL_CMD
        if not install_cmd:
            raise RuntimeError(
                f"沙箱内未找到 {traecli_bin},且 TRAE_CLI_INSTALL_CMD 为空。"
                f"请在沙箱镜像中预装 TRAE CLI,或配置 TRAE_CLI_INSTALL_CMD。"
            )
        logger.info(f"[trae_cli] traecli 未安装,执行: {install_cmd}")
        install_result = session.run_command(install_cmd, timeout=120, check=False)
        # 再次检查
        result = session.run_command(check_cmd, timeout=10)
        if not result.strip():
            raise RuntimeError(
                f"TRAE CLI 安装失败({install_cmd})。"
                f"安装日志: {install_result[:500]}"
            )

    logger.info(f"[trae_cli] 环境就绪: {traecli_bin} 可用,配置已写入 {TRAE_CLI_CONFIG_PATH}")


# ============================================================
# ACP bridge 生命周期管理
# ============================================================


def _start_acp_bridge(session) -> str:
    """后台启动 ACP bridge,返回 execution_id

    bridge 启动命令:python3 acp_bridge.py --port {port} --config {config} --traecli-bin {bin}
    通过 run_command_background 非阻塞启动,PAT 通过环境变量注入(双保险)。
    """
    cmd = (
        f"python3 {BRIDGE_SCRIPT_PATH}"
        f" --port {ACP_BRIDGE_PORT}"
        f" --config {TRAE_CLI_CONFIG_PATH}"
        f" --traecli-bin {settings.TRAE_CLI_BIN}"
    )
    execution_id = session.run_command_background(
        cmd,
        envs={"TRAE_CLI_PAT": ""},  # PAT 已在 yaml 中,env 留空避免覆盖
        work_dir=TRAE_CLI_WORK_DIR,
    )
    logger.info(f"[trae_cli] ACP bridge 后台启动: execution_id={execution_id}")
    return execution_id


def _wait_for_bridge_ready(
    session, execution_id: str, endpoint_url: str, endpoint_headers: dict[str, str]
) -> None:
    """等待 bridge HTTP 服务就绪(健康检查轮询)

    超时(BRIDGE_STARTUP_TIMEOUT 秒)未就绪时,读取 bridge 日志辅助排查并抛错。
    """
    deadline = time.time() + BRIDGE_STARTUP_TIMEOUT
    client = httpx.Client(headers=endpoint_headers, timeout=5)

    try:
        while time.time() < deadline:
            # 先检查后台进程是否还活着(避免 bridge 崩溃后空等)
            logs, _ = session.get_background_logs(execution_id)
            if "traecli 启动失败" in (logs or ""):
                raise RuntimeError(
                    f"ACP bridge 启动失败:traecli 进程退出。日志:\n{logs[-1000:]}"
                )

            # 健康检查
            try:
                resp = client.get(f"{endpoint_url}/health")
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "ok":
                        logger.info("[trae_cli] ACP bridge 就绪")
                        return
            except Exception:
                pass

            time.sleep(BRIDGE_HEALTH_INTERVAL)

        # 超时:读取日志辅助排查
        logs, _ = session.get_background_logs(execution_id)
        raise RuntimeError(
            f"ACP bridge 启动超时({BRIDGE_STARTUP_TIMEOUT}s)。"
            f"日志:\n{(logs or '')[-1000:]}"
        )
    finally:
        client.close()


def _stop_acp_bridge(session, execution_id: str) -> None:
    """停止 ACP bridge(中断后台命令)"""
    try:
        session.interrupt_command(execution_id)
        logger.info(f"[trae_cli] ACP bridge 已停止: {execution_id}")
    except Exception as e:
        logger.warning(f"[trae_cli] 停止 bridge 失败(忽略): {e}")


# ============================================================
# ACP 事件处理:翻译为 event_bus 事件 + 落库 Conversation
# ============================================================


class _ACPCollector:
    """收集 ACP 通知事件,翻译为 event_bus 事件并落库

    在 prompt 流式过程中作为 on_event 回调,逐条处理 session/update 通知:
    - thinking/reasoning → thinking_delta(phase=reasoning)
    - text/content → thinking_delta(phase=content)
    - tool_call/tool_use → conversation(type=tool_call)
    - tool_result/tool_response → conversation(type=tool_result)
    - error → thinking_delta(phase=error)

    同时累积 reasoning / content 文本,供调用方生成 summary 和落库 thinking。
    """

    def __init__(self, task: Task, db: Session, round_idx: int, conv_id: str):
        self.task = task
        self.db = db
        self.round_idx = round_idx
        self.conv_id = conv_id
        self.reasoning_full = ""
        self.content_full = ""
        self.tool_call_count = 0

    def __call__(self, msg: dict) -> None:
        """处理一条 ACP 通知"""
        method = msg.get("method", "")
        if method != "session/update":
            # 其他通知(如 initialized)忽略
            return

        params = msg.get("params") or {}
        update_type = params.get("type", "")
        content = params.get("content", "")

        # 提取文本(content 可能是 str / dict / list)
        text = _extract_text(content)

        if update_type in ("thinking", "reasoning"):
            self._handle_thinking(text)
        elif update_type in ("text", "content", "assistant"):
            self._handle_text(text)
        elif update_type in ("tool_call", "tool_use"):
            self._handle_tool_call(content)
        elif update_type in ("tool_result", "tool_response"):
            self._handle_tool_result(content)
        elif update_type == "error":
            self._handle_error(text)
        else:
            logger.debug(f"[acp] 未知 update 类型: {update_type}, content={str(content)[:100]}")

    def _handle_thinking(self, delta: str) -> None:
        if not delta:
            return
        self.reasoning_full += delta
        publish(self.task.id, "thinking_delta", {
            "conv_id": self.conv_id,
            "round_idx": self.round_idx,
            "role": "react_agent",
            "phase": "reasoning",
            "delta": delta,
        })

    def _handle_text(self, delta: str) -> None:
        if not delta:
            return
        self.content_full += delta
        publish(self.task.id, "thinking_delta", {
            "conv_id": self.conv_id,
            "round_idx": self.round_idx,
            "role": "react_agent",
            "phase": "content",
            "delta": delta,
        })

    def _handle_tool_call(self, content: Any) -> None:
        """记录工具调用(落库 conversation,推送 SSE)"""
        self.tool_call_count += 1
        tool_name = ""
        tool_args = ""
        if isinstance(content, dict):
            tool_name = content.get("name", "")
            args = content.get("arguments", content.get("input", ""))
            if isinstance(args, dict):
                tool_args = json.dumps(args, ensure_ascii=False)
            else:
                tool_args = str(args)
        elif isinstance(content, str):
            tool_name = content

        intent = f"调用 {tool_name}({tool_args[:200]})" if tool_name else "工具调用"
        _add_conversation(
            self.db, self.task,
            round_idx=self.round_idx,
            role="react_agent", type="tool_call",
            content=intent,
        )

    def _handle_tool_result(self, content: Any) -> None:
        """记录工具结果(落库 conversation,推送 SSE)"""
        text = _extract_text(content)
        _add_conversation(
            self.db, self.task,
            round_idx=self.round_idx,
            role="react_agent", type="tool_result",
            content=(text or str(content))[:500],
        )

    def _handle_error(self, text: str) -> None:
        if not text:
            text = "(未知错误)"
        publish(self.task.id, "thinking_delta", {
            "conv_id": self.conv_id,
            "round_idx": self.round_idx,
            "role": "react_agent",
            "phase": "error",
            "delta": text,
        })


def _extract_text(content: Any) -> str:
    """从 ACP content 字段提取文本

    content 可能是:
    - str:直接返回
    - dict:取 text / content / value 字段
    - list:遍历取每项的 text 字段拼接
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        # 常见字段名:text / content / value / delta
        for key in ("text", "content", "value", "delta"):
            val = content.get(key)
            if isinstance(val, str) and val:
                return val
        # 可能是 {"type": "text", "text": "..."} 格式
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts = []
        for item in content:
            text = _extract_text(item)
            if text:
                parts.append(text)
        return "".join(parts)
    return str(content)


# ============================================================
# 对话落库辅助(与 react_agent 模式一致)
# ============================================================


def _add_conversation(
    db: Session, task: Task, *, round_idx: int, role: str, type: str,
    content: str, reasoning: str | None = None,
    publish_event: bool = True,
) -> None:
    """记录一条对话,可选推送 SSE 事件

    与 react_agent._add_conversation 行为一致:
    - thinking 不推 SSE(流式卡片已展示,避免重复)
    - tool_call / tool_result 推 SSE(前端对话列表实时追加)
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


# ============================================================
# Plan 提取(复用 react_agent 的 <plan> 格式)
# ============================================================


_PLAN_BLOCK_RE = re.compile(r"<plan>\s*(.*?)\s*</plan>", re.DOTALL)
_PLAN_LINE_RE = re.compile(
    r"^\s*(?:\d+[.、)]\s*)?(?:\[([\w_]+)\]\s*)?(.+)$"
)


def _extract_plan(content: str) -> list[dict] | None:
    """从 content 提取 <plan>...</plan> 计划清单

    与 react_agent._extract_plan 格式一致,支持 TRAE CLI 在文本中输出 plan。
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
    """格式化 plan 状态,注入 prompt 让 TRAE CLI 续接进度"""
    if not plan_steps:
        return ""
    lines = ["[系统提醒] 当前计划清单状态(已完成的请标记 [done]):"]
    sym = {"pending": "○", "in_progress": "◌", "done": "✓"}
    for s in plan_steps:
        lines.append(f"{sym.get(s['status'], '○')} [{s['status']}] {s['text']}")
    return "\n".join(lines)


# ============================================================
# 主入口:run_trae_cli_agent
# ============================================================


def run_trae_cli_agent(
    task: Task,
    db: Session,
    round_idx: int = 1,
    followup_query: str | None = None,
    repo_context: str | None = None,
    previous_plan: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """跑一轮 TRAE CLI 执行器

    与 run_react_agent 签名对齐(不含 client 参数,TRAE CLI 自带模型配置)。

    返回:(results, summary, final_plan)
        results: 始终为空 list(结构化结果由 user_agent 在 done 时提取)
        summary: 本轮自然语言总结(TRAE CLI 的最终文本输出)
        final_plan: 本轮结束时的 plan 状态(从 content 提取 <plan>)
    """
    task_id_str = str(task.id)
    set_current_task(task_id_str, task.scenario)

    # ---- 检查沙箱模式 ----
    if settings.SANDBOX_MODE == "mock":
        raise RuntimeError(
            "TRAE CLI 执行器需要沙箱模式(SANDBOX_MODE=sandbox),"
            "mock 模式不支持(沙箱内无 traecli)。"
        )

    # ---- 加载 PAT ----
    pat = _load_trae_cli_pat(db, task.user_id)

    # ---- 获取/创建沙箱会话 ----
    # orchestrator 已通过 _prepare_repo_context 创建会话并 clone 仓库,
    # 这里复用同一会话(session 已含 repo_path)
    ctx = sandbox_tools._get_or_create_session(task_id_str)
    session = ctx["session"]
    repo_path = ctx.get("repo_path", "")

    # ---- 准备 TRAE CLI 环境 ----
    _ensure_trae_cli_env(session, pat)

    # ---- 启动 ACP bridge ----
    bridge_exec_id = _start_acp_bridge(session)

    # 获取端口转发地址
    endpoint_url, endpoint_headers = session.get_endpoint(ACP_BRIDGE_PORT)

    try:
        # 等待 bridge 就绪
        _wait_for_bridge_ready(session, bridge_exec_id, endpoint_url, endpoint_headers)

        # ---- ACP 通信 ----
        client = ACPClient(endpoint_url, endpoint_headers)
        try:
            # 握手
            client.initialize()

            # 创建会话(cwd 设为仓库路径,让 TRAE CLI 在仓库目录下工作)
            cwd = repo_path or TRAE_CLI_WORK_DIR
            acp_session_id = client.new_session(cwd=cwd)

            # ---- 构造 prompt 消息 ----
            user_msg = _build_prompt_message(
                task, round_idx, followup_query, repo_context, repo_path, previous_plan
            )

            # 记录 user 指令
            _add_conversation(
                db, task, round_idx=round_idx,
                role="user", type="question",
                content=user_msg,
            )

            # ---- 流式发送 prompt ----
            conv_id = str(uuid.uuid4())
            collector = _ACPCollector(task, db, round_idx, conv_id)

            # 推送流开始
            publish(task.id, "thinking_delta", {
                "conv_id": conv_id,
                "round_idx": round_idx,
                "role": "react_agent",
                "phase": "start",
                "delta": "",
            })

            try:
                result = client.prompt(
                    acp_session_id,
                    [{"role": "user", "content": user_msg}],
                    on_event=collector,
                )
            except Exception as e:
                logger.exception(f"[task={task.id}] TRAE CLI prompt 失败")
                publish(task.id, "thinking_delta", {
                    "conv_id": conv_id,
                    "round_idx": round_idx,
                    "role": "react_agent",
                    "phase": "error",
                    "delta": f"[TRAE CLI 调用失败: {e}]",
                })
                raise

            # 推送流结束
            publish(task.id, "thinking_delta", {
                "conv_id": conv_id,
                "round_idx": round_idx,
                "role": "react_agent",
                "phase": "end",
                "delta": "",
            })

            # ---- 落库 thinking(content + reasoning) ----
            # 不推 SSE(流式卡片已展示),与 react_agent 一致
            if collector.content_full or collector.reasoning_full:
                _add_conversation(
                    db, task, round_idx=round_idx,
                    role="react_agent", type="thinking",
                    content=collector.content_full,
                    reasoning=collector.reasoning_full,
                    publish_event=False,
                )

        finally:
            client.close()

    finally:
        # 停止 bridge(无论成功失败都清理)
        _stop_acp_bridge(session, bridge_exec_id)

    # ---- 提取 summary 和 plan ----
    summary = collector.content_full or ""
    if not summary:
        summary = f"第 {round_idx} 轮完成(TRAE CLI,{collector.tool_call_count} 次工具调用)"

    # 从 content 提取 plan
    current_plan: list[dict] = [dict(s) for s in (previous_plan or [])]
    extracted = _extract_plan(collector.content_full)
    if extracted:
        current_plan = extracted
        publish(task.id, "plan", {
            "round_idx": round_idx,
            "steps": current_plan,
        })

    logger.info(
        f"[task={task.id}] TRAE CLI 第 {round_idx} 轮完成: "
        f"content={len(collector.content_full)}字符, "
        f"reasoning={len(collector.reasoning_full)}字符, "
        f"tool_calls={collector.tool_call_count}"
    )

    # results 始终为空(结构化结果由 user_agent 在 done 时提取)
    return [], summary, current_plan


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
) -> str:
    """构造发给 TRAE CLI 的 prompt 消息

    与 react_agent 的 user_msg 构造逻辑对齐:
    - 第 1 轮:task.user_input + 仓库信息 + repo_context(已 clone 提示)
    - 追问轮:基于已有仓库继续,注入跨轮记忆(plan 续接)
    """
    if followup_query is None:
        # 第 1 轮:用 task.user_input
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
        # 追问轮:不重新 clone
        msg = (
            f"基于之前的审计结果,现在请针对以下问题继续检查(不需要重新 clone 仓库):\n"
        )
        if repo_path:
            msg += f"仓库路径(已 clone): {repo_path}\n\n"
        msg += f"[本轮追问]\n{followup_query}"

    # 跨轮 plan 续接:若有上轮 plan,作为提醒注入
    if previous_plan:
        reminder = _format_plan_reminder(previous_plan)
        if reminder:
            msg += f"\n\n{reminder}"

    return msg
