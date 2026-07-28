"""任务路由

阶段 0 实现:
- POST /tasks  提交任务,返回 task_id
- GET /tasks/{task_id}  查询任务状态与结果
- POST /tasks/{task_id}/run  手动触发假 agent 执行(模拟异步)
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agents.fake_runner import run_fake_audit
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

    阶段 0:创建任务记录后立即跑假 agent(同步)
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

    # 阶段 0:同步跑假 agent,后续替换为异步
    run_fake_audit(task, db)
    db.refresh(task)
    return task


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: uuid.UUID, db: Session = Depends(get_db)) -> Task:
    """查询任务详情,包含对话记录与漏洞发现"""
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task
