"""agent_checkpoint 单元测试:配置解析 + JSON 解析(不调 LLM,不连真实 DB)。

覆盖:
- resolve_agent_policy:用户级默认 + 任务级覆盖合并优先级
- get_effective_interval:内置/CLI 各自 K 值解析
- _parse_checkpoint_json:LLM 输出容错解析(带/不带 ```json 包裹)
- DEFAULT_AGENT_POLICY 常量字段完整性
"""
import uuid
from unittest.mock import MagicMock

import pytest

from app.agent_checkpoint import (
    DEFAULT_AGENT_POLICY,
    _parse_checkpoint_json,
    get_effective_interval,
    resolve_agent_policy,
)


# ============================================================
# 测试 fixture
# ============================================================

@pytest.fixture
def fake_task():
    """构造 fake task:user_id=None,params=None(匿名任务,只用默认值)。"""
    task = MagicMock()
    task.user_id = None
    task.params = None
    task.id = "test-task-id"
    task.user_input = "测试用户输入"
    return task


def _make_task_with_overrides(user_id=None, params=None):
    """构造 fake task,指定 user_id 和 params。"""
    task = MagicMock()
    task.user_id = user_id
    task.params = params
    task.id = "test-task-id"
    task.user_input = "测试用户输入"
    return task


def _mock_db_with_user_pref(user_pref=None):
    """构造 mock db:db.query(UserPreference).filter(...).first() 返回 user_pref。

    user_pref=None 表示用户未配置偏好(查无记录)。
    """
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = user_pref
    return db


# ============================================================
# DEFAULT_AGENT_POLICY 常量
# ============================================================

def test_default_policy_has_all_required_fields():
    """DEFAULT_AGENT_POLICY 应包含所有必需字段,默认值符合设计。"""
    expected_keys = {
        "user_agent_enabled",
        "max_rounds",
        "checkpoint_interval",
        "checkpoint_interval_builtin",
        "checkpoint_interval_cli",
        "allow_interrupt",
        "max_interrupts_per_round",
        "allow_verify",
        "verifier_auth_mode_default",
        "executor_command_confirm_default",
    }
    assert set(DEFAULT_AGENT_POLICY.keys()) == expected_keys
    # 关键默认值(与设计文档 / 前端 DEFAULT_POLICY 对齐)
    assert DEFAULT_AGENT_POLICY["user_agent_enabled"] is True
    assert DEFAULT_AGENT_POLICY["max_rounds"] == 4
    assert DEFAULT_AGENT_POLICY["checkpoint_interval"] == 3
    assert DEFAULT_AGENT_POLICY["checkpoint_interval_builtin"] is None
    assert DEFAULT_AGENT_POLICY["checkpoint_interval_cli"] is None
    assert DEFAULT_AGENT_POLICY["allow_interrupt"] is True
    assert DEFAULT_AGENT_POLICY["max_interrupts_per_round"] == 2
    assert DEFAULT_AGENT_POLICY["allow_verify"] is False
    assert DEFAULT_AGENT_POLICY["verifier_auth_mode_default"] == "per_action"
    assert DEFAULT_AGENT_POLICY["executor_command_confirm_default"] == "always_approve"


# ============================================================
# resolve_agent_policy:优先级 DEFAULT > user > task
# ============================================================

def test_resolve_returns_defaults_for_anonymous_task(fake_task):
    """匿名任务(user_id=None)+ 无 params → 纯默认值。"""
    db = _mock_db_with_user_pref(user_pref=None)
    policy = resolve_agent_policy(fake_task, db)

    # 应等于默认值
    assert policy["checkpoint_interval"] == 3
    assert policy["allow_interrupt"] is True
    assert policy["max_interrupts_per_round"] == 2


def test_resolve_uses_user_level_defaults_when_no_overrides():
    """有用户级默认 + 无任务级覆盖 → 用用户级默认。"""
    task = _make_task_with_overrides(user_id=uuid.uuid4(), params=None)
    user_pref = MagicMock()
    user_pref.agent_policy = {
        "checkpoint_interval": 5,
        "allow_interrupt": False,
    }
    db = _mock_db_with_user_pref(user_pref=user_pref)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 5
    assert policy["allow_interrupt"] is False
    # 未覆盖的字段仍用 DEFAULT
    assert policy["max_interrupts_per_round"] == 2


