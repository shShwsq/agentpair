"""任务完成后自动生成练习题 draft(题库供给自动化)

挂在 orchestrator 任务完成收尾处(与 memory_summarize 同模式):
- 只生成 draft,仍需用户在任务详情页预览确认后转 active
- 守卫条件:登录用户、开关开启、存在结构化 finding(带 cwe/severity 元信息);
  单 agent 模式的纯摘要 Result 无元信息,自动跳过避免低质量题
- 同一任务已生成过(任何状态的题目)则跳过,避免追问/重试后重复出题
"""
import logging

from sqlalchemy.orm import Session

from app.models.practice import Question
from app.models.task import Result, Task
from app.models.user_preference import UserPreference
from app.services.practice.generator import generate_questions_for_task

logger = logging.getLogger(__name__)

# 视为"结构化 finding"的元信息键(至少命中一个才出题)
_FINDING_META_KEYS = ("cwe", "severity")


def _has_structured_findings(db: Session, task_id) -> bool:
    """任务的 Results 中是否存在带 cwe/severity 元信息的结构化发现"""
    results = db.query(Result.metadata_).filter(Result.task_id == task_id).all()
    for (meta,) in results:
        if not meta:
            continue
        if any(str(meta.get(k) or "").strip() for k in _FINDING_META_KEYS):
            return True
    return False


def auto_generate_practice_for_task(task: Task, db: Session) -> int:
    """任务完成时为审计发现自动生成练习题 draft

    返回新生成的题目数;任一守卫条件不满足返回 0(调用方无需区分原因)。
    异常由调用方 try/except 兜底,不影响任务完成。
    """
    # 1) 匿名任务不支持(练习题 per-user 隔离)
    if task.user_id is None:
        return 0

    # 2) 用户开关
    pref = db.query(UserPreference).filter(
        UserPreference.user_id == task.user_id
    ).first()
    if pref is not None and not pref.auto_generate_practice:
        logger.info("[task=%s] 用户关闭了自动生成练习题,跳过", task.id)
        return 0

    # 3) 已有题目(任意状态):追问/重试重新完成时不重复出题
    exists = db.query(Question.id).filter(
        Question.source_task_id == task.id
    ).first()
    if exists:
        return 0

    # 4) 只对有结构化发现的任务出题(单 agent 纯摘要无元信息,质量差)
    if not _has_structured_findings(db, task.id):
        logger.info("[task=%s] 无结构化审计发现(cwe/severity),跳过自动生成", task.id)
        return 0

    created, skipped = generate_questions_for_task(db, task, task.user_id)
    logger.info(
        "[task=%s] 自动生成练习题 draft: %d 题(%d 条发现未能出题)",
        task.id, len(created), skipped,
    )
    return len(created)
