"""任务路由

阶段 4:双智能体协作(user_agent + react_agent)
阶段 7:异步化 + SSE 实时流

端点:
- GET /tasks  列出当前用户可见的任务(自己 + 匿名)
- POST /tasks  提交任务,立即返回 task_id,后台线程执行
- GET /tasks/{task_id}  查询任务状态与结果
- GET /tasks/{task_id}/stream  SSE 实时事件流(对话/状态/结果/完成)
- GET /scenarios  列出可用场景
"""
import ast
import html
import json
import logging
import threading
import uuid
from collections.abc import Generator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from app.agents.orchestrator import run_dual_agent_audit
from app.database import SessionLocal, get_db
from app.deps import get_optional_user, get_optional_user_sse
from app.event_bus import publish, subscribe, unsubscribe
from app.models.task import Conversation, Task, TaskStatus
from app.models.user import User
from app.pause_controller import (
    clear_pause_state,
    pause_task,
    resume_task,
)
from app.scenarios.base import get_scenario, list_scenarios
from app.schemas.task import (
    AnswerRequest,
    AnswerResponse,
    PendingQuestion,
    ScenarioInfo,
    TaskCreateRequest,
    TaskCreateResponse,
    TaskListItem,
    TaskResponse,
)
from app.user_interaction import (
    get_pending_question,
    submit_answers,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tasks"])


@router.get("/scenarios", response_model=list[ScenarioInfo])
def list_all_scenarios() -> list[dict[str, Any]]:
    """列出所有可用场景(含前端声明:表单字段/结果分组/meta字段/覆盖度看板)"""
    return list_scenarios()


@router.get("/tasks", response_model=list[TaskListItem])
def list_tasks(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> list[Task]:
    """列出当前用户可见的任务(自己的 + 匿名的),按创建时间倒序

    用于侧栏历史任务列表。精简字段(不含对话/结果),降低传输成本。
    """
    query = db.query(Task)
    if current_user:
        # 登录用户:返回自己的任务 + 匿名任务
        query = query.filter(
            (Task.user_id == current_user.id) | (Task.user_id.is_(None))
        )
    else:
        # 未登录:只返回匿名任务
        query = query.filter(Task.user_id.is_(None))
    return (
        query.order_by(Task.created_at.desc())
        .offset(max(0, offset))
        .limit(min(max(1, limit), 100))
        .all()
    )


@router.post("/tasks", response_model=TaskCreateResponse, status_code=status.HTTP_201_CREATED)
def create_task(
    req: TaskCreateRequest,
    current_user: User | None = Depends(get_optional_user),
) -> TaskCreateResponse:
    """提交任务

    阶段 7:异步化。立即创建 task 记录并返回 task_id,
    后台线程执行双智能体协作。前端通过 SSE 端点实时观看进度。

    支持两种提交方式:
    - 通用:scenario + user_input + params
    - 兼容旧 API:传 repo_url,自动转成 user_input + params
    """
    user_input, params = _normalize_request(req)

    # 用独立 session 创建 task(不依赖请求级 session,因为要立即返回)
    db = SessionLocal()
    try:
        task = Task(
            scenario=req.scenario,
            user_input=user_input,
            params=params,
            user_id=current_user.id if current_user else None,
            llm_config_id=req.llm_config_id,
            status=TaskStatus.PENDING,
            current_stage="已提交,等待执行",
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
        task_status = task.status
    finally:
        db.close()

    # 启动后台线程执行(用独立 DB session,线程安全)
    thread = threading.Thread(
        target=_run_task_in_background,
        args=(str(task_id),),
        daemon=True,
        name=f"task-{task_id}",
    )
    thread.start()

    return TaskCreateResponse(id=task_id, status=task_status)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> Task:
    """查询任务详情,包含对话记录与结果

    阶段 6:若任务关联了 user_id,则只允许该用户访问;匿名任务任何人可访问
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 权限:任务归属用户或匿名任务可访问
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权访问此任务")

    return task


# ============================================================
# 阶段 8:用户澄清(user_agent 向用户提问)
# ============================================================


@router.get("/tasks/{task_id}/pending_question")
def get_task_pending_question(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> PendingQuestion | None:
    """查询任务当前待回答的问题(刷新页面后恢复弹窗用)

    纯查数据库,不依赖 in-memory 状态(避免多 worker/时序窗口导致的残留)。
    判定逻辑:最新一条 user_agent question 是否已有对应 ask_round 的 answer。
    无待回答问题时返回 None。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权访问此任务")

    # 任务已结束,不恢复弹窗
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
        return None

    # 查最新一条 user_agent question(提问记录,reasoning 存 JSON payload)
    latest_question = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.role == "user_agent",
            Conversation.type == "question",
        )
        .order_by(Conversation.created_at.desc())
        .first()
    )
    if not latest_question or not latest_question.reasoning:
        return None

    try:
        payload = json.loads(latest_question.reasoning)
    except (json.JSONDecodeError, TypeError):
        return None

    ask_round = payload.get("ask_round", 0)

    # 查最新一条 user answer,若其 ask_round >= 当前提问的 ask_round,说明已回答
    latest_answer = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.role == "user",
            Conversation.type == "answer",
        )
        .order_by(Conversation.created_at.desc())
        .first()
    )
    if latest_answer and latest_answer.reasoning:
        try:
            ans_data = json.loads(latest_answer.reasoning)
            if int(ans_data.get("ask_round", -1)) >= ask_round:
                return None  # 已回答,不弹窗
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    return PendingQuestion(
        ask_round=ask_round,
        questions=payload.get("questions", []),
        reasoning=payload.get("reasoning", ""),
        conversation_id=str(latest_question.id),
    )


