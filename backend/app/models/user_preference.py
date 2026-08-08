"""User Profile (per-user, 1:1)

用户级的稳定倾向(输出语言/关注领域/评判风格 + 自由文本兜底),
注入 user_agent 的 system prompt,影响评判标准与 checklist 生成。

设计:
- 1:1 表(user_id unique),用户首次保存时 get_or_create
- custom_prompt: 自由文本 Markdown(用户在记忆管理页编辑),注入 user_agent
- preferences: 遗留 JSONB 列(DB 保留避免迁移风险),代码不再读写;功能已由 custom_prompt 完全覆盖
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserPreference(Base):
    __tablename__ = "user_preferences"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,  # 1:1
        nullable=False,
    )
    # 遗留结构化偏好列(DB 保留避免迁移风险,代码不再读写;功能由 custom_prompt 覆盖)
    preferences: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}", default=dict
    )
    # 自由文本兜底(用户大段自定义偏好/评判标准补充)
    custom_prompt: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="", default=""
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
