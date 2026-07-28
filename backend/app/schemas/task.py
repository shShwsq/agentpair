"""任务相关的 Pydantic 模型(请求与响应)"""
import uuid
from datetime import datetime

from pydantic import BaseModel, HttpUrl


class TaskCreateRequest(BaseModel):
    """提交任务的请求"""

    repo_url: HttpUrl
    branch: str | None = None
    scope: str | None = None


class FindingResponse(BaseModel):
    """漏洞发现响应"""

    id: uuid.UUID
    category: str
    severity: str
    file_path: str | None
    line_range: str | None
    description: str
    remediation: str | None
    verified: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationResponse(BaseModel):
    """对话记录响应"""

    id: uuid.UUID
    role: str
    type: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskResponse(BaseModel):
    """任务详情响应"""

    id: uuid.UUID
    scenario: str
    repo_url: str
    branch: str | None
    scope: str | None
    status: str
    current_stage: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None
    findings: list[FindingResponse] = []
    conversations: list[ConversationResponse] = []

    model_config = {"from_attributes": True}


class TaskCreateResponse(BaseModel):
    """提交任务后的响应(只返回 ID)"""

    id: uuid.UUID
    status: str

    model_config = {"from_attributes": True}