@router.post("/tasks/{task_id}/answer", response_model=AnswerResponse)
def submit_task_answer(
    task_id: uuid.UUID,
    req: AnswerRequest,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> AnswerResponse:
    """提交用户对澄清问题的答案

    同步落库 answer(确保刷新时数据库已有记录),再唤醒后台线程。
    若当前 task 没有待回答问题(重复提交或任务已结束),返回 accepted=false。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    if task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.PAUSED):
        return AnswerResponse(
            accepted=False,
            message=f"任务已结束({task.status.value}),无法提交答案",
        )

    # 拿 pending payload(含 questions,用于落库 answer)
    payload = get_pending_question(task_id)
    if payload is None:
        return AnswerResponse(
            accepted=False,
            message="当前没有待回答的问题(可能已回答或任务已结束)",
        )

    # 转换为 dict 列表(submit_answers 期望的格式)
    answers = [
        {"question_id": a.question_id, "value": a.value}
        for a in req.answers
    ]
    ask_round = payload.get("ask_round", 0)
    questions = payload.get("questions", [])

    # 同步落库 answer(在唤醒后台线程之前,确保刷新时数据库已有记录)。
    _record_answer(db, task, questions, answers, ask_round)

    # 唤醒后台线程
    ok = submit_answers(task_id, answers)
    if not ok:
        return AnswerResponse(
            accepted=False,
            message="提交失败:可能已被回答过或状态异常",
        )
    return AnswerResponse(accepted=True, message="答案已提交,智能体将继续评估")


def _record_answer(
    db: Session,
    task: Task,
    questions: list[dict],
    answers: list[dict],
    ask_round: int,
) -> None:
    """同步落库用户答案为 Conversation(role=user, type=answer)

    逻辑与 orchestrator._record_user_answer 一致,迁移到 API 端点同步执行。
    """
    answer_map: dict[str, dict] = {}
    for a in answers:
        qid = a.get("question_id")
        if qid:
            answer_map[qid] = a

    parts = []
    for i, q in enumerate(questions, 1):
        qid = q.get("id", f"q_{i}")
        q_text = q.get("question", f"问题 {i}")
        a = answer_map.get(qid)
        if a is None:
            continue
        value = a.get("value")
        if value is None or value == "":
            continue
        if isinstance(value, list):
            value_text = ", ".join(str(v) for v in value)
        else:
            value_text = str(value)
        if qid == "_supplement" and not value_text.strip():
            continue
        parts.append(f"Q: {q_text}\nA: {value_text}")

    content = "\n\n".join(parts) if parts else "(用户未填写有效答案)"

    conv = Conversation(
        task_id=task.id,
        round_idx=0,
        role="user",
        type="answer",
        content=content,
        reasoning=json.dumps(
            {"ask_round": ask_round, "answers": answers},
            ensure_ascii=False,
        ),
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    publish(task.id, "conversation", {
        "id": str(conv.id),
        "round_idx": conv.round_idx,
        "role": conv.role,
        "type": conv.type,
        "content": conv.content,
        "reasoning": conv.reasoning,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
    })


# ============================================================
# 任务暂停/恢复
# ============================================================


@router.post("/tasks/{task_id}/pause")
def pause_task_endpoint(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    """暂停正在运行的任务

    后台线程会在下一个检查点(迭代边界/工具调用前)阻塞。
    立即把 task.status 改为 PAUSED 并推送 status 事件,
    前端据此把"暂停"按钮变成"恢复"按钮。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    if task.status != TaskStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"任务状态为 {task.status.value},仅 RUNNING 可暂停",
        )

    # 标记 in-memory 暂停门控(后台线程下一次检查时会阻塞)
    pause_task(task.id)
    # 持久化状态变更 + 推送事件(前端立即看到 UI 切换)
    task.status = TaskStatus.PAUSED
    task.current_stage = "已暂停(等待恢复)"
    db.commit()
    _publish_task_status(task)
    return {"status": task.status.value, "message": "任务已暂停"}


