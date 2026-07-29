"""双智能体协作编排器(阶段 4)

驱动 user_agent + react_agent 多轮协作:
1. user_agent 初始评估,给出第一轮指令
2. react_agent 执行第一轮(含 clone)
3. user_agent 对照 checklist 评估 react_agent 结果
4. 若未覆盖完整,user_agent 构造追问
5. react_agent 执行追问(不重新 clone)
6. 循环 3-5 直到 done 或达到 MAX_ROUNDS

react_agent 自己落库 results 和 conversations(带 round_idx),
orchestrator 只管 task 状态 + user_agent 对话 + 沙箱清理

阶段 7:接入事件总线,每条对话/状态变更实时推送,前端 SSE 可见每一步
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agents.react_agent import run_react_agent
from app.agents.user_agent import MAX_ROUNDS, run_user_agent
from app.event_bus import finish_task, publish
from app.llm.client import LLMClient
from app.models.task import Conversation, Task, TaskStatus
from app.models.user_llm_config import UserLLMConfig
from app.tools import sandbox_tools

logger = logging.getLogger(__name__)


def run_dual_agent_audit(task: Task, db: Session) -> None:
    """执行双智能体协作审计"""
    task_id_str = str(task.id)

    task.status = TaskStatus.RUNNING
    task.current_stage = "双智能体协作启动"
    db.commit()
    _publish_status(task)

    scenario_id = task.scenario

    # 阶段 6:加载用户保存的 LLM 配置(若已配置则覆盖 env 默认)
    llm_client = _build_llm_client(db, task.user_id)

    # 用户原始意图
    user_intent = task.user_input
    params = task.params or {}
    if params.get("repo_url"):
        user_intent += f"\n仓库地址: {params['repo_url']}"
    if params.get("branch"):
        user_intent += f"\n分支: {params['branch']}"

    # react_agent 历轮结果摘要(给 user_agent 评估用)
    react_summaries: list[dict] = []
    all_results_count = 0

    try:
        # ---------- 第 0 轮:user_agent 初始评估 ----------
        task.current_stage = "user_agent 初始评估"
        db.commit()
        _publish_status(task)

        ua_result_0 = run_user_agent(
            user_intent, [],
            task_id=task.id, round_idx=0,
            scenario_id=scenario_id,
            client=llm_client,
        )
        _record_user_agent(db, task, 0, ua_result_0)

        followup = ua_result_0.get("followup_query", user_intent)

        # ---------- 协作循环 ----------
        for round_idx in range(1, MAX_ROUNDS + 1):
            task.current_stage = f"第 {round_idx} 轮:react_agent 执行"
            db.commit()
            _publish_status(task)

            # react_agent 跑一轮
            # 第 1 轮 followup_query=None(用初始指令,会 clone)
            # 后续轮 followup_query=追问(不 clone)
            is_first = round_idx == 1
            results, summary = run_react_agent(
                task, db,
                round_idx=round_idx,
                followup_query=None if is_first else followup,
                client=llm_client,
            )

            react_summaries.append({
                "round": round_idx,
                "results": results,
                "summary": summary,
            })
            all_results_count += len(results)

            # user_agent 评估
            task.current_stage = f"第 {round_idx} 轮:user_agent 评估"
            db.commit()
            _publish_status(task)

            ua_result = run_user_agent(
                user_intent, react_summaries,
                task_id=task.id, round_idx=round_idx,
                scenario_id=scenario_id,
                client=llm_client,
            )
            _record_user_agent(db, task, round_idx, ua_result)

            if ua_result.get("done"):
                logger.info(f"[task={task.id}] user_agent 在第 {round_idx} 轮宣布完成")
                break

            followup = ua_result.get("followup_query", "")
        else:
            logger.warning(f"[task={task.id}] 达到最大轮次 {MAX_ROUNDS}")

        # ---------- 标记完成 ----------
        task.status = TaskStatus.COMPLETED
        task.current_stage = (
            f"双智能体协作完成,{len(react_summaries)} 轮,"
            f"共 {all_results_count} 个结果"
        )
        task.completed_at = datetime.now(timezone.utc)
        db.commit()
        _publish_status(task)

        # user_agent 最终总结
        _add_conversation(
            db, task, round_idx=len(react_summaries),
            role="user_agent", type="summary",
            content=(
                f"双智能体协作完成。\n"
                f"协作轮次: {len(react_summaries)}\n"
                f"结果总数: {all_results_count}\n"
                f"user_agent 最终评估: {ua_result.get('reasoning', '')}"
            ),
        )

    except Exception as e:
        logger.exception(f"[task={task.id}] 双智能体协作失败")
        task.status = TaskStatus.FAILED
        task.error_message = str(e)[:1000]
        task.current_stage = "执行失败"
        db.commit()
        _publish_status(task)
        _add_conversation(
            db, task, round_idx=0,
            role="user_agent", type="error",
            content=f"执行失败: {e}",
        )
    finally:
        # 延迟关闭沙箱:标记任务完成,保留 session 供前端浏览工作区文件
        # 实际清理由 workspace 路由的 cleanup_expired_sessions() 惰性触发(TTL 1 小时)
        try:
            sandbox_tools.mark_task_completed(task_id_str)
        except Exception as cleanup_err:
            logger.warning(f"[task={task.id}] 标记任务完成失败: {cleanup_err}")
        # 通知事件总线:任务结束,推送 done/error 终止事件
        if task.status == TaskStatus.COMPLETED:
            publish(task.id, "done", {"status": "completed"})
        else:
            publish(task.id, "error", {
                "status": "failed",
                "error_message": task.error_message or "未知错误",
            })
        finish_task(task.id)


# ============================================================
# 辅助:记录 user_agent 的对话
# ============================================================


def _record_user_agent(
    db: Session, task: Task, round_idx: int, ua_result: dict
) -> None:
    """把 user_agent 的输出记录到 Conversation 表

    - content:精简显示,只放追问内容(前端默认展示)
    - reasoning:完整评估(覆盖情况/判断/追问/done),用于刷新页面后回看
    react_agent 接收追问是通过函数参数传递的,不依赖 Conversation 表。
    """
    covered = ua_result.get("covered", [])
    missing = ua_result.get("missing", [])
    reasoning_text = ua_result.get("reasoning", "")
    followup = ua_result.get("followup_query", "")
    done = ua_result.get("done", False)

    # 精简 content:只显示追问
    if done:
        content = "评估完成,无需追问"
    elif followup:
        content = followup
    else:
        content = "(未给出追问)"

    # 完整评估 reasoning(可折叠回看)
    full_eval = (
        f"[user_agent 第 {round_idx} 轮评估]\n"
        f"已覆盖: {covered}\n"
        f"未覆盖: {missing}\n"
        f"判断: {reasoning_text}\n"
    )
    if followup:
        full_eval += f"追问: {followup}\n"
    if done:
        full_eval += "→ 宣布完成\n"

    _add_conversation(
        db, task, round_idx=round_idx,
        role="user_agent", type="evaluation",
        content=content,
        reasoning=full_eval,
    )


def _add_conversation(
    db: Session, task: Task, *, round_idx: int, role: str, type: str, content: str,
    reasoning: str | None = None,
) -> None:
    """落库一条对话,同时推送事件给前端 SSE

    reasoning:可选,完整评估/思考链(如 user_agent evaluation 的覆盖情况+判断),
        前端默认折叠,点击展开回看。None 时不落库该字段。
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
    # 推送给事件总线(前端 SSE 实时接收)
    publish(task.id, "conversation", {
        "id": str(conv.id),
        "round_idx": conv.round_idx,
        "role": conv.role,
        "type": conv.type,
        "content": conv.content,
        "reasoning": conv.reasoning,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
    })


def _publish_status(task: Task) -> None:
    """推送任务状态变更事件"""
    publish(task.id, "status", {
        "status": task.status.value if hasattr(task.status, 'value') else str(task.status),
        "current_stage": task.current_stage,
    })


def _build_llm_client(db: Session, user_id) -> LLMClient | None:
    """按 task.user_id 加载用户保存的 LLM 配置,构造 LLMClient

    - user_id 为空(匿名任务)或未配置 → 返回 None,agent 内部回退到 env 默认
    - 用户已配置 → 返回 LLMClient.from_user_config(...)
    - 构造失败(如 provider 已下线)→ 记日志并回退到 None
    """
    if user_id is None:
        return None
    try:
        cfg = db.query(UserLLMConfig).filter(UserLLMConfig.user_id == user_id).first()
        if cfg is None or not cfg.llm_config:
            return None
        return LLMClient.from_user_config(cfg)
    except Exception as e:
        logger.warning(f"[user={user_id}] 加载用户 LLM 配置失败,回退到 env 默认: {e}")
        return None
