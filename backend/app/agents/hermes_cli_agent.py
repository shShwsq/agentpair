"""hermes_cli_agent:基于 Hermes CLI + ACP 协议的执行智能体(薄封装)

在沙箱内启动 Hermes CLI(开源 https://github.com/NousResearch/hermes-agent)
的 ACP 服务,通过 HTTP 桥接(acp_bridge.py)与后端通信。

共享基础设施(ACPClient / _ACPCollector / _ACPRecorder / bridge 管理 / 凭证加载 /
事件翻译 / plan 提取)在 acp_base.py 中实现,本模块仅包含 Hermes 特有逻辑:

与 Kimi/Qoder 的关键差异:
1. ACP 启动命令:`hermes acp`(子命令,非 --acp 标志)
2. 权限绕过:Hermes 在模块导入时读取 HERMES_YOLO_MODE 环境变量并冻结,
   设为 1 即跳过所有危险命令审批(等价 --yolo),无需 post_session_setup。
   per_command 模式不注入此环境变量,Hermes 进入审批模式,
   危险命令通过 acp_adapter/permissions.py 发 ACP request_permission 给前端确认。
3. 模型/Provider 配置:Hermes 从 ~/.hermes/config.yaml 读取模型名、provider、
   base_url(LLM_MODEL 环境变量已废弃),通过 pre_bridge_hook 在 bridge 启动前
   写入沙箱
4. API Key 注入:Hermes 按 provider 读取不同的环境变量名(如 OPENROUTER_API_KEY、
   ANTHROPIC_API_KEY 等),通过 credential_env_builder 动态映射

认证说明:
  Hermes 不从单一环境变量读取 API Key,而是按 provider 读取对应的
  <PROVIDER>_API_KEY(如 OPENROUTER_API_KEY / ANTHROPIC_API_KEY / GLM_API_KEY 等)。
  模型和 provider 配置必须写入 ~/.hermes/config.yaml(LLM_MODEL 环境变量已废弃)。
  凭证经 bridge 进程环境变量注入(API Key)+ config.yaml 文件注入(模型/provider),
  CLI 子进程继承环境变量并加载 config.yaml,无需命令行明文传递。
"""
import logging
from collections.abc import Generator
from typing import Any

from sqlalchemy.orm import Session

from app.agents.acp_base import (
    run_acp_agent,
    test_credential_streaming as _base_test_streaming,
)
from app.models.task import Task

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

# agent 类型标识(与 registry 中的 key 对齐)
AGENT_TYPE = "hermes_cli"

# 沙箱内 Hermes 配置目录和文件路径
HERMES_HOME_DIR = "/home/user/.hermes"
HERMES_CONFIG_PATH = f"{HERMES_HOME_DIR}/config.yaml"

# ============================================================
# Provider → 环境变量 / config.yaml 映射表
# ============================================================

