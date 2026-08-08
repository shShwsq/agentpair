"""User Profile (per-user, 1:1)

用户级的稳定倾向(输出语言/关注领域/评判风格 + 自由文本兜底),
注入 user_agent 的 system prompt,影响评判标准与 checklist 生成。

设计:
- 1:1 表(user_id unique),用户首次保存时 get_or_create
- preferences: 结构化 JSONB(易扩展),如 {"output_language":"zh","focus_areas":["security"],"style":"concise"}
- custom_prompt: 自由文本兜底(用户大段自定义偏好/评判标准补充),单独列便于校验与截断
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
    # 结构化偏好(易扩展):
    # {"output_language": "zh", "focus_areas": ["security","perf"], "style": "concise"}
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
