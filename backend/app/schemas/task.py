"""任务相关的 Pydantic 模型(请求与响应)

通用化设计:
- TaskCreateRequest: scenario + user_input + params(可选)
- 兼容旧 API:提供 repo_url 时自动转成 user_input + params
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, HttpUrl

from app.agents.registry import get_registered_types

# 合法执行器:builtin + registry 中已注册的 agent_type
# 新增 agent 类型只需在 registry 注册,此处自动生效
_VALID_EXECUTORS = ("builtin", *get_registered_types())
_EXECUTOR_PATTERN = "^(" + "|".join(_VALID_EXECUTORS) + ")$"


class TaskCreateRequest(BaseModel):
    """提交任务的请求

    方式 1(通用):传 scenario + user_input + params
    方式 2(兼容):传 repo_url,自动推断 scenario=code_security_audit
    """

    # 场景标识,对应已注册的场景(见 app/scenarios/)
    scenario: str = "code_security_audit"
    # 任务标题:可选,用户自定义便于识别;为空时前端用 user_input 截断展示
    title: str | None = Field(default=None, max_length=255)
    # 用户意图文本(必填,若提供 repo_url 则自动生成)
    user_input: str | None = None
    # 可选参数(如 repo_url、branch 等),场景专用
    params: dict[str, Any] | None = None

    # 用户选择的 LLM 配置 id(对应 user_llm_configs.llm_configs[].id)
    # 为空表示用 env 默认配置或匿名任务
    llm_config_id: str | None = None

    # 执行器选择:"builtin"(默认,内置 react_agent)或 registry 中已注册的 agent_type(如 "qoder_cli")
    # 外部 CLI 模式下,模型由该 CLI 的账号配额管理,llm_config_id 被忽略
    executor: str = Field(default="builtin", pattern=_EXECUTOR_PATTERN)

    # 兼容字段:旧 API 直接传 repo_url
    repo_url: HttpUrl | None = None
    branch: str | None = None
    scope: str | None = None


class TaskTitleUpdateRequest(BaseModel):
    """修改任务标题的请求

    title 为空字符串等价于清除自定义标题(回退到用 user_input 展示)。
    """

    title: str = Field(max_length=255)


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
    title: str | None = None
    user_input: str
    params: dict[str, Any] | None = None
    llm_config_id: str | None = None
    executor: str = "builtin"
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
    title: str | None = None
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
    """场景模板信息(给前端展示用)

    场景降级后:仅提供预设提示词 + 推荐 skill,不再驱动表单/分组/覆盖度。
    前端用 preset_prompt 预填输入框,用 recommended_skills 默认勾选 skill。
    """

    id: str
    name: str
    description: str = ""
    preset_prompt: str = ""
    recommended_skills: list[str] = []


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


# ============================================================
# 覆盖度清单动态生成 + 用户编辑(场景降级后)
# ============================================================


class ChecklistDimension(BaseModel):
    """覆盖度清单的一个维度(由 user_agent 动态生成,用户可编辑)"""

    id: str
    name: str
    description: str = ""
    checklist: list[str] = []


class ChecklistReviewRequest(BaseModel):
    """用户编辑覆盖度清单后提交(POST /tasks/{id}/checklist)

    checklist 为 None 表示"直接采用 LLM 生成结果"(不编辑)。
    """

    checklist: list[ChecklistDimension] | None = None


# ============================================================
# 用户补充消息(对话界面下方输入框)
# ============================================================


class SendMessageRequest(BaseModel):
    """用户在对话界面下方输入框发送的补充消息(POST /tasks/{id}/messages)

    用途:用户在任务运行中/暂停中/完成后追加指令或补充要求。
    后端按 task.status 分发:
    - running/paused:消息入队,react_agent 下一迭代注入 LLM 上下文
    - completed:启动新的协作 round(resume_audit_with_message)
    """

    content: str = Field(min_length=1, max_length=8000)


class SendMessageResponse(BaseModel):
    """发送用户补充消息的响应"""

    accepted: bool
    message: str = ""