def test_resolve_task_overrides_win_over_user_defaults():
    """任务级覆盖应优先于用户级默认。"""
    task = _make_task_with_overrides(
        user_id=uuid.uuid4(),
        params={"_agent_policy": {"allow_interrupt": False, "checkpoint_interval": 10}},
    )
    user_pref = MagicMock()
    user_pref.agent_policy = {
        "checkpoint_interval": 5,
        "allow_interrupt": True,  # 应被任务级 False 覆盖
        "max_interrupts_per_round": 4,  # 任务级未覆盖,应保留 4
    }
    db = _mock_db_with_user_pref(user_pref=user_pref)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 10  # 任务级覆盖
    assert policy["allow_interrupt"] is False   # 任务级覆盖
    assert policy["max_interrupts_per_round"] == 4  # 用户级保留


def test_resolve_task_overrides_win_over_defaults_for_anonymous():
    """匿名任务 + 任务级覆盖 → 任务级覆盖 DEFAULT。"""
    task = _make_task_with_overrides(
        user_id=None,
        params={"_agent_policy": {"checkpoint_interval": 7}},
    )
    db = _mock_db_with_user_pref(user_pref=None)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 7
    assert policy["allow_interrupt"] is True  # 未覆盖,用默认


def test_resolve_handles_empty_user_pref_agent_policy():
    """用户有 UserPreference 行但 agent_policy=None → 用 DEFAULT。"""
    task = _make_task_with_overrides(user_id=uuid.uuid4(), params=None)
    user_pref = MagicMock()
    user_pref.agent_policy = None
    db = _mock_db_with_user_pref(user_pref=user_pref)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 3
    assert policy["allow_interrupt"] is True


def test_resolve_handles_no_user_pref_row():
    """用户无 UserPreference 行(first() 返回 None)→ 用 DEFAULT。"""
    task = _make_task_with_overrides(user_id=uuid.uuid4(), params=None)
    db = _mock_db_with_user_pref(user_pref=None)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 3


def test_resolve_handles_empty_params_dict():
    """task.params = {} (空字典,非 None) → 无任务级覆盖,用 DEFAULT。"""
    task = _make_task_with_overrides(user_id=None, params={})
    db = _mock_db_with_user_pref(user_pref=None)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 3


def test_resolve_handles_empty_agent_policy_in_params():
    """task.params = {"_agent_policy": {}} (空覆盖) → 用 DEFAULT。"""
    task = _make_task_with_overrides(user_id=None, params={"_agent_policy": {}})
    db = _mock_db_with_user_pref(user_pref=None)

    policy = resolve_agent_policy(task, db)
    assert policy["checkpoint_interval"] == 3


# ============================================================
# get_effective_interval:K 值解析
# ============================================================

def test_effective_interval_uses_unified_for_builtin_when_no_specific():
    """无内置专用 K → 用统一 K 值。"""
    policy = {"checkpoint_interval": 5, "checkpoint_interval_builtin": None}
    assert get_effective_interval(policy, "builtin") == 5


def test_effective_interval_uses_unified_for_cli_when_no_specific():
    """无 CLI 专用 K → 用统一 K 值。"""
    policy = {"checkpoint_interval": 5, "checkpoint_interval_cli": None}
    assert get_effective_interval(policy, "cli") == 5


def test_effective_interval_uses_builtin_specific_when_set():
    """有内置专用 K → 用专用值,忽略统一值。"""
    policy = {"checkpoint_interval": 5, "checkpoint_interval_builtin": 2}
    assert get_effective_interval(policy, "builtin") == 2


def test_effective_interval_uses_cli_specific_when_set():
    """有 CLI 专用 K → 用专用值,忽略统一值。"""
    policy = {"checkpoint_interval": 5, "checkpoint_interval_cli": 10}
    assert get_effective_interval(policy, "cli") == 10


def test_effective_interval_builtin_specific_does_not_affect_cli():
    """内置专用 K 不影响 CLI(CLI 应 fallback 到统一值)。"""
    policy = {"checkpoint_interval": 5, "checkpoint_interval_builtin": 2, "checkpoint_interval_cli": None}
    assert get_effective_interval(policy, "cli") == 5


