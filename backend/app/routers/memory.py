"""长期记忆管理路由

用户可编辑三类记忆:
- 用户偏好(1:1,结构化字段 + 自由文本):影响 user_agent 评判标准与 checklist 生成
- 全局长期记忆(1:1,自由文本):跨项目通用经验,注入 user_agent
- 分项目记忆(1:N,按 repo_url 聚合):注入 react_agent,影响审计方向

端点:
- GET/PUT /memory/preferences       用户偏好
- GET/PUT /memory/global            全局长期记忆
- GET    /memory/projects           项目列表
- GET/PUT/DELETE /memory/projects/{project_id}  单个分项目记忆

鉴权:全部 Depends(get_current_user)。匿名用户无法访问(匿名任务不持久化记忆)。
跨用户隔离:所有查询/更新都带 user_id 过滤,确保用户 A 看不到/改不了用户 B 的数据。

注意:项目(repo_url)由 orchestrator 在任务完成时自动归纳创建(_get_or_create_project),
本路由不提供"新建项目"端点——用户只能编辑/删除已由 agent 自动归纳产生的项目记录。
若用户想手动为某仓库预置记忆,可在任务跑一次后编辑,或后续扩展 POST 端点。
"""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models.project import Project
from app.models.user import User
from app.models.user_memory import UserMemory
from app.models.user_preference import UserPreference
from app.schemas.memory import (
    ProjectListResponse,
    ProjectOut,
    SaveProjectRequest,
    SaveUserMemoryRequest,
    SaveUserPreferenceRequest,
    UserMemoryOut,
    UserPreferenceOut,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/memory", tags=["memory"])


# ============================================================
# 用户偏好(1:1)
# ============================================================


@router.get("/preferences", response_model=UserPreferenceOut)
def get_preferences(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPreferenceOut:
    """获取当前用户的偏好(未配置则返回空默认值)"""
    row = (
        db.query(UserPreference)
        .filter(UserPreference.user_id == current_user.id)
        .first()
    )
    if row is None:
        return UserPreferenceOut()
    return UserPreferenceOut.model_validate(row)


@router.put("/preferences", response_model=UserPreferenceOut)
def save_preferences(
    req: SaveUserPreferenceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserPreferenceOut:
    """保存/更新用户偏好(get_or_create)"""
    row = (
        db.query(UserPreference)
        .filter(UserPreference.user_id == current_user.id)
        .first()
    )
    if row is None:
        row = UserPreference(
            user_id=current_user.id,
            preferences=req.preferences,
            custom_prompt=req.custom_prompt,
        )
        db.add(row)
    else:
        row.preferences = req.preferences
        row.custom_prompt = req.custom_prompt
    db.commit()
    db.refresh(row)
    logger.info("用户 %s 更新了偏好", current_user.id)
    return UserPreferenceOut.model_validate(row)


# ============================================================
# 全局长期记忆(1:1)
# ============================================================


@router.get("/global", response_model=UserMemoryOut)
def get_global_memory(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserMemoryOut:
    """获取当前用户的全局长期记忆(未配置则返回空)"""
    row = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == current_user.id)
        .first()
    )
    if row is None:
        return UserMemoryOut()
    return UserMemoryOut.model_validate(row)


@router.put("/global", response_model=UserMemoryOut)
def save_global_memory(
    req: SaveUserMemoryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserMemoryOut:
    """保存/更新全局长期记忆(get_or_create)"""
    row = (
        db.query(UserMemory)
        .filter(UserMemory.user_id == current_user.id)
        .first()
    )
    if row is None:
        row = UserMemory(user_id=current_user.id, content=req.content)
        db.add(row)
    else:
        row.content = req.content
    db.commit()
    db.refresh(row)
    logger.info("用户 %s 更新了全局长期记忆", current_user.id)
    return UserMemoryOut.model_validate(row)


# ============================================================
# 分项目记忆(1:N)
# ============================================================


@router.get("/projects", response_model=ProjectListResponse)
def list_projects(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectListResponse:
    """获取当前用户的所有项目记忆列表(按更新时间倒序)"""
    rows = (
        db.query(Project)
        .filter(Project.user_id == current_user.id)
        .order_by(Project.updated_at.desc())
        .all()
    )
    return ProjectListResponse(projects=[_project_to_out(r) for r in rows])


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    """获取单个项目记忆详情"""
    row = _find_project(db, current_user.id, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到项目: {project_id}")
    return _project_to_out(row)


@router.put("/projects/{project_id}", response_model=ProjectOut)
def save_project(
    project_id: str,
    req: SaveProjectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectOut:
    """更新项目记忆的 alias/note/memory_content(用户手动编辑)

    不更新 repo_url(归一化值是项目身份,不可改);
    不更新 last_summary_at(那是自动归纳的时间戳,手动编辑不改它)。
    """
    row = _find_project(db, current_user.id, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到项目: {project_id}")
    row.alias = req.alias
    row.note = req.note
    row.memory_content = req.memory_content
    db.commit()
    db.refresh(row)
    logger.info("用户 %s 手动更新了项目记忆 %s", current_user.id, project_id)
    return _project_to_out(row)


@router.delete("/projects/{project_id}", response_model=ProjectListResponse)
def delete_project(
    project_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectListResponse:
    """删除项目记忆(整行删除,含 alias/note/memory_content)"""
    row = _find_project(db, current_user.id, project_id)
    if row is not None:
        db.delete(row)
        db.commit()
        logger.info("用户 %s 删除了项目记忆 %s", current_user.id, project_id)
    # 返回剩余列表
    rows = (
        db.query(Project)
        .filter(Project.user_id == current_user.id)
        .order_by(Project.updated_at.desc())
        .all()
    )
    return ProjectListResponse(projects=[_project_to_out(r) for r in rows])


# ============================================================
# 辅助函数
# ============================================================


def _find_project(db: Session, user_id, project_id: str) -> Project | None:
    """按 project_id + user_id 查(确保跨用户隔离)

    project_id 是 UUID 字符串,解析失败返回 None(404)。
    """
    try:
        pid = UUID(project_id)
    except (ValueError, TypeError):
        return None
    return (
        db.query(Project)
        .filter(
            Project.id == pid,
            Project.user_id == user_id,
        )
        .first()
    )


def _project_to_out(row: Project) -> ProjectOut:
    """Project ORM → ProjectOut(把 uuid/datetime 序列化为字符串)"""
    return ProjectOut(
        id=str(row.id),
        repo_url_normalized=row.repo_url_normalized,
        repo_url_raw=row.repo_url_raw,
        alias=row.alias,
        note=row.note,
        memory_content=row.memory_content,
        last_summary_at=row.last_summary_at.isoformat() if row.last_summary_at else None,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )
