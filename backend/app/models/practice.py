"""练习模块数据模型(题库 / 知识点 / 遗忘曲线状态 / 答题流水)

设计:
- 题目来源于审计任务的真实发现(Result),由 LLM 改编为客观题(draft),
  用户预览确认后转 active 进入题库
- 知识点按 CWE 编号归类(无 CWE 时回退分类),per-user 隔离
- user_knowledge_states 承载 SM-2 遗忘曲线调度状态(ease_factor / interval / due_at)
- attempts 记录每次作答,供薄弱点统计与能力值估计

全新表,随 Base.metadata.create_all 自动建表,无需迁移函数。
"""
import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class QuestionType(str, PyEnum):
    SINGLE_CHOICE = "single_choice"
    TRUE_FALSE = "true_false"


class QuestionStatus(str, PyEnum):
    # LLM 刚生成,待用户预览确认
    DRAFT = "draft"
    # 用户确认,进入选题池
    ACTIVE = "active"
    # 用户归档,不再参与选题
    ARCHIVED = "archived"


class KnowledgePoint(Base):
    """知识点(选题与遗忘曲线调度的最小单元)

    key 优先取 CWE 编号(如 "CWE-89"),来自 Result.metadata.cwe;
    无 CWE 时回退漏洞分类(如 "injection" / "auth" / "secrets")。
    per-user 唯一。
    """

    __tablename__ = "knowledge_points"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_knowledge_point_user_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    # 知识点唯一键:"CWE-89" / "injection" 等
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    # 展示名(如 "SQL 注入" / "CWE-89 SQL 注入")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 粗分类(前端分组展示用,如 injection / auth / crypto)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Question(Base):
    """练习题(客观题:单选 / 判断)

    由审计任务的 Result 经 LLM 改编生成;dedup_hash = sha256(stem + code_snippet),
    用于同用户下防重复生成。
    """

    __tablename__ = "practice_questions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    # 来源追溯(可空:手工导入等未来扩展)
    source_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    source_result_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("results.id", ondelete="SET NULL"), nullable=True
    )
    knowledge_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_points.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    qtype: Mapped[QuestionType] = mapped_column(Enum(QuestionType), nullable=False)
    # 题干
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    # 相关代码片段(可空)
    code_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 选项列表(判断题固定为 ["正确", "错误"])
    options: Mapped[list] = mapped_column(JSONB, nullable=False)
    # 正确选项下标
    answer_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    # 答案解析
    explanation: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    # 难度 1-5(LLM 初评,作答后微调)
    difficulty: Mapped[float] = mapped_column(Float, nullable=False, default=3.0)
    status: Mapped[QuestionStatus] = mapped_column(
        Enum(QuestionStatus), default=QuestionStatus.DRAFT, nullable=False, index=True
    )
    # sha256(stem + code_snippet),同用户去重
    dedup_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserKnowledgeState(Base):
    """用户对单个知识点的 SM-2 记忆状态(遗忘曲线核心)

    SM-2 参数:ease_factor(≥1.3) / interval_days / repetitions / due_at。
    首次作答该知识点时创建。
    """

    __tablename__ = "user_knowledge_states"
    __table_args__ = (
        UniqueConstraint("user_id", "knowledge_point_id", name="uq_knowledge_state_user_kp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    knowledge_point_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_points.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    ease_factor: Mapped[float] = mapped_column(Float, nullable=False, default=2.5)
    interval_days: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 下次到期复习时间(None = 尚未建立记忆轨迹)
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 作答统计(薄弱点分析用)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correct_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 最近一次作答质量(SM-2 quality:0-5)
    last_quality: Mapped[int | None] = mapped_column(Integer, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PracticeSession(Base):
    """一次练习会话(按需即时组卷)

    question_ids 保存本次组卷选中的题目 id 列表(JSON 字符串数组),
    全部作答完成后写 finished_at。
    """

    __tablename__ = "practice_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 组卷选中的题目 id(str 列表)
    question_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # 组卷时的策略快照(能力值 / 各知识点状态),供复盘
    stats: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Attempt(Base):
    """单次作答流水(薄弱点统计 / 能力值估计 / 错题回溯)"""

    __tablename__ = "practice_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("practice_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("practice_questions.id", ondelete="CASCADE"),
        nullable=False, index=True
    )

    chosen_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    is_correct: Mapped[bool] = mapped_column(nullable=False)

    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
