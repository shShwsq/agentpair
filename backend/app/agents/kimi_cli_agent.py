"""kimi_cli_agent:基于 Kimi Code CLI + ACP 协议的执行智能体(薄封装)

在沙箱内启动 Kimi Code CLI(开源 https://github.com/MoonshotAI/kimi-code)
的 ACP 服务,通过 HTTP 桥接(acp_bridge.py)与后端通信。

共享基础设施(ACPClient / _ACPCollector / _ACPRecorder / bridge 管理 / 凭证加载 /
事件翻译 / plan 提取)在 acp_base.py 中实现,本模块仅包含 Kimi 特有逻辑:

与 Qoder CLI 的关键差异:
1. ACP 启动命令:`kimi acp`(子命令,非 --acp 标志)
2. 权限绕过:ACP 模式下通过 session/set_config_option(configId='mode',
   value='yolo')在 session/new 后设置(等价 --yolo,ACP 惯用方式)
3. 模型选择:无 --model CLI 参数,通过 KIMI_MODEL_NAME 环境变量注入
   (经 registry credential_env 映射,默认 kimi-for-coding)
4. 凭证注入:不读 shell env 中的 KIMI_API_KEY 等,而是通过 KIMI_MODEL_*
   环境变量族(KIMI_MODEL_NAME + KIMI_MODEL_API_KEY + KIMI_MODEL_BASE_URL)
   合成临时 provider,适合沙箱非交互式场景
5. CLI 模型参数:不支持 --model / --reasoning-effort 等 CLI 参数
   (registry inject_cli_model_args=False),模型经 env vars 设置,
   思考强度经 set_config_option 在 session/new 后设置

认证说明:
  Kimi Code CLI 不从 shell 环境变量读取 KIMI_API_KEY 等凭证(设计决策),
  而是通过 KIMI_MODEL_* 环境变量族合成临时 provider:
  - KIMI_MODEL_NAME:模型名(必填,启用开关,默认 kimi-for-coding)
  - KIMI_MODEL_API_KEY:API Key(必填)
  - KIMI_MODEL_BASE_URL:API 基址(选填,默认 Moonshot 官方)
  - KIMI_MODEL_PROVIDER_TYPE:provider 类型(选填,默认 kimi)
  凭证经 bridge 进程环境变量注入,CLI 子进程继承,无需命令行明文传递。
"""
import logging
from collections.abc import Generator
from typing import Any

from sqlalchemy.orm import Session

from app.agents.acp_base import (
    ACPClient,
    run_acp_agent,
    test_credential_streaming as _base_test_streaming,
)
from app.models.task import Task

logger = logging.getLogger(__name__)


# ============================================================
# 常量
# ============================================================

# agent 类型标识(与 registry 中的 key 对齐)
AGENT_TYPE = "kimi_cli"


# ============================================================
# Kimi 特有:session/new 后的配置设置
# ============================================================


def _kimi_post_session_setup(
    client: ACPClient, session_id: str, task: Task | None
) -> None:
    """Kimi 特有:session/new 后通过 set_config_option 设置运行模式

    Kimi Code CLI 的 ACP 模式无 --yolo 启动参数,通过 ACP
    session/set_config_option 在 session/new 后设置:
    - mode='yolo':跳过所有权限确认(等价 --yolo,实现自主执行)
    - thinking=<effort>:思考强度(若 task.params.reasoning_effort 有值)

    模型选择不在此设置 —— 通过 KIMI_MODEL_NAME 环境变量在 bridge 启动时注入,
    避免与临时 provider 的模型冲突。
    """
    # 设置 yolo 模式(跳过权限确认,实现自主执行)
    client.set_config_option(session_id, "mode", "yolo")
    logger.info("[kimi_cli] 已设置 mode=yolo(跳过权限确认)")

    # 若有 task 且 task.params 含 reasoning_effort,设置思考强度
    if task and task.params:
        effort = task.params.get("reasoning_effort")
        if effort:
            try:
                client.set_config_option(session_id, "thinking", str(effort))
                logger.info(f"[kimi_cli] 已设置 thinking={effort}")
            except Exception as e:
                # 思考强度设置失败不阻塞主流程(模型可能不支持该 thinking 值)
                logger.warning(f"[kimi_cli] 设置 thinking={effort} 失败(忽略): {e}")


# ============================================================
# 主入口:run_kimi_cli_agent(薄封装)
# ============================================================


def run_kimi_cli_agent(
    task: Task,
    db: Session,
    round_idx: int = 1,
    followup_query: str | None = None,
    repo_context: str | None = None,
    previous_plan: list[dict[str, Any]] | None = None,
    agent_type: str = AGENT_TYPE,
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    """跑一轮 Kimi Code CLI 执行器

    与 run_react_agent 签名对齐(不含 client 参数,Kimi CLI 自带模型配置)。

    Kimi 特有:
    - session/new 后调 set_config_option(mode=yolo) 实现自主执行
    - 模型经 KIMI_MODEL_NAME 环境变量注入(用户在「智能体配置」中设置)
    - 不支持 --model / --yolo 等 CLI 参数

    返回:(results, summary, final_plan)
    """
    return run_acp_agent(
        task, db,
        round_idx=round_idx,
        followup_query=followup_query,
        repo_context=repo_context,
        previous_plan=previous_plan,
        agent_type=agent_type,
        post_session_setup=_kimi_post_session_setup,
    )


# ============================================================
# 凭证测试:用于「智能体配置」页面的测试连接按钮
# ============================================================


def test_credential(db: Session, user_id, agent_type: str = AGENT_TYPE) -> tuple[bool, str]:
    """测试 Kimi CLI 凭证是否可用(非流式版,收集 streaming 结果)

    在临时沙箱内启动 ACP bridge,依次验证:
    1. 沙箱镜像含 kimi CLI
    2. API Key 有效(KIMI_MODEL_* 环境变量合成的 provider 可用)
    3. 模型可响应(发送「你好」prompt,确认 LLM 正常工作)

    返回 (ok, message)。
    """
    for event in _base_test_streaming(
        db, user_id, agent_type,
        post_session_setup=_kimi_post_session_setup,
        # Kimi 无需 test_acp_args:模型经 KIMI_MODEL_NAME 环境变量注入
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

    Kimi 特有:session/new 后调 set_config_option(mode=yolo) 设置自主模式,
    模型经 KIMI_MODEL_NAME 环境变量注入(用户配置的或默认 kimi-for-coding)。
    """
    yield from _base_test_streaming(
        db, user_id, agent_type,
        post_session_setup=_kimi_post_session_setup,
        # Kimi 无需 test_acp_args:模型经 KIMI_MODEL_NAME 环境变量注入
    )
