"""用户模型

邮箱密码注册登录,GitHub 关联后续作为可选绑定(见 spec 8.5)
阶段 6 实现:JWT 鉴权 + 邮箱验证 + 重置密码 + GitHub OAuth
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    # bcrypt 加盐哈希
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 邮箱验证时间(为空表示未验证,登录受限)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # GitHub 绑定(为空表示未绑定)
    github_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
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

    # 便捷属性(非数据库字段)
    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None
