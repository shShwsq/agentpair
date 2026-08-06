"""分项目记忆(per-user-per-repo, 1:N 与 users)

按 Git 仓库聚合的项目历史(已知问题/审计方向/历史发现),
注入 react_agent 的 system prompt,影响审计方向。

设计:
- 1:N 表(每用户每仓库一行),UNIQUE(user_id, repo_url_normalized)
- repo_url_normalized: 归一化后的 url(由 services/repo_url.normalize_repo_url 处理),
  保证同仓库不同写法(git@/ssh/https/.git/尾部斜杠)映射到同一行
- repo_url_raw: 用户首次输入的原始写法,展示用
- alias/note: 用户给项目起的别名/备注(可空)
- memory_content: 分项目记忆,LLM 增量合并更新
- last_summary_at: 上次自动归纳时间,便于去重与"是否需重新归纳"判断

注意:与 Task 无 FK 关联(Task.params.repo_url 是字符串,归一化后字符串匹配即可)。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("user_id", "repo_url_normalized", name="uq_user_repo"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 归一化后的 repo_url(https://host/path 形式,去 .git,host 小写)
    repo_url_normalized: Mapped[str] = mapped_column(String(512), nullable=False)
    # 原始 repo_url(用户首次输入的写法,展示用)
    repo_url_raw: Mapped[str] = mapped_column(String(512), nullable=False)
    # 用户给项目起的别名(可空)
    alias: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 用户给项目的备注(可空,自由文本)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 分项目记忆(注入 react_agent;LLM 增量合并更新)
    memory_content: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="", default=""
    )
    # 上次自动归纳写入时间(便于"是否需要重新归纳"判断 + 去重)
    last_summary_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
