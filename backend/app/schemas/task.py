"""任务相关的 Pydantic 模型(请求与响应)

通用化设计:
- TaskCreateRequest: scenario + user_input + params(可选)
- 兼容旧 API:提供 repo_url 时自动转成 user_input + params
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, HttpUrl


class TaskCreateRequest(BaseModel):
    """提交任务的请求

    方式 1(通用):传 scenario + user_input + params
    方式 2(兼容):传 repo_url,自动推断 scenario=code_security_audit
    """

    # 场景标识,对应已注册的场景(见 app/scenarios/)
    scenario: str = "code_security_audit"
    # 用户意图文本(必填,若提供 repo_url 则自动生成)
    user_input: str | None = None
    # 可选参数(如 repo_url、branch 等),场景专用
    params: dict[str, Any] | None = None

    # 用户选择的 LLM 配置 id(对应 user_llm_configs.llm_configs[].id)
    # 为空表示用 env 默认配置或匿名任务
    llm_config_id: str | None = None

    # 兼容字段:旧 API 直接传 repo_url
    repo_url: HttpUrl | None = None
    branch: str | None = None
    scope: str | None = None


class ResultResponse(BaseModel):
    """任务结果项响应(通用)"""

    id: uuid.UUID
    round_idx: int
    title: str
    content: str
    metadata_: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class ConversationResponse(BaseModel):
    """对话记录响应"""

    id: uuid.UUID
    round_idx: int
    role: str
    type: str
    content: str
    # 思考链(仅 type=thinking 有,模型 reasoning_content 输出)
    reasoning: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskResponse(BaseModel):
    """任务详情响应"""

    id: uuid.UUID
    scenario: str
    user_input: str
    params: dict[str, Any] | None = None
    llm_config_id: str | None = None
    status: str
    current_stage: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None
    results: list[ResultResponse] = []
    conversations: list[ConversationResponse] = []

    model_config = {"from_attributes": True}


class TaskCreateResponse(BaseModel):
    """提交任务后的响应"""

    id: uuid.UUID
    status: str

    model_config = {"from_attributes": True}


class ScenarioInfo(BaseModel):
    """场景信息(给前端展示用)"""

    id: str
    name: str
