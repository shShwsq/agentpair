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
import json
import logging
import threading
import uuid
from collections.abc import Generator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agents.orchestrator import run_dual_agent_audit
from app.database import SessionLocal, get_db
from app.deps import get_optional_user, get_optional_user_sse
from app.event_bus import subscribe, unsubscribe
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.scenarios.base import list_scenarios
from app.schemas.task import (
    ScenarioInfo,
    TaskCreateRequest,
    TaskCreateResponse,
    TaskListItem,
    TaskResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tasks"])


@router.get("/scenarios", response_model=list[ScenarioInfo])
def list_all_scenarios() -> list[dict[str, str]]:
    """列出所有可用场景(给前端选择用)"""
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

    # 兼容旧 API:只有 repo_url,生成 user_input
    if req.repo_url:
        user_input = f"请审计这个仓库: {req.repo_url}"
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
