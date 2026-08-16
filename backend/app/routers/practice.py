"""练习模块路由:题目生成 / 确认 / 组卷 / 答题 / 统计 / 题库管理

鉴权:全部 Depends(get_current_user),数据 per-user 隔离。
题目来源为审计任务的真实发现(Result),经 LLM 改编为客观题;
选题策略见 services/practice/selector.py(SM-2 到期复习 + 薄弱点 + 难度匹配)。
"""
import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Integer, cast, func as sa_func
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models.practice import (
    Attempt,
    KnowledgePoint,
    PracticeSession,
    Question,
    QuestionStatus,
    UserKnowledgeState,
)
from app.models.task import Result, Task
from app.models.user import User
from app.schemas.practice import (
    ConfirmQuestionsRequest,
    ConfirmQuestionsResponse,
    DraftQuestionResponse,
    GenerateRequest,
    GenerateResponse,
    KnowledgeStateResponse,
    QuestionListItem,
    SessionQuestionResponse,
    StartSessionRequest,
    StartSessionResponse,
    StatsResponse,
    SubmitAnswerRequest,
    SubmitAnswerResponse,
    WeakPointItem,
)
from app.services.practice.difficulty import (
    ABILITY_WINDOW,
    adjust_question_difficulty,
    estimate_ability,
)
from app.services.practice.generator import generate_questions_for_task
from app.services.practice.selector import CandidateInfo, select_questions
from app.services.practice.sm2 import (
    SM2State,
    apply_sm2,
    quality_from_correct,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/practice", tags=["practice"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_task_owned(db: Session, task_id: UUID, user_id: UUID) -> Task:
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or task.user_id != user_id:
        raise HTTPException(status_code=404, detail="任务不存在或无权访问")
    return task


def _estimate_user_ability(db: Session, user_id: UUID) -> float:
    """由最近答对题目的难度估计能力值(升序传入 estimate_ability)"""
    rows = (
        db.query(Question.difficulty)
        .join(Attempt, Attempt.question_id == Question.id)
        .filter(Attempt.user_id == user_id, Attempt.is_correct.is_(True))
        .order_by(Attempt.answered_at.desc())
        .limit(ABILITY_WINDOW)
        .all()
    )
    return estimate_ability([d for (d,) in reversed(rows)])


# ============================================================
# 题目生成 / 确认
# ============================================================


@router.post("/generate", response_model=GenerateResponse)
def generate_questions(
    req: GenerateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GenerateResponse:
    """从审计任务的真实发现生成候选题(draft)"""
    task = _get_task_owned(db, req.task_id, current_user.id)
    result_count = (
        db.query(sa_func.count(Result.id))
        .filter(Result.task_id == task.id)
        .scalar()
    )
    if not result_count:
        raise HTTPException(status_code=400, detail="该任务还没有审计发现,无法生成练习题")

    created, skipped = generate_questions_for_task(
        db, task, current_user.id, max_findings=req.max_findings
    )
    if not created:
        raise HTTPException(status_code=502, detail="题目生成失败,请稍后重试")

    kp_by_id = {
        kp.id: kp
        for kp in db.query(KnowledgePoint).filter(
            KnowledgePoint.id.in_([q.knowledge_point_id for q in created])
        ).all()
    }
    items = []
    for q in created:
        kp = kp_by_id.get(q.knowledge_point_id)
        items.append(DraftQuestionResponse(
            id=q.id,
            qtype=q.qtype.value,
            stem=q.stem,
            code_snippet=q.code_snippet,
            options=q.options,
            answer_idx=q.answer_idx,
            explanation=q.explanation,
            difficulty=q.difficulty,
            knowledge_key=kp.key if kp else None,
            knowledge_name=kp.name if kp else None,
        ))
    return GenerateResponse(questions=items, skipped_findings=skipped)


@router.post("/questions/confirm", response_model=ConfirmQuestionsResponse)
def confirm_questions(
    req: ConfirmQuestionsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ConfirmQuestionsResponse:
    """确认勾选的 draft 题目入库,丢弃同一任务的其余 draft"""
    drafts = (
        db.query(Question)
        .filter(
            Question.user_id == current_user.id,
            Question.source_task_id == req.task_id,
            Question.status == QuestionStatus.DRAFT,
        )
        .all()
    )
    keep_ids = set(req.question_ids)
    confirmed = 0
    discarded = 0
    for q in drafts:
        if q.id in keep_ids:
            q.status = QuestionStatus.ACTIVE
            confirmed += 1
        else:
            db.delete(q)
            discarded += 1
    db.commit()
    return ConfirmQuestionsResponse(confirmed=confirmed, discarded=discarded)


# ============================================================
# 练习会话(按需即时组卷)
# ============================================================


@router.post("/sessions", response_model=StartSessionResponse)
def start_session(
    req: StartSessionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StartSessionResponse:
    """按选题策略组卷(答案不下发)"""
    questions = db.query(Question).filter(
        Question.user_id == current_user.id,
        Question.status == QuestionStatus.ACTIVE,
    ).all()
    if not questions:
        raise HTTPException(
            status_code=400,
            detail="题库为空,请先在审计任务详情页生成并确认练习题",
        )

    kp_ids = list({q.knowledge_point_id for q in questions})
    kps = {
        kp.id: kp
        for kp in db.query(KnowledgePoint).filter(KnowledgePoint.id.in_(kp_ids)).all()
    }
    if req.topic_filter:
        kps = {k: v for k, v in kps.items() if v.key == req.topic_filter}
        if not kps:
            raise HTTPException(status_code=404, detail=f"题库中没有知识点 {req.topic_filter} 的题目")
        questions = [q for q in questions if q.knowledge_point_id in kps]

    states = {
        s.knowledge_point_id: s
        for s in db.query(UserKnowledgeState).filter(
            UserKnowledgeState.user_id == current_user.id,
            UserKnowledgeState.knowledge_point_id.in_(list(kps.keys())),
        ).all()
    }
    # 每题历史作答次数(判新题)
    attempt_counts = dict(
        db.query(Attempt.question_id, sa_func.count(Attempt.id))
        .filter(Attempt.user_id == current_user.id)
        .group_by(Attempt.question_id)
        .all()
    )

    now = _now()
    candidates = []
    for q in questions:
        st = states.get(q.knowledge_point_id)
        candidates.append(CandidateInfo(
            question=q,
            question_id=q.id,
            kp_id=q.knowledge_point_id,
            difficulty=q.difficulty,
            question_attempts=attempt_counts.get(q.id, 0),
            kp_due_at=st.due_at if st else None,
            kp_attempts=st.attempts if st else 0,
            kp_correct_count=st.correct_count if st else 0,
        ))

    ability = _estimate_user_ability(db, current_user.id)
    picked = select_questions(candidates, ability, req.count, now)
    if not picked:
        raise HTTPException(status_code=400, detail="当前筛选条件下没有可出的题目")

    session = PracticeSession(
        user_id=current_user.id,
        question_count=len(picked),
        question_ids=[str(c.question_id) for c in picked],
        stats={
            "ability": ability,
            "topic_filter": req.topic_filter,
            "review_count": sum(
                1 for c in picked if c.kp_due_at is not None and c.kp_due_at <= now
            ),
        },
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    return StartSessionResponse(
        session_id=session.id,
        questions=[
            SessionQuestionResponse(
                id=c.question.id,
                qtype=c.question.qtype.value,
                stem=c.question.stem,
                code_snippet=c.question.code_snippet,
                options=c.question.options,
                difficulty=c.question.difficulty,
                knowledge_name=kps[c.kp_id].name if c.kp_id in kps else None,
            )
            for c in picked
        ],
    )


# ============================================================
# 答题(判分 + SM-2 更新)
# ============================================================


@router.post("/sessions/{session_id}/answers", response_model=SubmitAnswerResponse)
def submit_answer(
    session_id: UUID,
    req: SubmitAnswerRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubmitAnswerResponse:
    session = db.query(PracticeSession).filter(
        PracticeSession.id == session_id,
        PracticeSession.user_id == current_user.id,
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="练习会话不存在")
    if str(req.question_id) not in (session.question_ids or []):
        raise HTTPException(status_code=400, detail="该题不在本次练习中")

    # 同一会话内不允许重复作答
    dup = db.query(Attempt).filter(
        Attempt.session_id == session.id,
        Attempt.question_id == req.question_id,
    ).first()
    if dup:
        raise HTTPException(status_code=409, detail="该题已作答")

    question = db.query(Question).filter(
        Question.id == req.question_id,
        Question.user_id == current_user.id,
    ).first()
    if not question:
        raise HTTPException(status_code=404, detail="题目不存在")
    if req.chosen_idx >= len(question.options or []):
        raise HTTPException(status_code=400, detail="选项下标越界")

    now = _now()
    is_correct = req.chosen_idx == question.answer_idx

    # 本次作答前的会话进度与能力值(避免 autoflush 把本条计入)
    answered_before = (
        db.query(sa_func.count(Attempt.id))
        .filter(Attempt.session_id == session.id)
        .scalar()
        or 0
    )
    ability = _estimate_user_ability(db, current_user.id)

    # 1. 落答题流水
    attempt = Attempt(
        user_id=current_user.id,
        session_id=session.id,
        question_id=question.id,
        chosen_idx=req.chosen_idx,
        is_correct=is_correct,
    )
    db.add(attempt)

    # 2. SM-2 更新知识点记忆状态(get_or_create)
    kp = db.query(KnowledgePoint).filter(
        KnowledgePoint.id == question.knowledge_point_id
    ).first()
    state_row = db.query(UserKnowledgeState).filter(
        UserKnowledgeState.user_id == current_user.id,
        UserKnowledgeState.knowledge_point_id == question.knowledge_point_id,
    ).first()
    if state_row is None:
        state_row = UserKnowledgeState(
            user_id=current_user.id,
            knowledge_point_id=question.knowledge_point_id,
        )
        db.add(state_row)
        db.flush()

    new_state = apply_sm2(
        SM2State(
            ease_factor=state_row.ease_factor,
            interval_days=state_row.interval_days,
            repetitions=state_row.repetitions,
            due_at=state_row.due_at,
            attempts=state_row.attempts,
            correct_count=state_row.correct_count,
            last_quality=state_row.last_quality,
        ),
        quality_from_correct(is_correct),
        now,
    )
    state_row.ease_factor = new_state.ease_factor
    state_row.interval_days = new_state.interval_days
    state_row.repetitions = new_state.repetitions
    state_row.due_at = new_state.due_at
    state_row.attempts = new_state.attempts
    state_row.correct_count = new_state.correct_count
    state_row.last_quality = new_state.last_quality

    # 3. 题目难度微调(能力值用本次作答前的估计)
    question.difficulty = adjust_question_difficulty(
        question.difficulty, ability, is_correct
    )

    # 4. 会话进度 / 收尾
    answered_count = answered_before + 1
    if answered_count >= session.question_count and session.finished_at is None:
        session.finished_at = now

    db.commit()

    accuracy = (
        new_state.correct_count / new_state.attempts if new_state.attempts else None
    )
    return SubmitAnswerResponse(
        is_correct=is_correct,
        correct_idx=question.answer_idx,
        explanation=question.explanation or "",
        state=KnowledgeStateResponse(
            knowledge_key=kp.key if kp else "general",
            knowledge_name=kp.name if kp else "未分类",
            ease_factor=new_state.ease_factor,
            interval_days=new_state.interval_days,
            repetitions=new_state.repetitions,
            due_at=new_state.due_at,
            attempts=new_state.attempts,
            correct_count=new_state.correct_count,
            accuracy=accuracy,
        ),
        answered_count=answered_count,
        total_count=session.question_count,
    )


# ============================================================
# 统计 / 题库管理
# ============================================================


@router.get("/stats", response_model=StatsResponse)
def get_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StatsResponse:
    now = _now()
    states = db.query(UserKnowledgeState).filter(
        UserKnowledgeState.user_id == current_user.id
    ).all()
    kps = {
        kp.id: kp
        for kp in db.query(KnowledgePoint).filter(
            KnowledgePoint.user_id == current_user.id
        ).all()
    }

    total_attempts = sum(s.attempts for s in states)
    total_correct = sum(s.correct_count for s in states)
    due_count = sum(1 for s in states if s.due_at is not None and s.due_at <= now)

    weak_points = []
    for s in states:
        if s.attempts <= 0:
            continue
        kp = kps.get(s.knowledge_point_id)
        weak_points.append(WeakPointItem(
            knowledge_key=kp.key if kp else "general",
            knowledge_name=kp.name if kp else "未分类",
            attempts=s.attempts,
            correct_count=s.correct_count,
            accuracy=round(1.0 - s.correct_count / s.attempts, 4),
            ease_factor=s.ease_factor,
            due_at=s.due_at,
        ))
    # 错误率高且样本多的排前面
    weak_points.sort(key=lambda w: (-w.accuracy, -w.attempts))

    active_count = (
        db.query(sa_func.count(Question.id))
        .filter(
            Question.user_id == current_user.id,
            Question.status == QuestionStatus.ACTIVE,
        )
        .scalar()
        or 0
    )
    draft_count = (
        db.query(sa_func.count(Question.id))
        .filter(
            Question.user_id == current_user.id,
            Question.status == QuestionStatus.DRAFT,
        )
        .scalar()
        or 0
    )

    return StatsResponse(
        ability=_estimate_user_ability(db, current_user.id),
        due_count=due_count,
        total_attempts=total_attempts,
        total_correct=total_correct,
        accuracy=(total_correct / total_attempts) if total_attempts else None,
        weak_points=weak_points[:10],
        active_question_count=active_count,
        draft_question_count=draft_count,
    )


@router.get("/questions", response_model=list[QuestionListItem])
def list_questions(
    status: str | None = Query(default=None, pattern="^(draft|active|archived)$"),
    knowledge_point: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[QuestionListItem]:
    query = db.query(Question).filter(Question.user_id == current_user.id)
    if status:
        query = query.filter(Question.status == QuestionStatus(status))
    questions = query.order_by(Question.created_at.desc()).all()

    if knowledge_point:
        kp_ids = {
            kp.id
            for kp in db.query(KnowledgePoint).filter(
                KnowledgePoint.user_id == current_user.id,
                KnowledgePoint.key == knowledge_point,
            ).all()
        }
        questions = [q for q in questions if q.knowledge_point_id in kp_ids]

    kps = {
        kp.id: kp
        for kp in db.query(KnowledgePoint).filter(
            KnowledgePoint.user_id == current_user.id
        ).all()
    }
    attempt_stats = dict(
        db.query(
            Attempt.question_id,
            sa_func.count(Attempt.id),
            sa_func.sum(cast(Attempt.is_correct, Integer)),
        )
        .filter(Attempt.user_id == current_user.id)
        .group_by(Attempt.question_id)
        .all()
    ) if questions else {}

    items = []
    for q in questions:
        stats = attempt_stats.get(q.id)
        attempts = stats[1] if stats else 0
        correct = stats[2] if stats else 0
        kp = kps.get(q.knowledge_point_id)
        items.append(QuestionListItem(
            id=q.id,
            qtype=q.qtype.value,
            stem=q.stem,
            difficulty=q.difficulty,
            status=q.status.value,
            knowledge_name=kp.name if kp else None,
            attempts=attempts or 0,
            accuracy=(correct / attempts) if attempts else None,
            created_at=q.created_at,
        ))
    return items


@router.post("/questions/{question_id}/archive")
def archive_question(
    question_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """归档题目(不再参与组卷);已是 active/draft 均可归档"""
    question = db.query(Question).filter(
        Question.id == question_id,
        Question.user_id == current_user.id,
    ).first()
    if not question:
        raise HTTPException(status_code=404, detail="题目不存在")
    question.status = QuestionStatus.ARCHIVED
    db.commit()
    return {"archived": True}
