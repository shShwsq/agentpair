"""练习模块的 Pydantic 模型(请求与响应)"""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# ============================================================
# 题目生成 / 确认
# ============================================================


class GenerateRequest(BaseModel):
    """从审计任务的真实发现生成练习题(POST /practice/generate)"""

    task_id: uuid.UUID
    # 参与生成的 finding 数上限(防 LLM 成本失控)
    max_findings: int = Field(default=10, ge=1, le=20)


class DraftQuestionResponse(BaseModel):
    """生成的候选题(draft 状态,待用户预览确认)

    预览阶段即下发 answer_idx 与 explanation,供用户校对题目质量。
    """

    id: uuid.UUID
    qtype: str
    stem: str
    code_snippet: str | None = None
    options: list[str]
    answer_idx: int
    explanation: str
    difficulty: float
    knowledge_key: str | None = None
    knowledge_name: str | None = None

    model_config = {"from_attributes": True}


class GenerateResponse(BaseModel):
    """题目生成结果"""

    questions: list[DraftQuestionResponse] = []
    # 因解析失败/重复被丢弃的 finding 数(前端提示用)
    skipped_findings: int = 0


class ConfirmQuestionsRequest(BaseModel):
    """确认 draft 题目入库(POST /practice/questions/confirm)

    只传用户勾选保留的 id;同一来源任务的其余 draft 一并删除。
    """

    task_id: uuid.UUID
    question_ids: list[uuid.UUID] = []


class ConfirmQuestionsResponse(BaseModel):
    confirmed: int
    discarded: int = 0


# ============================================================
# 练习会话(按需即时组卷)
# ============================================================


class StartSessionRequest(BaseModel):
    """开始练习(POST /practice/sessions)"""

    count: int = Field(default=8, ge=1, le=30)
    # 限定知识点 key(如只看 "CWE-89"),为空表示全部
    topic_filter: str | None = None


class SessionQuestionResponse(BaseModel):
    """组卷下发的题面(不含 answer_idx / explanation,防作弊)"""

    id: uuid.UUID
    qtype: str
    stem: str
    code_snippet: str | None = None
    options: list[str]
    difficulty: float
    knowledge_name: str | None = None

    model_config = {"from_attributes": True}


class StartSessionResponse(BaseModel):
    session_id: uuid.UUID
    questions: list[SessionQuestionResponse] = []
    # 选题池不足时的提示(如题库为空)
    message: str = ""


class SubmitAnswerRequest(BaseModel):
    """提交单题答案(POST /practice/sessions/{id}/answers)"""

    question_id: uuid.UUID
    chosen_idx: int = Field(ge=0)


class KnowledgeStateResponse(BaseModel):
    """知识点记忆状态(答题反馈与统计展示共用)"""

    knowledge_key: str
    knowledge_name: str
    ease_factor: float
    interval_days: float
    repetitions: int
    due_at: datetime | None = None
    attempts: int
    correct_count: int
    accuracy: float | None = None

    model_config = {"from_attributes": True}


class SubmitAnswerResponse(BaseModel):
    is_correct: bool
    correct_idx: int
    explanation: str
    # 该题知识点的最新记忆状态
    state: KnowledgeStateResponse | None = None
    # 本会话进度(answered / total)
    answered_count: int
    total_count: int


# ============================================================
# 统计 / 题库管理
# ============================================================


class WeakPointItem(BaseModel):
    """薄弱点条目(按错误率排序)"""

    knowledge_key: str
    knowledge_name: str
    attempts: int
    correct_count: int
    accuracy: float
    ease_factor: float
    due_at: datetime | None = None


class StatsResponse(BaseModel):
    """练习首页统计(GET /practice/stats)"""

    # 用户能力估计值(难度匹配基准)
    ability: float
    # 到期待复习的知识点数
    due_count: int
    total_attempts: int
    total_correct: int
    accuracy: float | None = None
    weak_points: list[WeakPointItem] = []
    # 题库规模
    active_question_count: int = 0
    draft_question_count: int = 0


class QuestionListItem(BaseModel):
    """题库列表项(GET /practice/questions)"""

    id: uuid.UUID
    qtype: str
    stem: str
    difficulty: float
    status: str
    knowledge_name: str | None = None
    attempts: int = 0
    accuracy: float | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
