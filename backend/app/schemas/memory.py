"""长期记忆管理相关的 Pydantic schema

对应 /memory 系列 API:
- UserPreferenceOut/SaveUserPreferenceRequest:用户偏好(1:1)
- UserMemoryOut/SaveUserMemoryRequest:全局长期记忆(1:1)
- ProjectOut/SaveProjectRequest/ProjectListResponse:分项目记忆(1:N)

大小校验在 schema 层做第一道防线(写入时):
- custom_prompt ≤ 8000
- content(全局记忆) ≤ 20000
- memory_content(项目记忆) ≤ 20000

后续还有合并时截断(项目 8000 / 全局 10000)与注入时截断(2000)两道防线。
"""
from datetime import datetime

from pydantic import BaseModel, Field


class UserPreferenceOut(BaseModel):
    """用户偏好响应(GET /memory/preferences)"""

    # 结构化偏好,如 {"output_language":"zh","focus_areas":["security"],"style":"concise"}
    preferences: dict = Field(default_factory=dict)
    # 自由文本兜底(用户大段自定义偏好/评判标准补充)
    custom_prompt: str = ""
    # 最后更新时间(可空 — 未配置时为 None;FastAPI 序列化为 ISO 字符串)
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class SaveUserPreferenceRequest(BaseModel):
    """保存用户偏好请求(PUT /memory/preferences)"""

    preferences: dict = Field(default_factory=dict)
    custom_prompt: str = Field(default="", max_length=8000)


class UserMemoryOut(BaseModel):
    """全局长期记忆响应(GET /memory/global)"""

    content: str = ""
    # 最后更新时间(可空 — 未配置时为 None;FastAPI 序列化为 ISO 字符串)
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class SaveUserMemoryRequest(BaseModel):
    """保存全局长期记忆请求(PUT /memory/global)"""

    content: str = Field(default="", max_length=20000)


class ProjectOut(BaseModel):
    """分项目记忆响应(GET /memory/projects、GET/PUT /memory/projects/{id})"""

    id: str
    repo_url_normalized: str
    repo_url_raw: str
    alias: str | None = None
    note: str | None = None
    memory_content: str = ""
    # 上次自动归纳时间(ISO 字符串,可空)
    last_summary_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    model_config = {"from_attributes": True}


class SaveProjectRequest(BaseModel):
    """保存分项目记忆请求(PUT /memory/projects/{id})

    仅允许编辑 alias/note/memory_content(不修改 repo_url 与 last_summary_at)。
    """

    alias: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None)
    memory_content: str = Field(default="", max_length=20000)


class ProjectListResponse(BaseModel):
    """分项目记忆列表响应(GET /memory/projects、DELETE 后响应)"""

    projects: list[ProjectOut] = Field(default_factory=list)
