"""任务模型

代码安全审计任务,阶段 0 仅建表,不接 agent
"""
import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TaskStatus(str, PyEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskScenario(str, PyEnum):
    CODE_SECURITY_AUDIT = "code_security_audit"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 阶段 0 暂不鉴权,user_id 可空
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    scenario: Mapped[TaskScenario] = mapped_column(
        Enum(TaskScenario), default=TaskScenario.CODE_SECURITY_AUDIT, nullable=False
    )
    repo_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scope: Mapped[str | None] = mapped_column(String(1024), nullable=True)  # 审计范围/目录

    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.PENDING, nullable=False
    )
    # 当前阶段描述,展示给前端(如"正在克隆仓库"、"user_agent 评估中")
    current_stage: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 失败时的错误信息
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 关联
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    findings: Mapped[list["Finding"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class Conversation(Base):
    """对话记录:user / user_agent / react_agent 之间的所有消息"""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )

    # 角色:user(用户)/ user_agent / react_agent
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    # 消息类型:question / answer / tool_call / tool_result / finding / followup
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task: Mapped[Task] = relationship(back_populates="conversations")


class Finding(Base):
    """漏洞发现"""

    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )

    # CWE 类别
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    # 严重度:info / low / medium / high / critical
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    line_range: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    remediation: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 验证状态:true / false / unverified
    verified: Mapped[str] = mapped_column(
        String(16), default="unverified", nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task: Mapped[Task] = relationship(back_populates="findings")
