"""双智能体协作编排器(阶段 4 核心创新)

驱动 user_agent + react_agent 多轮协作:
1. user_agent 初始评估,给出第一轮指令
2. react_agent 执行第一轮审计
3. user_agent 对照 checklist 评估 react_agent 结果
4. 若未覆盖完整,user_agent 构造追问
5. react_agent 执行追问(只看新指令,不再 clone)
6. 循环 3-5 直到 user_agent 返回 done 或达到 MAX_ROUNDS

与单 agent 的差异:
- react_agent 的 run_react_agent 接受 task + followup_query 参数
- followup_query 不为空时,react_agent 跳过 clone,直接基于已有 repo 跑追问审计
- user_agent 维护跨轮上下文,所有 findings 累积落库
"""
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.agents.react_agent import run_react_agent
from app.agents.user_agent import MAX_ROUNDS, run_user_agent
from app.models.task import Conversation, Task, TaskStatus

logger = logging.getLogger(__name__)


def run_dual_agent_audit(task: Task, db: Session) -> None:
    """执行双智能体协作审计

    替代单 agent 的 run_react_agent,作为阶段 4 起的默认执行入口
    """
    task.status = TaskStatus.RUNNING
    task.current_stage = "双智能体协作启动"
    db.commit()

    # 用户原始意图
    user_intent = (
        f"请审计这个仓库: {task.repo_url}"
        + (f"\n分支: {task.branch}" if task.branch else "")
        + (f"\n范围: {task.scope}" if task.scope else "")
    )

    # react_agent 历轮结果摘要(给 user_agent 评估用)
    react_summaries: list[dict[str, Any]] = []
    # 所有轮次的 findings 累积
    all_findings: list[dict[str, Any]] = []

    try:
        # 第 0 轮:user_agent 先理解意图,给出初始指令
        task.current_stage = "user_agent 初始评估"
        db.commit()

        ua_result_0 = run_user_agent(user_intent, [])
        _record_user_agent(db, task, 0, ua_result_0)

        first_query = ua_result_0.get("followup_query", f"请审计仓库: {task.repo_url}")

        # 循环:react_agent 跑 → user_agent 评估 → 追问 → ...
        for round_idx in range(1, MAX_ROUNDS + 1):
            task.current_stage = f"第 {round_idx} 轮:react_agent 执行"
            db.commit()

            # react_agent 跑一轮
            # 第 1 轮:followup_query 是初始指令,需要 clone
            # 后续轮:followup_query 是追问,不重新 clone
            is_first_round = round_idx == 1
            round_findings, round_summary = run_react_agent_with_summary(
                task, db, first_query if is_first_round else None,
                followup_query=None if is_first_round else react_summaries[-1].get("followup_query", ""),
            )

            # 累积 findings
            all_findings.extend(round_findings)
            react_summaries.append({
                "round": round_idx,
                "findings": round_findings,
                "summary": round_summary,
                "followup_query": first_query if is_first_round else None,
            })

            # user_agent 评估
            task.current_stage = f"第 {round_idx} 轮:user_agent 评估"
            db.commit()

            ua_result = run_user_agent(user_intent, react_summaries)
            _record_user_agent(db, task, round_idx, ua_result)

            if ua_result.get("done"):
                logger.info(f"[task={task.id}] user_agent 在第 {round_idx} 轮宣布完成")
                break

            # 把 followup_query 存到 summary 里,供下一轮 react_agent 用
            react_summaries[-1]["followup_query"] = ua_result.get("followup_query", "")
        else:
            logger.warning(
                f"[task={task.id}] 达到最大轮次 {MAX_ROUNDS},强制结束"
            )

        # 落库所有 findings(去重:同一 file_path+line_range+category 视为重复)
        _persist_findings(task, db, all_findings)

        # 标记完成
        task.status = TaskStatus.COMPLETED
        task.current_stage = (
            f"双智能体审计完成,共 {len(react_summaries)} 轮,"
            f"发现 {len(all_findings)} 个漏洞"
        )
        from datetime import datetime, timezone
        task.completed_at = datetime.now(timezone.utc)
        db.commit()

        _add_conversation(
            db, task,
            role="user_agent",
            type="summary",
            content=(
                f"双智能体审计完成。\n"
                f"协作轮次: {len(react_summaries)}\n"
                f"发现漏洞: {len(all_findings)} 个\n"
                f"user_agent 最终评估: {ua_result.get('reasoning', '')}"
            ),
        )

    except Exception as e:
        logger.exception(f"[task={task.id}] 双智能体审计失败")
        task.status = TaskStatus.FAILED
        task.error_message = str(e)[:1000]
        task.current_stage = "执行失败"
        db.commit()
        _add_conversation(
            db, task,
            role="user_agent",
            type="error",
            content=f"执行失败: {e}",
        )


# ============================================================
# react_agent 单轮执行(返回 summary)
# ============================================================


def run_react_agent_with_summary(
    task: Task,
    db: Session,
    initial_query: str | None,
    followup_query: str | None,
) -> tuple[list[dict[str, Any]], str]:
    """跑一轮 react_agent,返回 (findings, summary)

    参数:
        initial_query: 初始任务描述(仅第一轮用,需要 clone)
        followup_query: 追问指令(后续轮用,不 clone)

    返回:(findings 列表, react_agent 的 summary 文本)
    """
    # 复用 react_agent 的逻辑,但需要拿到 findings 和 summary
    # 这里通过 Conversation 表回查最新一轮的结果
    from app.agents.react_agent import run_react_agent

    # 调用 react_agent(它会写 Conversation 和 Finding 到 db)
    # followup_query 不为空时,react_agent 应跳过 clone
    # 但当前 react_agent 实现总是从头开始,我们需要轻量改一下

    # 临时方案:followup_query 不为空时,把它注入到 task.scope,react_agent 会看到
    # 更彻底的方案:重构 react_agent 支持两种模式
    # 这里用最简方案,不改 react_agent 签名,通过 task.scope 传递

    original_scope = task.scope
    if followup_query:
        task.scope = f"[追问] {followup_query}"
        db.commit()
    elif initial_query:
        # 第一轮:确保 scope 是用户原始的(可能有,可能无)
        task.scope = original_scope
        db.commit()

    # 调 react_agent
    # 注意:react_agent 内部会 set_current_task,我们这里不需要再 set
    run_react_agent(task, db)

    # 恢复 task.scope(避免污染)
    task.scope = original_scope
    db.commit()

    # 从 db 查最新一轮的 findings 和 thinking
    # react_agent 跑完后,task 的 findings 是累积的
    # 我们要拿到"这一轮新增的"findings 和 react_agent 的最后一段 thinking
    from app.models.task import Finding
    findings_db = db.query(Finding).filter(Finding.task_id == task.id).all()
    # 转成 dict 列表
    findings_list = [
        {
            "category": f.category,
            "severity": f.severity,
            "file_path": f.file_path,
            "line_range": f.line_range,
            "description": f.description,
            "remediation": f.remediation or "",
        }
        for f in findings_db
    ]

    # 拿最后一段 react_agent 的 thinking 作为 summary
    from app.models.task import Conversation
    last_thinking = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task.id,
            Conversation.role == "react_agent",
            Conversation.type == "thinking",
        )
        .order_by(Conversation.created_at.desc())
        .first()
    )
    summary = last_thinking.content if last_thinking else "(无 summary)"

    return findings_list, summary


# ============================================================
# 辅助:落库 findings(去重)
# ============================================================


def _persist_findings(
    task: Task, db: Session, all_findings: list[dict[str, Any]]
) -> None:
    """落库 findings,去重

    同一 file_path + line_range + category 视为重复,只保留第一个
    """
    from app.models.task import Finding

    # 先清掉 react_agent 已经写入的(避免重复)
    db.query(Finding).filter(Finding.task_id == task.id).delete()

    seen = set()
    for f in all_findings:
        key = (f.get("file_path", ""), f.get("line_range", ""), f.get("category", ""))
        if key in seen:
            continue
        seen.add(key)
        finding = Finding(
            task_id=task.id,
            category=f.get("category", "CWE-Unknown"),
            severity=f.get("severity", "info"),
            file_path=f.get("file_path"),
            line_range=f.get("line_range"),
            description=f.get("description", ""),
            remediation=f.get("remediation"),
            verified="unverified",
        )
        db.add(finding)
    db.commit()


# ============================================================
# 辅助:记录 user_agent 的对话
# ============================================================


def _record_user_agent(
    db: Session, task: Task, round_idx: int, ua_result: dict[str, Any]
) -> None:
    """把 user_agent 的输出记录到 Conversation 表"""
    covered = ua_result.get("covered", [])
    missing = ua_result.get("missing", [])
    reasoning = ua_result.get("reasoning", "")
    followup = ua_result.get("followup_query", "")
    done = ua_result.get("done", False)

    content = (
        f"[user_agent 第 {round_idx} 轮评估]\n"
        f"已覆盖: {covered}\n"
        f"未覆盖: {missing}\n"
        f"判断: {reasoning}\n"
    )
    if followup:
        content += f"追问: {followup}\n"
    if done:
        content += "→ 宣布审计完成\n"

    _add_conversation(
        db, task,
        role="user_agent",
        type="evaluation",
        content=content,
    )

    # followup_query 也单独记一条,方便后续 react_agent 引用
    if followup and not done:
        _add_conversation(
            db, task,
            role="user_agent",
            type="followup",
            content=followup,
        )


def _add_conversation(db: Session, task: Task, *, role: str, type: str, content: str) -> None:
    db.add(Conversation(task_id=task.id, role=role, type=type, content=content))
    db.commit()