@router.post("/tasks/{task_id}/resume")
def resume_task_endpoint(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    """恢复已暂停的任务

    唤醒在检查点阻塞的后台线程,task.status 改回 RUNNING。
    current_stage 恢复到暂停前的描述不现实(已覆盖),改为通用提示。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权操作此任务")

    if task.status != TaskStatus.PAUSED:
        raise HTTPException(
            status_code=409,
            detail=f"任务状态为 {task.status.value},仅 PAUSED 可恢复",
        )

    # 唤醒后台线程(若已阻塞在 wait_if_paused)
    resume_task(task.id)
    task.status = TaskStatus.RUNNING
    task.current_stage = "已恢复,继续执行"
    db.commit()
    _publish_task_status(task)
    return {"status": task.status.value, "message": "任务已恢复"}


def _publish_task_status(task: Task) -> None:
    """推送任务状态变更事件(供 pause/resume 端点复用)"""
    publish(task.id, "status", {
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "current_stage": task.current_stage,
    })


@router.get("/tasks/{task_id}/coverage")
def get_task_coverage(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> dict[str, Any]:
    """覆盖度看板:从 user_agent 最新一轮 evaluation 解析各维度覆盖状态

    仅当任务场景声明了 coverage 时可用。维度定义来自场景声明,
    覆盖状态来自最新一条 user_agent evaluation 的 covered/missing。
    """
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权访问此任务")

    result = _compute_task_coverage(task, db)
    if result is None:
        raise HTTPException(status_code=404, detail="该场景无覆盖度看板")
    return result


def _compute_task_coverage(task: Task, db: Session) -> dict[str, Any] | None:
    """计算任务覆盖度(供 coverage 端点和报告导出共用)

    返回各维度覆盖状态 dict;场景无 coverage 声明时返回 None。
    """
    try:
        scenario = get_scenario(task.scenario)
    except ValueError:
        return None
    coverage_decl = getattr(scenario, "coverage", None)
    if not coverage_decl:
        return None
    dimensions_decl = coverage_decl.get("dimensions", [])

    # 取最新一条 user_agent evaluation
    latest_eval = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task.id,
            Conversation.role == "user_agent",
            Conversation.type == "evaluation",
        )
        .order_by(Conversation.round_idx.desc(), Conversation.created_at.desc())
        .first()
    )

    covered_set, _missing_set = set(), set()
    last_round = None
    if latest_eval and latest_eval.reasoning:
        covered_set, _missing_set = _parse_evaluation_reasoning(latest_eval.reasoning)
        last_round = latest_eval.round_idx

    dims = [
        {
            "id": d["id"],
            "name": d["name"],
            "description": d.get("description", ""),
            "covered": d["id"] in covered_set,
        }
        for d in dimensions_decl
    ]
    covered_count = sum(1 for d in dims if d["covered"])
    return {
        "dimensions": dims,
        "covered_count": covered_count,
        "total_count": len(dims),
        "last_round": last_round,
    }


def _parse_evaluation_reasoning(reasoning: str) -> tuple[set[str], set[str]]:
    """从 user_agent evaluation 的 reasoning 文本解析 covered/missing id 集合

    reasoning 由 orchestrator._record_user_agent 拼接,格式:
        [user_agent 第 X 轮评估]
        已覆盖: ['injection', 'auth']
        未覆盖: ['crypto', 'deps']
        ...
    元素可能是 str 或 dict{'id': ...}
    """
    covered: set[str] = set()
    missing: set[str] = set()
    for line in reasoning.splitlines():
        stripped = line.strip()
        if stripped.startswith("已覆盖:"):
            covered = _extract_ids(stripped[len("已覆盖:"):].strip())
        elif stripped.startswith("未覆盖:"):
            missing = _extract_ids(stripped[len("未覆盖:"):].strip())
    return covered, missing


def _extract_ids(s: str) -> set[str]:
    """从 Python list repr 提取 id 集合,元素可能是 str 或 dict{'id':...}"""
    try:
        val = ast.literal_eval(s)
    except (ValueError, SyntaxError):
        return set()
    if not isinstance(val, list):
        return set()
    ids: set[str] = set()
    for item in val:
        if isinstance(item, str):
            ids.add(item)
        elif isinstance(item, dict) and "id" in item:
            ids.add(str(item["id"]))
    return ids


# ============================================================
# 报告导出(Markdown / HTML)
# ============================================================


@router.get("/tasks/{task_id}/export")
def export_task_report(
    task_id: uuid.UUID,
    format: str = "markdown",
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> Response:
    """导出任务报告

    format=markdown:返回 .md 附件下载
    format=html:返回打印友好的完整 HTML(前端用于新窗口打印为 PDF)
    """
    if format not in ("markdown", "html"):
        raise HTTPException(status_code=400, detail="format 仅支持 markdown / html")

    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权访问此任务")

    coverage = _compute_task_coverage(task, db)

    if format == "markdown":
        body = _build_markdown_report(task, db, coverage)
        return Response(
            content=body.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="task-{task.id}.md"',
            },
        )
    # html
    body = _build_html_report(task, db, coverage)
    return Response(content=body.encode("utf-8"), media_type="text/html; charset=utf-8")


def _build_markdown_report(
    task: Task, db: Session, coverage: dict[str, Any] | None
) -> str:
    """生成 Markdown 报告:任务信息 + 覆盖度 + 结果清单(按场景分组)"""
    lines: list[str] = []
    lines.append("# 任务报告")
    lines.append("")
    status_val = task.status.value if hasattr(task.status, "value") else str(task.status)
    lines.append(f"- 任务 ID: `{task.id}`")
    lines.append(f"- 场景: {task.scenario}")
    lines.append(f"- 状态: {status_val}")
    lines.append(f"- 创建时间: {task.created_at}")
    if task.completed_at:
        lines.append(f"- 完成时间: {task.completed_at}")
    if task.current_stage:
        lines.append(f"- 当前阶段: {task.current_stage}")
    if task.error_message:
        lines.append(f"- 错误信息: {task.error_message}")
    lines.append("")
    lines.append("## 用户意图")
    lines.append("")
    lines.append(task.user_input)
    lines.append("")

    # 覆盖度
    if coverage:
        lines.append("## 覆盖度")
        lines.append("")
        round_hint = (
            f"(第 {coverage['last_round']} 轮评估)" if coverage.get("last_round") is not None else ""
        )
        lines.append(
            f"已覆盖 {coverage['covered_count']}/{coverage['total_count']} {round_hint}"
        )
        lines.append("")
        for d in coverage["dimensions"]:
            mark = "x" if d["covered"] else " "
            desc = f": {d['description']}" if d.get("description") else ""
            lines.append(f"- [{mark}] {d['name']}{desc}")
        lines.append("")

    # 结果清单(按场景声明分组)
    results = list(task.results)
    if results:
        scenario = get_scenario(task.scenario)
        grouping = getattr(scenario, "result_grouping", None)
        meta_fields = getattr(scenario, "result_meta_fields", [])
        lines.append("## 结果清单")
        lines.append("")
        if grouping:
            _append_grouped_results_md(lines, results, grouping, meta_fields)
        else:
            for r in results:
                _append_result_md(lines, r, meta_fields)

    return "\n".join(lines)


def _append_grouped_results_md(
    lines: list[str],
    results: list,
    grouping: dict[str, Any],
    meta_fields: list[dict[str, Any]],
) -> None:
    """按场景声明 result_grouping 分组追加结果到 Markdown"""
    buckets: dict[str, list] = {}
    field = grouping["field"]
    for r in results:
        md = r.metadata_ or {}
        val = md.get(field)
        key = val if val else "__default__"
        buckets.setdefault(key, []).append(r)

    if grouping["type"] == "ordered":
        for v in sorted(grouping.get("values", []), key=lambda x: x.get("order", 0)):
            rs = buckets.get(v["value"], [])
            if rs:
                lines.append(f"### {v['label']} ({len(rs)})")
                lines.append("")
                for r in rs:
                    _append_result_md(lines, r, meta_fields)
        default_rs = buckets.get("__default__", [])
        if default_rs:
            lines.append(f"### {grouping['default_label']} ({len(default_rs)})")
            lines.append("")
            for r in default_rs:
                _append_result_md(lines, r, meta_fields)
    else:
        for key, rs in buckets.items():
            label = grouping["default_label"] if key == "__default__" else key
            lines.append(f"### {label} ({len(rs)})")
            lines.append("")
            for r in rs:
                _append_result_md(lines, r, meta_fields)


def _append_result_md(
    lines: list[str], r, meta_fields: list[dict[str, Any]]
) -> None:
    """追加单个结果到 Markdown"""
    lines.append(f"#### {r.title}")
    lines.append("")
    lines.append(r.content or "(无内容)")
    lines.append("")
    md = r.metadata_ or {}
    if meta_fields and md:
        parts = []
        for f in meta_fields:
            v = md.get(f["name"])
            if v:
                parts.append(f"{f['label']}: {v}")
        if parts:
            lines.append("> " + " | ".join(parts))
            lines.append("")
    lines.append(f"_第 {r.round_idx} 轮产出_")
    lines.append("")


def _build_html_report(
    task: Task, db: Session, coverage: dict[str, Any] | None
) -> str:
    """生成打印友好的 HTML 报告(前端新窗口打印为 PDF)"""
    status_val = task.status.value if hasattr(task.status, "value") else str(task.status)
    parts: list[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="zh-CN"><head><meta charset="utf-8">')
    parts.append(f"<title>任务报告 - {task.id}</title>")
    parts.append("<style>")
    parts.append(
        "body{font-family:-apple-system,'Segoe UI',sans-serif;max-width:900px;"
        "margin:32px auto;padding:0 24px;color:#1a1a1a;line-height:1.6;}"
        "h1{font-size:24px;border-bottom:2px solid #2563eb;padding-bottom:8px;}"
        "h2{font-size:18px;margin-top:32px;border-left:4px solid #2563eb;padding-left:8px;}"
        "h3{font-size:15px;margin-top:20px;color:#374151;}"
        "h4{font-size:14px;margin:12px 0 4px;}"
        ".meta{color:#6b7280;font-size:13px;}"
        ".meta div{margin:2px 0;}"
        ".intent{background:#f9fafb;padding:12px 16px;border-radius:6px;"
        "white-space:pre-wrap;font-size:14px;}"
        ".coverage-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px;margin-top:12px;}"
        ".cov{padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px;}"
        ".cov.yes{border-color:#16a34a;background:#f0fdf4;}"
        ".cov.no{border-color:#e5e7eb;background:#f9fafb;}"
        ".dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;}"
        ".dot.yes{background:#16a34a;}.dot.no{background:#9ca3af;}"
        ".result{margin:12px 0;padding:12px 16px;background:#fafafa;border-radius:6px;break-inside:avoid;}"
        ".result h4{margin:0 0 6px;}"
        ".result .rmeta{font-size:12px;color:#6b7280;margin:6px 0;}"
        ".result .round{font-size:11px;color:#9ca3af;}"
        "@media print{body{margin:0;max-width:none;}}"
    )
    parts.append("</style></head><body>")

    parts.append("<h1>任务报告</h1>")
    parts.append('<div class="meta">')
    parts.append(f"<div>任务 ID:<code>{task.id}</code></div>")
    parts.append(f"<div>场景:{html.escape(task.scenario)}</div>")
    parts.append(f"<div>状态:{html.escape(status_val)}</div>")
    parts.append(f"<div>创建时间:{task.created_at}</div>")
    if task.completed_at:
        parts.append(f"<div>完成时间:{task.completed_at}</div>")
    if task.current_stage:
        parts.append(f"<div>当前阶段:{html.escape(task.current_stage)}</div>")
    if task.error_message:
        parts.append(f"<div>错误信息:{html.escape(task.error_message)}</div>")
    parts.append("</div>")

    parts.append("<h2>用户意图</h2>")
    parts.append(f'<div class="intent">{html.escape(task.user_input)}</div>')

    # 覆盖度
    if coverage:
        parts.append("<h2>覆盖度</h2>")
        round_hint = (
            f"(第 {coverage['last_round']} 轮评估)"
            if coverage.get("last_round") is not None
            else ""
        )
        parts.append(
            f'<p>已覆盖 {coverage["covered_count"]}/{coverage["total_count"]} '
            f"{html.escape(round_hint)}</p>"
        )
        parts.append('<div class="coverage-grid">')
        for d in coverage["dimensions"]:
            cls = "yes" if d["covered"] else "no"
            parts.append(f'<div class="cov {cls}">')
            parts.append(f'<span class="dot {cls}"></span>')
            parts.append(html.escape(d["name"]))
            parts.append("</div>")
        parts.append("</div>")

    # 结果清单
    results = list(task.results)
    if results:
        scenario = get_scenario(task.scenario)
        grouping = getattr(scenario, "result_grouping", None)
        meta_fields = getattr(scenario, "result_meta_fields", [])
        parts.append("<h2>结果清单</h2>")
        if grouping:
            _append_grouped_results_html(parts, results, grouping, meta_fields)
        else:
            for r in results:
                _append_result_html(parts, r, meta_fields)

    parts.append("</body></html>")
    return "\n".join(parts)


def _append_grouped_results_html(
    parts: list[str],
    results: list,
    grouping: dict[str, Any],
    meta_fields: list[dict[str, Any]],
) -> None:
    """按场景声明 result_grouping 分组追加结果到 HTML"""
    buckets: dict[str, list] = {}
    field = grouping["field"]
    for r in results:
        md = r.metadata_ or {}
        val = md.get(field)
        key = val if val else "__default__"
        buckets.setdefault(key, []).append(r)

    def _emit_group(label: str, rs: list) -> None:
        parts.append(f"<h3>{html.escape(label)} ({len(rs)})</h3>")
        for r in rs:
            _append_result_html(parts, r, meta_fields)

    if grouping["type"] == "ordered":
        for v in sorted(grouping.get("values", []), key=lambda x: x.get("order", 0)):
            rs = buckets.get(v["value"], [])
            if rs:
                _emit_group(v["label"], rs)
        default_rs = buckets.get("__default__", [])
        if default_rs:
            _emit_group(grouping["default_label"], default_rs)
    else:
        for key, rs in buckets.items():
            label = grouping["default_label"] if key == "__default__" else key
            _emit_group(label, rs)


def _append_result_html(
    parts: list[str], r, meta_fields: list[dict[str, Any]]
) -> None:
    """追加单个结果到 HTML"""
    parts.append('<div class="result">')
    parts.append(f"<h4>{html.escape(r.title or '(无标题)')}</h4>")
    parts.append(f"<div>{html.escape(r.content or '(无内容)')}</div>")
    md = r.metadata_ or {}
    if meta_fields and md:
        mp = []
        for f in meta_fields:
            v = md.get(f["name"])
            if v:
                mp.append(f"{html.escape(f['label'])}: {html.escape(str(v))}")
        if mp:
            parts.append(f'<div class="rmeta">{" | ".join(mp)}</div>')
    parts.append(f'<div class="round">第 {r.round_idx} 轮产出</div>')
    parts.append("</div>")


@router.get("/tasks/{task_id}/stream")
def stream_task_events(
    task_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user_sse),
) -> StreamingResponse:
    """SSE 端点:实时推送任务事件

    事件类型:
    - conversation: 新对话消息(user_agent/react_agent 的每一步)
    - status: 任务状态变更(进入新阶段)
    - thinking_delta: LLM 流式 token 增量(打字机效果)
    - done: 任务完成(终止事件)
    - error: 任务失败(终止事件)

    前端用 EventSource 连接,每条事件 data 字段是 JSON。
    """
    # 鉴权 + 任务存在性检查
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id is not None:
        if current_user is None or current_user.id != task.user_id:
            raise HTTPException(status_code=403, detail="无权访问此任务")

    task_id_str = str(task_id)

    def event_generator() -> Generator[str, None, None]:
        """SSE 事件生成器"""
        q = subscribe(task_id_str)
        try:
            # 先推送一个 connected 事件(带当前状态,前端可据此判断是否已结束)
            connected_event = {
                "type": "connected",
                "task_id": task_id_str,
                "data": {
                    "status": task.status.value if hasattr(task.status, 'value') else str(task.status),
                    "current_stage": task.current_stage,
                },
                "timestamp": "",
            }
            yield _format_sse(connected_event)

            # 若任务已结束,直接推一个终止事件然后关闭
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                done_event = {
                    "type": "done" if task.status == TaskStatus.COMPLETED else "error",
                    "task_id": task_id_str,
                    "data": {"status": task.status.value},
                    "timestamp": "",
                }
                yield _format_sse(done_event)
                return

            # 阻塞读事件队列,直到收到 done/error
            while True:
                # 检查客户端是否断开
                if await_request_disconnect(request):
                    logger.info(f"[task={task_id_str}] SSE 客户端断开")
                    break

                try:
                    # 超时 15 秒无事件,发心跳保持连接
                    event = q.get(timeout=15)
                except Exception:
                    # queue.Empty 是正常的,发心跳
                    yield ": heartbeat\n\n"
                    continue

                yield _format_sse(event)

                # 终止事件:结束循环
                if event.get("type") in ("done", "error"):
                    break
        finally:
            unsubscribe(task_id_str, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Nginx:禁用缓冲,确保实时推送
        },
    )


# ============================================================
# 后台任务执行(独立线程 + 独立 DB session)
# ============================================================


def _run_task_in_background(task_id: str) -> None:
    """后台线程执行双智能体协作

    用独立的 DB session(线程安全),执行完毕后关闭。
    """
    db = SessionLocal()
    try:
        task = db.get(Task, uuid.UUID(task_id))
        if not task:
            logger.error(f"后台任务:task {task_id} 不存在")
            return
        run_dual_agent_audit(task, db)
    except Exception as e:
        logger.exception(f"[task={task_id}] 后台执行失败")
        # 兜底:确保 task 状态被标记为失败
        try:
            task = db.get(Task, uuid.UUID(task_id))
            if task and task.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                task.status = TaskStatus.FAILED
                task.error_message = str(e)[:1000]
                task.current_stage = "执行失败"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
        # 清理 in-memory 暂停状态(防止任务结束但状态卡住)
        clear_pause_state(task_id)


# ============================================================
# 辅助函数
# ============================================================


def _format_sse(event: dict) -> str:
    """格式化为 SSE 事件字符串

    格式:
    event: <type>
    data: <json>

    """
    event_type = event.get("type", "message")
    data = json.dumps(event, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {data}\n\n"


def await_request_disconnect(request: Request) -> bool:
    """检查请求是否已断开(客户端关闭连接)

    FastAPI/Starlette 的 Request.is_disconnected() 是 async 方法,
    但我们在同步生成器里,用 anyio.from_thread 桥接。
    """
    try:
        import anyio
        return anyio.from_thread.run(request.is_disconnected)
    except Exception:
        return False


def _normalize_request(req: TaskCreateRequest) -> tuple[str, dict | None]:
    """把请求归一化为 (user_input, params)

    - 通用方式:直接用 scenario + user_input + params
    - 兼容旧 API:传了 repo_url 但没传 user_input,自动生成
    """
    if req.user_input:
        # 通用方式:直接用
        params = req.params
        # 若同时传了 repo_url 等,合并到 params
        if req.repo_url:
            params = dict(params or {})
            params["repo_url"] = str(req.repo_url)
            if req.branch:
                params["branch"] = req.branch
            if req.scope:
                params["scope"] = req.scope
        return req.user_input, params

    # 兼容旧 API:只有 repo_url,生成通用 user_input(场景无关)
    if req.repo_url:
        user_input = f"请处理这个仓库: {req.repo_url}"
        params = dict(req.params or {})
        params["repo_url"] = str(req.repo_url)
        if req.branch:
            params["branch"] = req.branch
        if req.scope:
            params["scope"] = req.scope
        return user_input, params

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="必须提供 user_input 或 repo_url",
    )
