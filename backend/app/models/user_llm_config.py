"""用户模型配置(per-user)

阶段 6:用户可在前端配置自己的 LLM / Embedding 模型,
配置存于数据库,任务执行时按 task.user_id 加载,覆盖默认 env 配置。

设计:
- 一对一关联 User(每个用户一行,首次保存时 upsert)
- llm_config / embedding_config 用 JSONB 存,字段结构与 schemas/settings.py 对齐
- API Key 明文存储(后续可接入字段级加密,当前阶段与 password_hash 同表,访问受鉴权保护)
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserLLMConfig(Base):
    __tablename__ = "user_llm_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 一对一关联用户
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False
    )

    # LLM 配置(provider/api_key/model/enable_thinking/base_url)
    # 为空表示未配置,任务执行时回退到 env 默认配置
    llm_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=None)
    # Embedding 配置(provider/api_key/model/base_url/dimension)
    # 当前阶段仅存储 + 测试连通性,尚未接入向量检索流程
    embedding_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
