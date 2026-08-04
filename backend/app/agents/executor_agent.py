"""执行智能体抽象层(Executor Provider)

将"执行智能体"抽象为统一接口,支持多种实现:
- BuiltinReactAgent:系统内置的 ReAct 智能体(基于 react_agent.py)
- TraeCLIAgent:基于 TRAE CLI + ACP 协议的外部智能体(沙箱内运行)

orchestrator 通过 get_executor(task) 拿到对应的 provider,调用 .run() 执行一轮,
无需关心底层是内置 LLM 循环还是外部 CLI 协议。

接口契约:
    run(task, db, round_idx, followup_query, client, repo_context, previous_plan)
        -> (results, summary, final_plan)

    - results: 结构化结果列表(始终为空,由 user_agent 在 done 时提取)
    - summary: 本轮自然语言总结(供 user_agent 评估)
    - final_plan: 本轮结束时的 plan 状态(供下一轮续接)

设计说明:
- client(LLMClient)仅对内置 provider 有意义;TRAE CLI provider 自带模型配置,
  会忽略该参数(签名保持一致以符合抽象契约)。
- provider 内部负责设置 task 上下文(set_current_task)、推送 SSE 事件、
  落库 Conversation 记录,与原 run_react_agent 行为对齐。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy.orm import Session

from app.llm.client import LLMClient
from app.models.task import Task

logger = logging.getLogger(__name__)


# ============================================================
# 执行器类型常量(与 Task.executor 字段值对齐)
# ============================================================

EXECUTOR_BUILTIN = "builtin"
EXECUTOR_TRAE_CLI = "trae_cli"

# 已注册的执行器(供 get_executor 校验 + 前端枚举)
REGISTERED_EXECUTORS: tuple[str, ...] = (EXECUTOR_BUILTIN, EXECUTOR_TRAE_CLI)


class ExecutorAgent(ABC):
    """执行智能体抽象基类

    子类必须实现 run(),签名与原 react_agent.run_react_agent 对齐,
    便于 orchestrator 无差别调用。

    子类应在 __init__ 中接收所需的依赖(如沙箱客户端、ACP 客户端等),
    但 run() 的参数列表保持统一。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """执行器标识(用于日志/事件标注)"""

    @abstractmethod
    def run(
        self,
        task: Task,
        db: Session,
        round_idx: int = 1,
        followup_query: str | None = None,
        client: LLMClient | None = None,
        repo_context: str | None = None,
        previous_plan: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        """执行一轮审计

        参数:
            task: 任务对象(含 user_input / params / scenario 等)
            db: 数据库会话(用于落库 Conversation)
            round_idx: 当前协作轮次(1 开始)
            followup_query: 追问指令。None 表示第一轮(用 task.user_input)
            client: LLMClient(仅内置 provider 使用;TRAE CLI 忽略)
            repo_context: 第 1 轮专用,orchestrator 主动 clone 后的仓库上下文
            previous_plan: 上一轮结束时的 plan 状态(跨轮续接)

        返回:(results, summary, final_plan)
            results: 始终为空 list(结构化结果由 user_agent 在 done 时提取)
            summary: 本轮自然语言总结
            final_plan: 本轮结束时的 plan 状态(可能为空 list)
        """
        raise NotImplementedError


# ============================================================
# 内置执行器:包装现有 react_agent
# ============================================================


class BuiltinReactAgent(ExecutorAgent):
    """内置 ReAct 智能体

    直接委托给 app.agents.react_agent.run_react_agent,
    行为与改造前完全一致(零行为变更,纯包装)。
    """

    @property
    def name(self) -> str:
        return EXECUTOR_BUILTIN

    def run(
        self,
        task: Task,
        db: Session,
        round_idx: int = 1,
        followup_query: str | None = None,
        client: LLMClient | None = None,
        repo_context: str | None = None,
        previous_plan: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        # 延迟导入避免循环依赖(react_agent 依赖 tools.schema,本模块被 orchestrator 导入)
        from app.agents.react_agent import run_react_agent

        return run_react_agent(
            task,
            db,
            round_idx=round_idx,
            followup_query=followup_query,
            client=client,
            repo_context=repo_context,
            previous_plan=previous_plan,
        )


# ============================================================
# TRAE CLI 执行器:基于 ACP 协议(沙箱内运行)
# ============================================================


class TraeCLIAgent(ExecutorAgent):
    """基于 TRAE CLI 的执行智能体

    通过 ACP(Agent Client Protocol)与沙箱内运行的 TRAE CLI 通信:
    1. 沙箱内启动 acp_bridge.py(HTTP<->stdio 桥接服务)
    2. 后端通过 ACP HTTP client 与 bridge 通信
    3. bridge 转发请求到 traecli 的 stdio ACP 接口

    模型配置在沙箱内的 trae_cli.yaml 中指定,后端不直接管理 LLM 调用。

    详见 app/agents/trae_cli_agent.py 的完整实现。
    """

    @property
    def name(self) -> str:
        return EXECUTOR_TRAE_CLI

    def run(
        self,
        task: Task,
        db: Session,
        round_idx: int = 1,
        followup_query: str | None = None,
        client: LLMClient | None = None,
        repo_context: str | None = None,
        previous_plan: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        # 延迟导入:TRAE CLI 依赖 sandbox_tools + ACP client,
        # 仅在真正使用时加载,避免内置模式启动时引入额外依赖
        from app.agents.trae_cli_agent import run_trae_cli_agent

        logger.info(
            f"[task={task.id}] 使用 TRAE CLI 执行器,round={round_idx}"
        )
        return run_trae_cli_agent(
            task,
            db,
            round_idx=round_idx,
            followup_query=followup_query,
            repo_context=repo_context,
            previous_plan=previous_plan,
            # client 参数被忽略:TRAE CLI 自带模型配置
        )


# ============================================================
# 工厂:按 task.executor 选择 provider
# ============================================================


def get_executor(task: Task) -> ExecutorAgent:
    """根据 task.executor 字段返回对应的执行器实例

    - "builtin"(默认):返回 BuiltinReactAgent
    - "trae_cli":返回 TraeCLIAgent

    未知值回退到 builtin 并记录 warning(不阻塞任务执行)。
    每次调用返回新实例(provider 无状态,实例化成本低)。
    """
    executor = (task.executor or EXECUTOR_BUILTIN).strip().lower()

    if executor == EXECUTOR_TRAE_CLI:
        return TraeCLIAgent()

    if executor != EXECUTOR_BUILTIN:
        logger.warning(
            f"[task={task.id}] 未知 executor='{executor}',回退到 builtin"
        )

    return BuiltinReactAgent()