# 每个 provider 的配置:
# - api_key_env:     API Key 环境变量名(注入到 bridge 进程)
# - base_url_env:    Base URL 环境变量名(可选,用户提供 base_url 时注入)
# - default_base_url: 默认 base_url(写入 config.yaml;空串表示用 Hermes 内置默认)
# - default_model:    默认模型名(用户未填时写入 config.yaml)
# - config_provider:  config.yaml 中 model.provider 字段的值
#   参考 references/hermes-agent-main/cli-config.yaml.example 的 provider 枚举
_PROVIDER_CONFIG: dict[str, dict[str, str]] = {
    "openrouter": {
        "api_key_env": "OPENROUTER_API_KEY",
        "base_url_env": "",  # OpenRouter base_url 固定,无需环境变量覆盖
        "default_base_url": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-opus-4.6",
        "config_provider": "openrouter",
    },
    "anthropic": {
        "api_key_env": "ANTHROPIC_API_KEY",
        "base_url_env": "ANTHROPIC_BASE_URL",
        "default_base_url": "",  # Hermes 内置 https://api.anthropic.com
        "default_model": "claude-opus-4.6",
        "config_provider": "anthropic",
    },
    "openai": {
        # Hermes 无独立 "openai" provider,用 "custom" + base_url 指向 OpenAI 端点
        "api_key_env": "OPENAI_API_KEY",
        "base_url_env": "OPENAI_BASE_URL",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "config_provider": "custom",
    },
    "zai": {
        # z.ai / ZhipuAI GLM 系列
        "api_key_env": "GLM_API_KEY",
        "base_url_env": "GLM_BASE_URL",
        "default_base_url": "https://api.z.ai/api/paas/v4",
        "default_model": "glm-4-plus",
        "config_provider": "zai",
    },
    "kimi-coding": {
        # Kimi / Moonshot AI(经 Hermes 内置 kimi-coding provider)
        "api_key_env": "KIMI_API_KEY",
        "base_url_env": "KIMI_BASE_URL",
        "default_base_url": "https://api.kimi.com/coding/v1",
        "default_model": "kimi-k2.5",
        "config_provider": "kimi-coding",
    },
    "minimax": {
        # MiniMax 在 Hermes 中用 api_mode=anthropic_messages(Anthropic 兼容端点)
        "api_key_env": "MINIMAX_API_KEY",
        "base_url_env": "MINIMAX_BASE_URL",
        "default_base_url": "https://api.minimaxi.com/anthropic",
        "default_model": "MiniMax-M2",
        "config_provider": "minimax",
    },
    "gemini": {
        # Google AI Studio / Gemini(OpenAI 兼容端点)
        "api_key_env": "GOOGLE_API_KEY",
        "base_url_env": "GEMINI_BASE_URL",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-3-flash-preview",
        "config_provider": "gemini",
    },
}

# 默认 provider(用户未选择时)
_DEFAULT_PROVIDER = "openrouter"

# 使用 api_mode=anthropic_messages 的 provider(Hermes 的 anthropic/minimax profile)
# 这些 provider 需要 anthropic Python 包,但 install.sh 默认不装 [anthropic] extra
_PROVIDERS_NEEDING_ANTHROPIC_PKG = {"anthropic", "minimax"}


# ============================================================
# Hermes 特有:动态凭证环境变量构建
# ============================================================


def _hermes_credential_env_builder(
    credentials: dict[str, str], task: Task | None = None
) -> dict[str, str]:
    """按 provider 选择动态构建凭证环境变量

    Hermes 按 provider 读取不同的 <PROVIDER>_API_KEY 环境变量名,
    无法用 registry 的静态 credential_env 映射,需在此动态构建。

    命令确认模式(task.params._executor_command_confirm):
    - always_approve(默认 / task=None 测试场景):注入 HERMES_YOLO_MODE=1
      Hermes 在模块导入时读取并冻结此变量(tools/approval.py:_YOLO_MODE_FROZEN),
      必须在 bridge 进程启动前设置(经 envs 注入,子进程继承)
    - per_command:不注入 HERMES_YOLO_MODE,Hermes 进入审批模式,
      遇到危险命令通过 acp_adapter/permissions.py 发 ACP request_permission 给前端确认

    按 provider 注入:
    - <PROVIDER>_API_KEY:API Key(如 OPENROUTER_API_KEY / ANTHROPIC_API_KEY)
    - <PROVIDER>_BASE_URL:Base URL(可选,用户提供时才注入)
    """
    provider = (credentials.get("provider") or _DEFAULT_PROVIDER).strip()
    cfg = _PROVIDER_CONFIG.get(provider)
    if cfg is None:
        logger.warning(
            f"[hermes_cli] 未知 provider '{provider}',回退到 {_DEFAULT_PROVIDER}"
        )
        cfg = _PROVIDER_CONFIG[_DEFAULT_PROVIDER]
        provider = _DEFAULT_PROVIDER

    # 读命令确认模式:task=None(测试连接)时默认 always_approve
    approval_mode = "always_approve"
    if task and task.params:
        approval_mode = task.params.get("_executor_command_confirm", "always_approve")

    envs: dict[str, str] = {}
    if approval_mode == "per_command":
        # 不注入 HERMES_YOLO_MODE:Hermes 进入审批模式,危险命令发 request_permission
        logger.info("[hermes_cli] per_command 模式:不注入 HERMES_YOLO_MODE,危险命令将弹窗确认")
    else:
        # always_approve:注入 HERMES_YOLO_MODE=1 跳过权限审批(冻结于导入时)
        envs["HERMES_YOLO_MODE"] = "1"

    # API Key
    api_key = credentials.get("api_key", "")
    if api_key:
        envs[cfg["api_key_env"]] = api_key
    else:
        logger.warning(f"[hermes_cli] provider={provider} 但 api_key 为空")

    # Base URL(可选,用户提供时注入对应环境变量)
    base_url = credentials.get("base_url", "")
    if base_url and cfg.get("base_url_env"):
        envs[cfg["base_url_env"]] = base_url

    logger.info(
        f"[hermes_cli] 凭证环境变量构建: provider={provider}, "
        f"api_key_env={cfg['api_key_env']}, "
        f"base_url_env={cfg.get('base_url_env') or '(none)'}, "
        f"approval_mode={approval_mode}, "
        f"env_keys={list(envs.keys())}"
    )
    return envs


