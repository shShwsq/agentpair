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


class TaskListItem(BaseModel):
    """任务列表项(精简版,不含对话/结果,用于侧栏列表)"""

    id: uuid.UUID
    scenario: str
    user_input: str
    status: str
    current_stage: str | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class TaskCreateResponse(BaseModel):
    """提交任务后的响应"""

    id: uuid.UUID
    status: str

    model_config = {"from_attributes": True}


class ScenarioInfo(BaseModel):
    """场景信息(给前端展示用)

    除 id/name 外,携带四项场景声明,驱动前端场景无关渲染:
    - form_fields: 提交表单字段定义
    - result_grouping: 结果分组维度(None 表示平铺)
    - result_meta_fields: 结果 meta 字段展示
    - coverage: 覆盖度看板声明(None 表示不显示)
    """

    id: str
    name: str
    form_fields: list[dict[str, Any]] = []
    result_grouping: dict[str, Any] | None = None
    result_meta_fields: list[dict[str, Any]] = []
    coverage: dict[str, Any] | None = None


# ============================================================
# 阶段 8:用户澄清(user_agent 向用户提问)
# ============================================================


class ClarificationQuestionOption(BaseModel):
    """选择题选项"""

    value: str
    label: str


class ClarificationQuestion(BaseModel):
    """user_agent 向用户提出的问题

    两种类型:
    - choice: 选择题(用户从 options 中选,可单选或多选)
    - text: 填空题(用户自由文本回答)
    """

    id: str
    type: str  # "choice" | "text"
    question: str
    placeholder: str | None = None
    required: bool = False
    options: list[ClarificationQuestionOption] | None = None
    multi: bool = False


class PendingQuestion(BaseModel):
    """任务当前待回答的问题(GET /tasks/{id}/pending_question 返回)

    前端在收到 question 事件后弹出 QuestionDialog;刷新页面后通过
    GET /tasks/{id}/pending_question 恢复弹窗。无待回答问题时返回 None。
    """

    ask_round: int
    questions: list[ClarificationQuestion]
    reasoning: str = ""
    conversation_id: str | None = None


class AnswerItem(BaseModel):
    """单个问题的答案"""

    question_id: str
    value: str | list[str]


class AnswerRequest(BaseModel):
    """用户提交答案(POST /tasks/{id}/answer)"""

    answers: list[AnswerItem]


class AnswerResponse(BaseModel):
    """提交答案的响应"""

    accepted: bool
    message: str = ""
