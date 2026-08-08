"""Codex CLI 执行器:OpenAI Codex CLI 集成(通过 codex exec --json + ACP 翻译 bridge)

Codex CLI 不原生支持 ACP 协议,但提供:
- `codex exec --json`:非交互模式,输出 JSONL 流式事件
- `codex exec resume <thread_id>`:恢复之前的会话(多轮对话)
- `~/.codex/config.toml`:模型/provider/审批策略配置

集成方式:
1. registry 注册 codex_cli,指定 bridge_script="codex_bridge"(非默认的 acp_bridge)
2. codex_bridge.py 在沙箱内运行,将 ACP JSON-RPC 翻译为 codex exec 调用
3. pre_bridge_hook 向沙箱写入 ~/.codex/config.toml(模型/provider/base_url 配置)
4. 凭证经环境变量 CODEX_API_KEY 注入(config.toml 的 env_key 指向它)

凭证字段:
- api_key(secret):API Key(注入到 CODEX_API_KEY 环境变量)
- base_url(text,可选):自定义 API 端点(留空用 OpenAI 官方)
- model(text,可选):模型名(留空用 gpt-5)
- wire_api(select):通信协议(responses=Responses API / chat=Chat Completions API)
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.agents.acp_base import (
    run_acp_agent,
    test_credential_streaming as _base_test_streaming,
)
from app.models.task import Task

logger = logging.getLogger(__name__)


# ============================================================
# Codex config.toml 配置
# ============================================================

# 默认模型(留空时使用)
_DEFAULT_MODEL = "gpt-5"

# 默认 wire_api(Responses API,Codex 0.81.0+ 默认)
_DEFAULT_WIRE_API = "responses"


def _codex_pre_bridge_hook(session, credentials: dict[str, str], agent_type: str) -> None:
    """bridge 启动前:向沙箱写入 ~/.codex/config.toml

    Codex 从 ~/.codex/config.toml 读取模型/provider 配置,
    环境变量 CODEX_API_KEY 作为 API Key(config.toml 的 env_key 指向它)。

    config.toml 关键字段:
    - model:模型名(如 gpt-5)
    - model_provider:使用的 provider 名(默认 "agentpair")
    - approval_policy:审批策略("full-auto" = 跳过所有审批)
    - sandbox_mode:沙箱模式("danger-full-access" = 关闭 Codex 内部沙箱,我们用 OpenSandbox)
    - [model_providers.agentpair]:自定义 provider 配置
      - base_url:API 端点
      - wire_api:通信协议("responses" 或 "chat")
      - env_key:读取哪个环境变量的 API Key
    """
    api_key = credentials.get("api_key", "")
    base_url = (credentials.get("base_url") or "").strip()
    model = (credentials.get("model") or "").strip() or _DEFAULT_MODEL
    wire_api = (credentials.get("wire_api") or "").strip() or _DEFAULT_WIRE_API

    # 构建 config.toml
    # 注意:base_url 留空时不写 model_provider,让 Codex 用默认 OpenAI provider
    if base_url:
        # 自定义端点:写入 [model_providers.agentpair] 表
        config_toml = f"""# Codex CLI 配置(由 AgentPair 自动生成)
# 模型配置
model = "{model}"
model_provider = "agentpair"

# 审批策略:never = 从不审批(非交互模式必须)
# 合法值:untrusted / on-failure / on-request / granular / never
approval_policy = "never"

# 沙箱模式:关闭 Codex 内部沙箱(我们用 OpenSandbox 隔离)
sandbox_mode = "danger-full-access"

# 自定义 model provider
[model_providers.agentpair]
name = "AgentPair Custom Provider"
base_url = "{base_url}"
wire_api = "{wire_api}"
env_key = "CODEX_API_KEY"
"""
    else:
        # 使用 OpenAI 官方端点(不需要自定义 provider)
        config_toml = f"""# Codex CLI 配置(由 AgentPair 自动生成)
# 模型配置
model = "{model}"

# 审批策略:never = 从不审批(非交互模式必须)
# 合法值:untrusted / on-failure / on-request / granular / never
approval_policy = "never"

# 沙箱模式:关闭 Codex 内部沙箱(我们用 OpenSandbox 隔离)
sandbox_mode = "danger-full-access"
"""

    # 写入沙箱
    # 先创建 ~/.codex 目录
    session.run_command("mkdir -p ~/.codex", timeout=5)
    session.write_file("~/.codex/config.toml", config_toml)
    logger.info(
        f"[codex_cli] config.toml 已写入(model={model}, base_url={'自定义' if base_url else 'OpenAI默认'}, wire_api={wire_api})"
    )


# ============================================================
# 执行器入口
# ============================================================


def run_codex_cli_agent(
    task: Task,
    db: Session,
    round_idx: int = 1,
    followup_query: str | None = None,
    repo_context: str | None = None,
    previous_plan: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """跑一轮 Codex CLI 执行器

    通过 codex_bridge.py(ACP 翻译层)与 codex exec --json 交互:
    1. pre_bridge_hook 写入 ~/.codex/config.toml(模型/provider/审批策略)
    2. codex_bridge.py 启动,监听 HTTP 端口
    3. ACPClient 发送 initialize → session/new → session/prompt
    4. codex_bridge.py 收到 session/prompt 后运行 codex exec --json
    5. JSONL 事件翻译为 ACP 通知,经 SSE 流式返回
    6. 首次调用提取 thread_id,后续用 codex exec resume 恢复会话
    """
    return run_acp_agent(
        task=task,
        db=db,
        round_idx=round_idx,
        followup_query=followup_query,
        repo_context=repo_context,
        previous_plan=previous_plan,
        agent_type="codex_cli",
        pre_bridge_hook=_codex_pre_bridge_hook,
        # Codex 不需要 credential_env_builder:
        # API Key 经 config.toml 的 env_key="CODEX_API_KEY" 指定,
        # registry 的 credential_env 映射 api_key → CODEX_API_KEY 即可
    )


def test_credential_streaming(db: Session, user_id, agent_type: str):
    """流式测试 Codex CLI 凭证连通性

    流程:
    1. 创建临时沙箱
    2. 写入 ~/.codex/config.toml(含模型/provider 配置)
    3. 启动 codex_bridge.py
    4. 发送测试 prompt("你好,请简短回复确认连接正常")
    5. 流式返回 stage/thinking/content/done 事件
    6. 销毁临时沙箱
    """
    return _base_test_streaming(
        db=db,
        user_id=user_id,
        agent_type=agent_type,
        pre_bridge_hook=_codex_pre_bridge_hook,
    )
