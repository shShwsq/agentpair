"""任务路由

阶段 4 实现:
- POST /tasks  提交任务,返回 task_id
- GET /tasks/{task_id}  查询任务状态与结果
- GET /scenarios  列出可用场景

阶段 4:切换为双智能体协作(user_agent + react_agent)
注意:仍同步执行(阻塞),阶段 9 起会改为 Celery 异步队列
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agents.orchestrator import run_dual_agent_audit
from app.database import get_db
from app.deps import get_optional_user
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.scenarios.base import list_scenarios
from app.schemas.task import (
    ScenarioInfo,
    TaskCreateRequest,
    TaskCreateResponse,
    TaskResponse,
)

router = APIRouter(tags=["tasks"])


@router.get("/scenarios", response_model=list[ScenarioInfo])
def list_all_scenarios() -> list[dict[str, str]]:
    """列出所有可用场景(给前端选择用)"""
    return list_scenarios()


@router.post("/tasks", response_model=TaskCreateResponse, status_code=status.HTTP_201_CREATED)
def create_task(
    req: TaskCreateRequest,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> Task:
    """提交任务

    阶段 4:双智能体协作(user_agent 驱动 react_agent 多轮追问)

    支持两种提交方式:
    - 通用:scenario + user_input + params
    - 兼容旧 API:传 repo_url,自动转成 user_input + params

    阶段 6:可选鉴权,有 token 则关联 user_id,无 token 也允许匿名提交
    """
    user_input, params = _normalize_request(req)

    task = Task(
        scenario=req.scenario,
        user_input=user_input,
        params=params,
        user_id=current_user.id if current_user else None,
        status=TaskStatus.PENDING,
        current_stage="已提交,等待执行",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # 阶段 4:双智能体协作
    run_dual_agent_audit(task, db)
    db.refresh(task)
    return task


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
# 辅助:请求归一化
# ============================================================


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