def test_effective_interval_falls_back_to_default_3_when_missing():
    """policy 完全缺 checkpoint_interval → 用默认值 3。"""
    policy = {}
    assert get_effective_interval(policy, "builtin") == 3
    assert get_effective_interval(policy, "cli") == 3


def test_effective_interval_handles_zero_specific_as_falsy():
    """specific=0 时(falsy)应回退到统一值(与实现 `or base_k` 行为一致)。

    注意:这是设计取舍——0 不能作为有效 K(K=0 会让评估永远不触发),
    所以与 None 同等处理。如果业务需要支持 K=0,需改实现。
    """
    policy = {"checkpoint_interval": 5, "checkpoint_interval_builtin": 0}
    # 0 falsy → 回退到 base_k=5
    assert get_effective_interval(policy, "builtin") == 5


# ============================================================
# _parse_checkpoint_json:LLM 输出容错
# ============================================================

def test_parse_plain_json_interrupt_true():
    """纯 JSON(无 markdown 包裹)解析正常。"""
    content = '{"interrupt": true, "reason": "方向跑偏", "query": "请转向 X"}'
    result = _parse_checkpoint_json(content)
    assert result["interrupt"] is True
    assert result["reason"] == "方向跑偏"
    assert result["query"] == "请转向 X"


def test_parse_plain_json_interrupt_false():
    """interrupt=false 时 query 应为 None(即使 LLM 输出空字符串)。"""
    content = '{"interrupt": false, "reason": "继续", "query": ""}'
    result = _parse_checkpoint_json(content)
    assert result["interrupt"] is False
    assert result["reason"] == "继续"
    assert result["query"] is None


def test_parse_json_with_markdown_fence():
    """LLM 输出带 ```json ... ``` 包裹时应正确解析。"""
    content = '```json\n{"interrupt": true, "reason": "r", "query": "q"}\n```'
    result = _parse_checkpoint_json(content)
    assert result["interrupt"] is True
    assert result["query"] == "q"


def test_parse_json_with_plain_fence():
    """LLM 输出带 ``` ... ``` 包裹(无 json 标记)时应正确解析。"""
    content = '```\n{"interrupt": false, "reason": "r", "query": null}\n```'
    result = _parse_checkpoint_json(content)
    assert result["interrupt"] is False
    assert result["query"] is None


def test_parse_json_with_extra_whitespace():
    """LLM 输出前后有空白时应正确解析。"""
    content = '\n\n  {"interrupt": true, "reason": "r", "query": "q"}  \n\n'
    result = _parse_checkpoint_json(content)
    assert result["interrupt"] is True
    assert result["query"] == "q"


def test_parse_json_missing_interrupt_defaults_to_false():
    """LLM 输出缺 interrupt 字段时默认为 False(安全降级)。"""
    content = '{"reason": "r", "query": null}'
    result = _parse_checkpoint_json(content)
    assert result["interrupt"] is False
    assert result["reason"] == "r"


def test_parse_json_missing_reason_defaults_to_empty():
    """LLM 输出缺 reason 字段时默认为空字符串。"""
    content = '{"interrupt": false, "query": null}'
    result = _parse_checkpoint_json(content)
    assert result["reason"] == ""


def test_parse_json_null_query_becomes_none():
    """LLM 输出 query=null 时解析为 Python None。"""
    content = '{"interrupt": false, "reason": "r", "query": null}'
    result = _parse_checkpoint_json(content)
    assert result["query"] is None


def test_parse_json_truthy_non_bool_interrupt_coerced_to_bool():
    """interrupt 字段非 bool(如 1/0/"true")时强制转换为 bool。"""
    # truthy 值转为 True
    assert _parse_checkpoint_json('{"interrupt": 1, "reason": "r"}')["interrupt"] is True
    assert _parse_checkpoint_json('{"interrupt": "true", "reason": "r"}')["interrupt"] is True
    # falsy 值转为 False
    assert _parse_checkpoint_json('{"interrupt": 0, "reason": "r"}')["interrupt"] is False
    assert _parse_checkpoint_json('{"interrupt": "", "reason": "r"}')["interrupt"] is False


def test_parse_invalid_json_raises():
    """无效 JSON 应抛出异常(由调用方 run_user_agent_checkpoint 捕获并降级)。"""
    with pytest.raises(Exception):
        _parse_checkpoint_json("not a json at all")
