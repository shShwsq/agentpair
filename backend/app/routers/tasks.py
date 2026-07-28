"""任务路由

阶段 1 实现:
- POST /tasks  提交任务,返回 task_id
- GET /tasks/{task_id}  查询任务状态与结果

注意:阶段 1 仍同步执行 react_agent(阻塞),阶段 9 起会改为 Celery 异步队列
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agents.react_agent import run_react_agent
from app.database import get_db
from app.models.task import Task, TaskScenario, TaskStatus
from app.schemas.task import (
    TaskCreateRequest,
    TaskCreateResponse,
    TaskResponse,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", response_model=TaskCreateResponse, status_code=status.HTTP_201_CREATED)
def create_task(req: TaskCreateRequest, db: Session = Depends(get_db)) -> Task:
    """提交审计任务

    阶段 1:同步执行 react_agent(阻塞,可能耗时几分钟)
    后续阶段会改为异步队列
    """
    task = Task(
        scenario=TaskScenario.CODE_SECURITY_AUDIT,
        repo_url=str(req.repo_url),
        branch=req.branch,
        scope=req.scope,
        status=TaskStatus.PENDING,
        current_stage="已提交,等待执行",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    # 阶段 1:同步跑真实 react_agent
    run_react_agent(task, db)
    db.refresh(task)
    return task


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: uuid.UUID, db: Session = Depends(get_db)) -> Task:
    """查询任务详情,包含对话记录与漏洞发现"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task
