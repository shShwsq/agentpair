"""qoder_cli_agent:基于 Qoder CLI + ACP 协议的执行智能体

在沙箱内启动 Qoder CLI 的 ACP(Agent Client Protocol)服务,通过 HTTP 桥接
(acp_bridge.py)与后端通信。模型配置由 Qoder 账号配额管理,后端不直接管理
LLM 调用 —— Qoder CLI 内部自主完成 ReAct 循环(思考→工具→观察)。

工作流程:
1. 复用 sandbox_tools 的沙箱会话(orchestrator 已预 clone 仓库)
2. 从 user_agent_configs 加载用户凭证(加密存储),经 registry 映射为环境变量
3. 将 acp_bridge.py 写入沙箱
4. 后台启动 acp_bridge.py(监听端口 ACP_BRIDGE_PORT),凭证经 envs 注入,
   bridge 进程继承后传给 qodercli 子进程(不在命令行明文出现)
5. 通过 get_endpoint(ACP_BRIDGE_PORT) 获取转发地址 + headers
6. ACP 客户端:initialize → session/new → session/prompt
7. 流式接收 session/update 通知,翻译为 event_bus 事件(thinking_delta /
   conversation),推送前端,实现与内置 react_agent 一致的流式体验
8. 收集最终 summary,提取 plan,返回 (results, summary, plan)

与内置 react_agent 的差异:
- Qoder CLI 自主管理 ReAct 循环(工具调用/观察),后端只发一个 prompt
- 模型由 Qoder 账号配额管理,不使用 task.llm_config_id
- 凭证认证:用户在「智能体配置」配置 Qoder PAT,加密存入 user_agent_configs,
  运行时解密并经环境变量注入沙箱
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

from app.agents.registry import get_agent_meta, get_sandbox_config
from app.config import settings
from app.event_bus import publish
from app.models.task import Conversation, Task
from app.models.user_agent_config import UserAgentConfig
from app.security import decrypt_secret
from app.tools import sandbox_tools
from app.tools.schema import set_current_task

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

# agent 类型标识(与 registry 中的 key 对齐)
AGENT_TYPE = "qoder_cli"

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

# 本地 acp_bridge.py 源文件路径(用于写入沙箱)
_BRIDGE_SOURCE = Path(__file__).parent / "acp_bridge.py"


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

        with self._client.stream(
            "POST",
            f"{self.base_url}/rpc",
            json=request,
            timeout=timeout,
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
        客户端选一个调本方法。PAT 等凭证经环境变量注入到 bridge 进程,
        qodercli 子进程继承后在此步骤完成服务端认证。

        认证成功后才能创建 session,否则会收到 -32000 Authentication required。
        timeout: 超时(秒),认证可能涉及网络往返 qoder.com 验证 PAT,建议传有限值。
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


def _load_credentials(db: Session, user_id, agent_type: str = AGENT_TYPE) -> dict[str, str]:
    """从 user_agent_configs 加载解密后的凭证 dict

    返回如 {"pat": "xxx"}。未配置或解密失败时抛错(CLI executor 需要凭证才能认证)。
    agent_type: agent 类型标识(如 "qoder_cli" / "qoder_cli_cn"),用于查对应的配置记录。
    """
    if user_id is None:
        raise RuntimeError("Qoder CLI 执行器需要登录用户(匿名任务不支持)")

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
            f"未配置 {agent_type} 凭证。请在「智能体配置」中配置 Qoder Personal Access Token。"
        )

    try:
        plaintext = decrypt_secret(row.credentials_encrypted)
        data = json.loads(plaintext)
        if not isinstance(data, dict):
            raise ValueError("凭证格式错误(非 JSON 对象)")
        return data
    except Exception as e:
        raise RuntimeError(f"Qoder CLI 凭证解密失败: {e}") from e


def _build_credential_envs(credentials: dict[str, str], agent_type: str = AGENT_TYPE) -> dict[str, str]:
    """将凭证 dict 映射为环境变量 dict(按 registry 的 credential_env)

    registry 中 credential_env 形如 {"pat": "QODER_PERSONAL_ACCESS_TOKEN"},
    即凭证 key → 环境变量名。只注入有值的凭证。
    """
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    cred_env_map: dict[str, str] = sandbox_cfg.get("credential_env", {})
    envs: dict[str, str] = {}
    for cred_key, env_name in cred_env_map.items():
        val = credentials.get(cred_key)
        if val:
            envs[env_name] = val
    return envs


# ============================================================
# 沙箱环境准备:bridge 脚本 + CLI 可用性检查
# ============================================================


def _get_bin(agent_type: str = AGENT_TYPE) -> str:
    """从 settings 读取 CLI 可执行文件名(经 registry 的 config key)"""
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    config_key = sandbox_cfg.get("bin_config_key", "")
    if config_key:
        val = getattr(settings, config_key, None)
        if val:
            return val
    return sandbox_cfg.get("bin_default", "qodercli")


def _get_install_cmd(agent_type: str = AGENT_TYPE) -> str:
    """从 settings 读取 CLI 安装命令(经 registry 的 config key)"""
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    config_key = sandbox_cfg.get("install_cmd_config_key", "")
    if config_key:
        val = getattr(settings, config_key, None)
        if val:
            return val
    return sandbox_cfg.get("install_cmd_default", "")


def _get_acp_args(task: Task | None = None, agent_type: str = AGENT_TYPE) -> list[str]:
    """从 registry 读取 ACP 启动参数,并注入 task.params 中的模型配置

    支持的 task.params 字段(均为可选,见 https://docs.qoder.cn/cli/model):
        model:            模型分级(auto/ultimate/performance/efficient/lite)或前沿模型名
        reasoning_effort: 思考强度(low/medium/high/xhigh/max)
        context_window:   上下文窗口(200000/400000/1000000)
    """
    sandbox_cfg = get_sandbox_config(agent_type) or {}
    args = list(sandbox_cfg.get("acp_args", ["--acp", "--yolo"]))

    if task and task.params:
        model = task.params.get("model")
        if model:
            args.extend(["--model", str(model)])
        effort = task.params.get("reasoning_effort")
        if effort:
            args.extend(["--reasoning-effort", str(effort)])
        ctx = task.params.get("context_window")
        if ctx:
            args.extend(["--context-window", str(ctx)])
    return args


def _write_bridge_script(session) -> None:
    """将 acp_bridge.py 写入沙箱(从本地源文件读取)"""
    if not _BRIDGE_SOURCE.exists():
        raise RuntimeError(f"acp_bridge.py 源文件不存在: {_BRIDGE_SOURCE}")
    content = _BRIDGE_SOURCE.read_text(encoding="utf-8")
    session.write_file(BRIDGE_SCRIPT_PATH, content)


def _ensure_cli_env(session, agent_type: str = AGENT_TYPE) -> None:
    """准备沙箱内 CLI 运行环境

    1. 创建 bridge 脚本目录
    2. 写入 acp_bridge.py
    3. 检查 CLI 是否可用,不可用则尝试安装

    注意:Qoder CLI 通过环境变量认证,无需配置文件。
    """
    # 创建脚本目录
    session.run_command(f"mkdir -p {Path(BRIDGE_SCRIPT_PATH).parent.as_posix()}")

    # 写入 bridge 脚本
    _write_bridge_script(session)

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
                f"请在沙箱镜像中预装 Qoder CLI,或在配置中设置安装命令。"
            )
        logger.info(f"[qoder_cli] {cli_bin} 未安装,执行: {install_cmd}")
        install_result = session.run_command(install_cmd, timeout=120, check=False)
        # 再次检查
        result = session.run_command(check_cmd, timeout=10)
        if not result.strip():
            raise RuntimeError(
                f"Qoder CLI 安装失败({install_cmd})。"
                f"安装日志: {install_result[:500]}"
            )

    logger.info(f"[qoder_cli] 环境就绪: {cli_bin} 可用,bridge 脚本已写入 {BRIDGE_SCRIPT_PATH}")


# ============================================================
# ACP bridge 生命周期管理
# ============================================================


def _start_acp_bridge(
    session,
    credential_envs: dict[str, str],
    task: Task | None = None,
    agent_type: str = AGENT_TYPE,
) -> str:
    """后台启动 ACP bridge,返回 execution_id

    bridge 启动命令:
        python3 acp_bridge.py --port {port} --bin {bin} --args '{json}'
    凭证经 envs 注入到 bridge 进程,bridge 子进程(qodercli)继承这些环境变量,
    实现 PAT 不在命令行明文出现。

    task 参数用于从 task.params 读取模型/思考强度/上下文窗口配置(见 _get_acp_args)。
    通过 run_command_background 非阻塞启动。
    """
    cli_bin = _get_bin(agent_type)
    acp_args = _get_acp_args(task, agent_type)
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
        envs=credential_envs,  # 凭证注入 bridge 进程,继承给 qodercli
        work_dir=BRIDGE_WORK_DIR,
    )
    logger.info(f"[qoder_cli] ACP bridge 后台启动: execution_id={execution_id}")
    return execution_id


def _wait_for_bridge_ready(
    session, execution_id: str, endpoint_url: str, endpoint_headers: dict[str, str]
) -> None:
    """等待 bridge HTTP 服务就绪(健康检查轮询)

    超时(BRIDGE_STARTUP_TIMEOUT 秒)未就绪时,读取 bridge 日志辅助排查并抛错。
    """
    deadline = time.time() + BRIDGE_STARTUP_TIMEOUT
    client = httpx.Client(headers=endpoint_headers, timeout=5)

    logger.info(
        f"[qoder_cli] 等待 bridge 就绪: endpoint={endpoint_url}, "
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
            # 新增日志内容打印为 debug,便于实时观察 bridge 状态(生产环境不刷屏)
            if logs and len(logs) > last_logs_len:
                new_part = logs[last_logs_len:]
                logger.debug(f"[qoder_cli] bridge 日志增量: {new_part.rstrip()}")
                last_logs_len = len(logs)

            # 健康检查
            try:
                resp = client.get(f"{endpoint_url}/health")
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "ok":
                        logger.info("[qoder_cli] ACP bridge 就绪")
                        return
                    else:
                        logger.debug(f"[qoder_cli] health 200 但状态非 ok: {data}")
                else:
                    # 非 200:打印响应体辅助排查(可能是 server proxy 错误)
                    body = resp.text[:300]
                    logger.debug(f"[qoder_cli] health 返回 HTTP {resp.status_code}: {body}")
            except Exception as e:
                logger.debug(f"[qoder_cli] health 请求失败: {type(e).__name__}: {e}")

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
        logger.info(f"[qoder_cli] ACP bridge 已停止: {execution_id}")
    except Exception as e:
        logger.warning(f"[qoder_cli] 停止 bridge 失败(忽略): {e}")


# ============================================================
# 凭证测试:用于「智能体配置」页面的测试连接按钮
# ============================================================


def test_credential(db: Session, user_id, agent_type: str = AGENT_TYPE) -> tuple[bool, str]:
    """测试 Qoder CLI 凭证是否可用

    在临时沙箱内启动 ACP bridge,依次验证:
    1. 沙箱镜像含 CLI(qodercli / qoderclicn)
    2. PAT 有效(Qoder 服务端认证通过,ACP initialize + authenticate)
    3. 模型可响应(发送「你好」prompt,确认 LLM 正常工作,消耗少量 credits)

    临时沙箱在测试结束后立即销毁,不污染任务执行环境。

    agent_type: "qoder_cli"(国际版)或 "qoder_cli_cn"(国内版),决定用哪个 CLI 和 PAT 环境变量。

    返回 (ok, message):
        ok=True: 测试通过,message 含 ACP 协议版本 + 模型响应预览
        ok=False: 测试失败,message 为人类可读的错误原因(可显示给用户)
    """
    # ---- 加载凭证 ----
    try:
        credentials = _load_credentials(db, user_id, agent_type)
    except RuntimeError as e:
        return False, f"凭证加载失败: {e}"

    credential_envs = _build_credential_envs(credentials, agent_type)
    if not credential_envs:
        return False, "凭证映射为空(请检查 registry 配置)"

    # ---- 检查沙箱模式 ----
    if settings.SANDBOX_MODE == "mock":
        return False, "测试连接需要 SANDBOX_MODE=sandbox(mock 模式无 CLI)"

    # ---- 创建临时沙箱 ----
    # 注意:不复用任务的沙箱会话,避免污染任务上下文
    from app.sandbox.client import create_sandbox

    logger.info(f"[qoder_cli_test] 开始测试({agent_type}):创建临时沙箱")
    session = create_sandbox()
    bridge_exec_id: str | None = None

    try:
        # ---- 准备 CLI 环境(写 bridge 脚本 + 检查/安装 CLI) ----
        try:
            _ensure_cli_env(session, agent_type)
            logger.info("[qoder_cli_test] CLI 环境就绪")
        except RuntimeError as e:
            return False, f"CLI 环境准备失败: {e}"

        # 清理可能残留的旧登录态(国际版 ~/.qoder,国内版 ~/.qoder-cn)
        cli_bin = _get_bin(agent_type)
        config_dirs = "~/.qoder ~/.qoder-cn"
        session.run_command(f"rm -rf {config_dirs} 2>/dev/null", timeout=5)

        try:
            logger.info(
                f"[qoder_cli_test] 注入环境变量 keys: {list(credential_envs.keys())}"
            )
            bridge_exec_id = _start_acp_bridge(session, credential_envs, agent_type=agent_type)
            endpoint_url, endpoint_headers = session.get_endpoint(ACP_BRIDGE_PORT)
            logger.info(
                f"[qoder_cli_test] bridge 已启动,等待就绪: endpoint={endpoint_url}"
            )
            _wait_for_bridge_ready(session, bridge_exec_id, endpoint_url, endpoint_headers)
            logger.info("[qoder_cli_test] bridge 就绪,开始 ACP 握手")

            # 诊断:确认环境变量真的传到了 bridge 进程(及 qoderclicn 子进程)
            # 通过 /proc/<pid>/environ 检查,只显示 key 不显示 value
            diag_cmd = (
                'BRIDGE_PID=$(pgrep -f acp_bridge.py | head -1);'
                'if [ -n "$BRIDGE_PID" ]; then'
                '  echo "bridge PID=$BRIDGE_PID";'
                '  cat /proc/$BRIDGE_PID/environ 2>/dev/null | tr "\\0" "\\n"'
                '    | grep -iE "QODER|TOKEN" | sed "s/=.*/=<set>/";'
                'else echo "WARN: 未找到 bridge 进程"; fi'
            )
            diag_result = session.run_command(diag_cmd, timeout=5)
            logger.info(f"[qoder_cli_test] bridge 进程环境变量诊断: {diag_result}")
        except RuntimeError as e:
            err_msg = str(e)
            # bridge 日志可能含认证失败关键词
            if any(kw in err_msg.lower() for kw in ("auth", "unauthorized", "token", "401", "credential")):
                return False, f"PAT 认证失败: {err_msg}"
            return False, f"ACP bridge 启动失败: {err_msg}"

        # ---- ACP 握手 + 认证 + 测试 prompt ----
        # qodercli 在 PAT 无效时通常会在 initialize/authenticate 阶段报认证错误;
        # 认证通过后再发个「你好」prompt,确认模型能正常响应(消耗少量 credits)。
        client = ACPClient(endpoint_url, endpoint_headers)
        try:
            result = client.initialize()
            protocol_version = result.get("protocolVersion", "?")
            auth_methods = result.get("authMethods", []) or []
            logger.info(
                f"[qoder_cli_test] ACP 握手成功: protocolVersion={protocol_version}, "
                f"authMethods={[m.get('id') for m in auth_methods]}"
            )

            # ---- ACP 认证(若 Agent 要求) ----
            # CLI 通过 authMethods 声明需要认证,客户端必须调 authenticate
            # 才能创建 session。PAT 经环境变量注入,在此步骤完成服务端认证。
            if auth_methods:
                # 动态选择认证方法:优先含 "login" 的,否则取第一个
                # (不同 CLI 版本可能用不同的 method_id,如 qodercli-login / qoderclicn-login)
                method_id = ""
                for m in auth_methods:
                    mid = m.get("id", "")
                    if "login" in mid:
                        method_id = mid
                        break
                if not method_id:
                    method_id = auth_methods[0].get("id", "")
                if not method_id:
                    return False, "ACP 握手成功但未找到可用的认证方法"

                logger.info(f"[qoder_cli_test] 开始 ACP 认证: methodId={method_id}")
                try:
                    # authenticate 涉及网络往返验证 PAT,给 90s 超时
                    auth_result = client.authenticate(method_id, timeout=90)
                    logger.info(
                        f"[qoder_cli_test] ACP 认证成功: "
                        f"{json.dumps(auth_result, ensure_ascii=False)[:200]}"
                    )
                except httpx.TimeoutException:
                    if bridge_exec_id:
                        logs, _ = session.get_background_logs(bridge_exec_id)
                        logger.warning(
                            f"[qoder_cli_test] 认证超时({agent_type}),"
                            f"bridge 完整日志(最多3000字符):\n{(logs or '')[-3000:]}"
                        )
                    return False, (
                        f"ACP 认证超时(90s,{agent_type})。"
                        f"可能原因:PAT 无效/过期、沙箱网络不通、"
                        f"CLI 版本不支持环境变量认证。"
                        f"请查看后端日志 [qoder_cli_test] 开头的输出排查。"
                    )
                except RuntimeError as e:
                    err_msg = str(e)
                    low = err_msg.lower()
                    if any(kw in low for kw in ("auth", "unauthorized", "token", "401",
                                                "credential", "authentication required")):
                        return False, f"PAT 认证失败: {err_msg}"
                    return False, f"ACP 认证失败: {err_msg}"
                except Exception as e:
                    logger.exception("[qoder_cli_test] ACP 认证异常")
                    return False, f"ACP 认证异常: {e}"

            # ---- 发送测试 prompt 验证模型可响应 ----
            try:
                # cwd 用 /tmp(沙箱内必定存在),避免 /home/user 不存在导致 -32602
                acp_session_id = client.new_session(cwd="/tmp")
                logger.info(
                    f"[qoder_cli_test] session/new 返回: sessionId={acp_session_id}"
                )

                test_prompt = [{"type": "text", "text": "你好"}]
                logger.info(
                    f"[qoder_cli_test] 发送 session/prompt: "
                    f"sessionId={acp_session_id}, prompt={json.dumps(test_prompt, ensure_ascii=False)}"
                )

                # 收集模型文本回复
                # ACP session/update 结构: params.update.sessionUpdate / params.update.content
                reply_chunks: list[str] = []

                def _on_test_event(msg: dict) -> None:
                    if msg.get("method") != "session/update":
                        return
                    params = msg.get("params") or {}
                    update = params.get("update") or {}
                    update_type = update.get("sessionUpdate", "")
                    content = update.get("content", "")
                    # agent_message_chunk 是模型文本回复
                    if update_type == "agent_message_chunk":
                        text = _extract_text(content)
                        if text:
                            reply_chunks.append(text)

                client.prompt(
                    acp_session_id,
                    test_prompt,
                    on_event=_on_test_event,
                    timeout=30,  # 测试场景 30s 超时,避免模型无响应时卡死
                )

                reply = "".join(reply_chunks).strip()
                if not reply:
                    return False, "ACP 认证成功,但模型未响应(请检查 PAT 配额或网络)"

                preview = reply[:80] + ("..." if len(reply) > 80 else "")
                logger.info(f"[qoder_cli_test] 模型响应: {preview}")
                return True, (
                    f"连接成功(ACP 协议版本 {protocol_version}),"
                    f"模型响应: {preview}"
                )
            except httpx.TimeoutException:
                return False, "ACP 认证成功,但模型响应超时(30s,请检查网络或配额)"
            except RuntimeError as e:
                err_msg = str(e)
                low = err_msg.lower()
                if any(kw in low for kw in ("auth", "unauthorized", "token", "401",
                                            "quota", "credit", "limit", "余额", "配额")):
                    return False, f"PAT 认证或配额失败: {err_msg}"
                return False, f"模型响应测试失败: {err_msg}"

        except RuntimeError as e:
            err_msg = str(e)
            if any(kw in err_msg.lower() for kw in ("auth", "unauthorized", "token", "401")):
                return False, f"PAT 认证失败: {err_msg}"
            return False, f"ACP 握手失败: {err_msg}"
        finally:
            client.close()

    except Exception as e:
        logger.exception("[qoder_cli_test] 测试过程异常")
        return False, f"测试异常: {e}"
    finally:
        # ---- 清理:停止 bridge + 销毁沙箱 ----
        if bridge_exec_id:
            _stop_acp_bridge(session, bridge_exec_id)
        try:
            session.close()
        except Exception as e:
            logger.info(f"[qoder_cli_test] 关闭沙箱失败(忽略): {e}")


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
              "sessionUpdate": "agent_message_chunk" | "plan" | "tool_call" | "tool_call_update" | ...,
              "content": { "type": "text", "text": "..." },  # agent_message_chunk
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
        # 记录原始 msg 结构(qodercli 实际输出可能与标准有差异,便于核对)
        logger.debug(
            f"[acp] raw msg: {json.dumps(msg, ensure_ascii=False)[:500]}"
        )

        method = msg.get("method", "")
        if method != "session/update":
            return

        params = msg.get("params") or {}
        update = params.get("update") or {}
        update_type = update.get("sessionUpdate", "")
        content = update.get("content", "")

        # 提取文本(content 可能是 str / dict / list)
        text = _extract_text(content)

        if update_type in ("thought_chunk", "thinking", "reasoning"):
            self._handle_thinking(text)
        elif update_type == "agent_message_chunk":
            self._handle_text(text)
        elif update_type == "tool_call":
            self._handle_tool_call(update)
        elif update_type == "tool_call_update":
            # status=completed 时携带工具结果
            if update.get("status") == "completed":
                self._handle_tool_result(content)
        elif update_type == "plan":
            self._handle_plan(update.get("entries", []))
        elif update_type == "error":
            self._handle_error(text)
        else:
            logger.debug(f"[acp] 未知 update 类型: {update_type}, update={str(update)[:100]}")

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

    def _handle_tool_call(self, update: dict) -> None:
        """记录工具调用(落库 conversation,推送 SSE)

        ACP tool_call update 结构:
            {"sessionUpdate":"tool_call","toolCallId":"...","title":"...","kind":"...","status":"pending"}
        """
        self.tool_call_count += 1
        tool_name = update.get("title", "") or update.get("toolCallId", "")
        intent = f"调用 {tool_name}" if tool_name else "工具调用"
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

    与 react_agent._extract_plan 格式一致,支持 CLI 在文本中输出 plan。
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
# 主入口:run_qoder_cli_agent
# ============================================================


def run_qoder_cli_agent(
    task: Task,
    db: Session,
    round_idx: int = 1,
    followup_query: str | None = None,
    repo_context: str | None = None,
    previous_plan: list[dict[str, Any]] | None = None,
    agent_type: str = AGENT_TYPE,
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """跑一轮 Qoder CLI 执行器

    与 run_react_agent 签名对齐(不含 client 参数,Qoder CLI 自带模型配置)。
    agent_type 决定使用哪个 CLI(国际版 qoder_cli / 国内版 qoder_cli_cn),
    默认 AGENT_TYPE("qoder_cli"),由 executor_agent.ExternalCLIAgent 注入。

    返回:(results, summary, final_plan)
        results: 始终为空 list(结构化结果由 user_agent 在 done 时提取)
        summary: 本轮自然语言总结(CLI 的最终文本输出)
        final_plan: 本轮结束时的 plan 状态(从 content 提取 <plan>)
    """
    task_id_str = str(task.id)
    set_current_task(task_id_str, task.scenario)

    # ---- 校验 agent 类型已注册 ----
    if get_agent_meta(agent_type) is None:
        raise RuntimeError(f"agent 类型未注册: {agent_type}")

    # ---- 检查沙箱模式 ----
    if settings.SANDBOX_MODE == "mock":
        raise RuntimeError(
            "Qoder CLI 执行器需要沙箱模式(SANDBOX_MODE=sandbox),"
            "mock 模式不支持(沙箱内无 qodercli)。"
        )

    # ---- 加载凭证 + 映射为环境变量 ----
    credentials = _load_credentials(db, task.user_id, agent_type)
    credential_envs = _build_credential_envs(credentials, agent_type)
    if not credential_envs:
        raise RuntimeError("Qoder CLI 凭证映射为空,无法注入环境变量(请检查 registry 配置)")

    # ---- 获取/创建沙箱会话 ----
    # orchestrator 已通过 _prepare_repo_context 创建会话并 clone 仓库,
    # 这里复用同一会话(session 已含 repo_path)
    ctx = sandbox_tools._get_or_create_session(task_id_str)
    session = ctx["session"]
    repo_path = ctx.get("repo_path", "")

    # ---- 准备 CLI 环境 ----
    _ensure_cli_env(session, agent_type)

    # ---- 启动 ACP bridge ----
    bridge_exec_id = _start_acp_bridge(session, credential_envs, task, agent_type=agent_type)

    # 获取端口转发地址
    endpoint_url, endpoint_headers = session.get_endpoint(ACP_BRIDGE_PORT)

    try:
        # 等待 bridge 就绪
        _wait_for_bridge_ready(session, bridge_exec_id, endpoint_url, endpoint_headers)

        # ---- ACP 通信 ----
        client = ACPClient(endpoint_url, endpoint_headers)
        try:
            # 握手
            init_result = client.initialize()

            # 认证(若 Agent 要求)
            # 动态选择认证方法:优先含 "login" 的 method_id
            # (qodercli-login 国际版 / qoderclicn-login 国内版,见 test_credential 同款逻辑)
            auth_methods = init_result.get("authMethods", []) or []
            if auth_methods:
                method_id = ""
                for m in auth_methods:
                    mid = m.get("id", "")
                    if "login" in mid:
                        method_id = mid
                        break
                if not method_id:
                    method_id = auth_methods[0].get("id", "")
                if method_id:
                    logger.info(f"[qoder_cli:{agent_type}] ACP 认证: methodId={method_id}")
                    client.authenticate(method_id)

            # 创建会话(cwd 设为仓库路径,让 CLI 在仓库目录下工作)
            cwd = repo_path or BRIDGE_WORK_DIR
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
                    [{"type": "text", "text": user_msg}],
                    on_event=collector,
                )
            except Exception as e:
                logger.exception(f"[task={task.id}] Qoder CLI prompt 失败")
                publish(task.id, "thinking_delta", {
                    "conv_id": conv_id,
                    "round_idx": round_idx,
                    "role": "react_agent",
                    "phase": "error",
                    "delta": f"[Qoder CLI 调用失败: {e}]",
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
        summary = f"第 {round_idx} 轮完成(Qoder CLI,{collector.tool_call_count} 次工具调用)"

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
        f"[task={task.id}] Qoder CLI 第 {round_idx} 轮完成: "
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
    """构造发给 Qoder CLI 的 prompt 消息

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
