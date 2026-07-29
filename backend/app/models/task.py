"""任务模型

通用任务模型,支持多场景。场景标识为字符串,可任意注册新场景。
"""
import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TaskStatus(str, PyEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 阶段 0 暂不鉴权,user_id 可空
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    # 场景标识:字符串,支持任意场景注册(见 app/scenarios/)
    scenario: Mapped[str] = mapped_column(
        String(64), default="code_security_audit", nullable=False
    )
    # 用户原始输入(意图)。通用化:不再固定 repo_url,而是 user_input
    user_input: Mapped[str] = mapped_column(Text, nullable=False)
    # 可选的补充参数(如 repo_url、branch、scope 等),放 metadata
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.PENDING, nullable=False
    )
    # 当前阶段描述,展示给前端
    current_stage: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 失败时的错误信息
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 用户提交任务时选择的 LLM 配置 id(对应 user_llm_configs.llm_configs[].id)
    # 为空表示用 env 默认配置或匿名任务
    llm_config_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

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
    results: Mapped[list["Result"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class Conversation(Base):
    """对话记录:user / user_agent / react_agent 之间的所有消息

    每轮 user_agent 的理解+提问、react_agent 的输出、最终总结,都存这里
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    # 协作轮次(第几轮,从 0 开始;0 = 初始评估)
    round_idx: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 角色:user / user_agent / react_agent
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    # 消息类型:
    #   question(用户提问)
    #   evaluation(user_agent 评估)
    #   followup(user_agent 追问)
    #   thinking(react_agent 思考)
    #   tool_call / tool_result
    #   submit(react_agent 提交结果)
    #   summary(user_agent 最终总结)
    #   error
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 思考链(reasoning_content):仅 type=thinking 有,模型一边想一边输出的临时过程
    # 落库以便刷新页面后仍可查看;其他 type 此字段为 None
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task: Mapped[Task] = relationship(back_populates="conversations")


class Result(Base):
    """任务结果项(通用)

    不再绑定安全审计语义。安全场景下 metadata 可放 cwe/severity/file_path/line_range 等;
    其他场景可自定义 metadata 结构。

    round_idx 记录由哪一轮 react_agent 产出,便于追溯
    """

    __tablename__ = "results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    # 由第几轮 react_agent 产出(从 1 开始)
    round_idx: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # 通用字段
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 场景专用信息放 metadata(JSONB)
    # 安全场景示例:{"cwe": "CWE-89", "severity": "high", "file_path": "src/x.py", "line_range": "42"}
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task: Mapped[Task] = relationship(back_populates="results")