# ============================================================
# Hermes 特有:bridge 启动前写入 config.yaml
# ============================================================


def _hermes_pre_bridge_hook(session, credentials: dict[str, str], agent_type: str) -> None:
    """在 bridge 启动前向沙箱写入 ~/.hermes/config.yaml

    Hermes 的模型/provider/base_url 配置只能从 ~/.hermes/config.yaml 读取
    (LLM_MODEL 环境变量已废弃,见 .env.example 注释),必须写文件。

    config.yaml 结构(最小化,仅含 model 节):
        model:
          default: "<模型名>"
          provider: "<provider>"
          base_url: "<base_url>"   # 仅在有值时写入

    同时创建 ~/.hermes 目录(Hermes 启动时 load_hermes_dotenv 需要)。
    """
    provider = (credentials.get("provider") or _DEFAULT_PROVIDER).strip()
    cfg = _PROVIDER_CONFIG.get(provider) or _PROVIDER_CONFIG[_DEFAULT_PROVIDER]

    # 模型名:用户配置 > provider 默认
    model = credentials.get("model", "").strip() or cfg["default_model"]

    # Base URL:用户配置 > provider 默认(空串表示用 Hermes 内置默认,不写入)
    base_url = credentials.get("base_url", "").strip() or cfg["default_base_url"]

    # 构建 config.yaml 内容
    lines = [
        "# Hermes CLI 配置(由 AgentPair 自动生成)",
        "model:",
        f'  default: "{model}"',
        f'  provider: "{cfg["config_provider"]}"',
    ]
    if base_url:
        lines.append(f'  base_url: "{base_url}"')
    config_content = "\n".join(lines) + "\n"

    # 创建 ~/.hermes 目录
    session.run_command(f"mkdir -p {HERMES_HOME_DIR}", timeout=5)

    # 写入 config.yaml
    session.write_file(HERMES_CONFIG_PATH, config_content)

    # 确保 anthropic_messages 模式的 provider 有 anthropic 包
    # install.sh 默认不装 [anthropic] extra;需在 Dockerfile 构建时预装(见 build-sandbox-image.sh)
    # 运行时无法补装:venv 目录 root 所有,沙箱以 user 身份运行,写入会 Permission denied
    if provider in _PROVIDERS_NEEDING_ANTHROPIC_PKG:
        venv_python = "/usr/local/lib/hermes-agent/venv/bin/python"
        verify = session.run_command(
            f"{venv_python} -c 'import anthropic; print(anthropic.__version__)'",
            timeout=10, check=False,
        )
        if verify and verify.strip():
            logger.info(f"[hermes_cli] anthropic 包就绪: v{verify.strip()}")
        else:
            raise RuntimeError(
                f"沙箱镜像缺少 anthropic Python 包(provider={provider} 需要 anthropic_messages 模式)。\n"
                f"请重建沙箱镜像:bash scripts/build-sandbox-image.sh\n"
                f"(构建时会自动执行 ensurepip + pip install anthropic)"
            )
    logger.info(
        f"[hermes_cli] 已写入 {HERMES_CONFIG_PATH}: "
        f"provider={cfg['config_provider']}, model={model}, "
        f"base_url={base_url or '(hermes default)'}"
    )


# ============================================================
# 主入口:run_hermes_cli_agent(薄封装)
# ============================================================


def run_hermes_cli_agent(
    task: Task,
    db: Session,
    round_idx: int = 1,
    followup_query: str | None = None,
    repo_context: str | None = None,
    previous_plan: list[dict[str, Any]] | None = None,
    agent_type: str = AGENT_TYPE,
    agent_policy: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """跑一轮 Hermes CLI 执行器

    与 run_react_agent 签名对齐(不含 client 参数,Hermes CLI 自带模型配置)。

    Hermes 特有:
    - 命令确认模式(task.params._executor_command_confirm):
      - always_approve(默认):注入 HERMES_YOLO_MODE=1,跳过权限审批,无需 post_session_setup
      - per_command:不注入 HERMES_YOLO_MODE,Hermes 遇到危险命令发 ACP request_permission 给前端确认
    - 模型/provider/base_url 经 ~/.hermes/config.yaml 注入(经 pre_bridge_hook 写入)
    - API Key 按 provider 动态映射到对应环境变量名(经 credential_env_builder)

    返回:(results, summary, final_plan)
    """
    return run_acp_agent(
        task, db,
        round_idx=round_idx,
        followup_query=followup_query,
        repo_context=repo_context,
        previous_plan=previous_plan,
        agent_type=agent_type,
        agent_policy=agent_policy,
        credential_env_builder=_hermes_credential_env_builder,
        pre_bridge_hook=_hermes_pre_bridge_hook,
        # 无需 post_session_setup:
        # - always_approve:HERMES_YOLO_MODE 经 env 注入,导入时冻结
        # - per_command:不注入 HERMES_YOLO_MODE,Hermes 自动走 ACP request_permission
    )


# ============================================================
# 凭证测试:用于「智能体配置」页面的测试连接按钮
# ============================================================


def test_credential(db: Session, user_id, agent_type: str = AGENT_TYPE) -> tuple[bool, str]:
    """测试 Hermes CLI 凭证是否可用(非流式版,收集 streaming 结果)

    在临时沙箱内启动 ACP bridge,依次验证:
    1. 沙箱镜像含 hermes CLI
    2. API Key 有效(provider 凭证经环境变量注入)
    3. 模型可响应(发送「你好」prompt,确认 LLM 正常工作)

    返回 (ok, message)。
    """
    for event in _base_test_streaming(
        db, user_id, agent_type,
        credential_env_builder=_hermes_credential_env_builder,
        pre_bridge_hook=_hermes_pre_bridge_hook,
    ):
        if event.get("type") == "done":
            data = event.get("data", {})
            return data.get("ok", False), data.get("message", "")
        if event.get("type") == "error":
            data = event.get("data", {})
            return False, data.get("message", "测试异常")
    return False, "测试未返回结果"


def test_credential_streaming(
    db: Session, user_id, agent_type: str = AGENT_TYPE
) -> Generator[dict, None, None]:
    """流式版测试凭证:yield SSE 事件 dict(供路由层格式化为 SSE)

    Hermes 特有:
    - HERMES_YOLO_MODE=1 经环境变量注入
    - 模型/provider/base_url 经 config.yaml 注入(经 pre_bridge_hook 写入)
    - API Key 按 provider 动态映射(经 credential_env_builder)
    """
    yield from _base_test_streaming(
        db, user_id, agent_type,
        credential_env_builder=_hermes_credential_env_builder,
        pre_bridge_hook=_hermes_pre_bridge_hook,
    )
