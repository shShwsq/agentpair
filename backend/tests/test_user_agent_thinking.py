"""user_agent 真实思考链落库测试(mock LLM 流式,不连真实服务)。

核心约束:user_agent 的 LLM reasoning_content 需以 type=thinking 落库,
供前端刷新后还原思考卡片;结构化评估记录(evaluation)仍由 orchestrator
另行落库,两边职责不混。
"""
import json
from unittest.mock import MagicMock

from app.agents.user_agent import run_user_agent
from app.models.task import Conversation


class _MockChunk:
    """模拟 LLMClient.chat_stream 产出的 chunk"""

    def __init__(
        self, reasoning_delta="", content_delta="",
        tool_call_deltas=None, finish_reason=None,
    ):
        self.reasoning_delta = reasoning_delta
        self.content_delta = content_delta
        self.tool_call_deltas = tool_call_deltas or []
        self.finish_reason = finish_reason


def _mk_client(chunks):
    client = MagicMock()
    client.chat_stream = MagicMock(return_value=iter(chunks))
    return client


def _mk_task():
    task = MagicMock()
    task.id = "task-1"
    task.verifier_enabled = False
    task.test_env_url = ""
    return task


def _eval_json(**overrides):
    """合法的 user_agent 结构化输出 JSON"""
    result = {
        "covered": [],
        "missing": ["d1"],
        "reasoning": "需要先查后端",
        "followup_query": "请检查 backend 目录",
        "done": False,
        "ask_user": False,
        "questions": [],
    }
    result.update(overrides)
    return json.dumps(result, ensure_ascii=False)


def test_thinking_persisted_as_conversation():
    """有 reasoning 增量 → 落库一条 role=user_agent/type=thinking 记录。"""
    db = MagicMock()
    chunks = [
        _MockChunk(reasoning_delta="思考第一段。"),
        _MockChunk(reasoning_delta="思考第二段。"),
        _MockChunk(content_delta=_eval_json(), finish_reason="stop"),
    ]
    result = run_user_agent(
        "审查这个仓库", [],
        task_id="task-1", db=db, round_idx=1,
        client=_mk_client(chunks), ask_round=2, task=_mk_task(),
    )
    assert result["followup_query"] == "请检查 backend 目录"

    added = [c.args[0] for c in db.add.call_args_list]
    thinkings = [
        c for c in added
        if isinstance(c, Conversation) and c.type == "thinking"
    ]
    assert len(thinkings) == 1
    conv = thinkings[0]
    assert conv.role == "user_agent"
    assert conv.reasoning == "思考第一段。思考第二段。"
    assert conv.content == ""
    assert conv.round_idx == 1


def test_empty_reasoning_not_persisted():
    """模型无 reasoning 输出(非思考型) → 不落库 thinking 记录。"""
    db = MagicMock()
    chunks = [
        _MockChunk(content_delta=_eval_json(), finish_reason="stop"),
    ]
    run_user_agent(
        "审查这个仓库", [],
        task_id="task-1", db=db, round_idx=1,
        client=_mk_client(chunks), ask_round=2, task=_mk_task(),
    )
    added = [c.args[0] for c in db.add.call_args_list]
    assert not [
        c for c in added
        if isinstance(c, Conversation) and c.type == "thinking"
    ]


def test_no_db_no_task_still_returns_result():
    """db/task 缺失(兜底场景) → 不落库但评估结果正常返回。"""
    chunks = [
        _MockChunk(reasoning_delta="思考内容"),
        _MockChunk(content_delta=_eval_json(), finish_reason="stop"),
    ]
    result = run_user_agent(
        "审查这个仓库", [],
        task_id="task-1", db=None, round_idx=0,
        client=_mk_client(chunks), ask_round=0, task=None,
    )
    assert result["followup_query"] == "请检查 backend 目录"
