"""集成测试:_ACPCollector 检查点触发逻辑 + orchestrator 清理逻辑。

覆盖:
- _ACPCollector._maybe_trigger_checkpoint:K 值触发条件、前 2 迭代跳过、
  allow_interrupt=false 禁用、max_interrupts 上限拒绝
- orchestrator finally 块的清理函数:clear_interrupts + clear_interrupt_count
  在任务结束时正确清理(push + increment 后调用清理,验证队列/计数归零)

测试不连真实 DB / LLM,用 MagicMock 替换 task/db,直接调用 collector 的
_maybe_trigger_checkpoint 方法,验证 checkpoint_callback 是否被调用。

不测试 run_user_agent_checkpoint 本身(涉及 LLM 流式调用,已在单元测试
覆盖配置解析 + JSON 解析)。
"""
import uuid
from unittest.mock import MagicMock

import pytest

from app.agent_checkpoint import DEFAULT_AGENT_POLICY
from app.agent_interrupt import (
    clear_interrupt_count,
    clear_interrupts,
    drain_interrupts,
    get_interrupt_count,
    has_pending_interrupts,
    increment_interrupt_count,
    push_interrupt,
)
from app.agents.acp_base import _ACPCollector


# ============================================================
# fixtures
# ============================================================

@pytest.fixture
def mock_task():
    """构造 mock task:_ACPCollector 构造只存引用,不调用方法。"""
    task = MagicMock()
    task.id = f"test-task-{uuid.uuid4()}"
    return task


@pytest.fixture
def mock_db():
    """构造 mock db:_ACPCollector 在 _maybe_trigger_checkpoint 中不调用 db
    (db 只在 _flush_iteration 落库时用,本测试不触发)。"""
    return MagicMock()


@pytest.fixture
def checkpoint_callback():
    """mock checkpoint_callback:记录每次被调用时的 iteration 参数。"""
    calls: list[int] = []
    cb = MagicMock(side_effect=lambda iteration, snapshot: calls.append(iteration))
    cb.calls = calls  # type: ignore[attr-defined]
    return cb


def _make_collector(
    mock_task, mock_db, checkpoint_callback, *,
    agent_policy: dict | None = None,
    agent_type: str = "cli",
):
    """构造 _ACPCollector,默认注入 K=3 的策略。"""
    if agent_policy is None:
        agent_policy = {
            **DEFAULT_AGENT_POLICY,
            "checkpoint_interval": 3,
            "allow_interrupt": True,
            "max_interrupts_per_round": 2,
        }
    return _ACPCollector(
        mock_task, mock_db, round_idx=1,
        agent_policy=agent_policy,
        agent_type=agent_type,
        checkpoint_callback=checkpoint_callback,
    )


def _advance_iterations(collector, n: int) -> None:
    """模拟 n 次 _start_new_iteration 调用(即 n 个迭代边界)。

    _start_new_iteration 先调 _maybe_trigger_checkpoint(用当前 iteration),
    然后 iteration += 1。所以调用 n 次后,iteration = n,
    触发点在 iteration ∈ {2, 3, ..., n-1}(避开前 2 个)中满足 %K==0 的值。
    """
    for _ in range(n):
        collector._start_new_iteration()


# ============================================================
# _ACPCollector:K 值触发条件
# ============================================================

def test_collector_triggers_at_K_multiples(mock_task, mock_db, checkpoint_callback):
    """K=3 时,iteration=3,6 应触发检查点(前 2 个跳过)。"""
    collector = _make_collector(mock_task, mock_db, checkpoint_callback)

    # 跑 8 个迭代边界:_start_new_iteration 触发点在 iteration=3,6
    _advance_iterations(collector, 8)

    assert checkpoint_callback.calls == [3, 6]
    assert collector.iteration == 8


def test_collector_skips_first_2_iterations(mock_task, mock_db, checkpoint_callback):
    """K=1 时(每迭代都评估),前 2 个迭代仍跳过,从 iteration=2 开始触发。

    注:_maybe_trigger_checkpoint 用 `if self.iteration < 2: return`,
    所以 iteration=0,1 跳过,iteration=2,3,4,... 满足 K 倍数时触发。
    K=1 时 iteration=2,3,4,5 都触发。
    """
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={**DEFAULT_AGENT_POLICY, "checkpoint_interval": 1, "allow_interrupt": True, "max_interrupts_per_round": 100},
    )
    _advance_iterations(collector, 6)

    # iteration=0,1 跳过(< 2);iteration=2,3,4,5 触发(K=1,都满足 %1==0)
    assert checkpoint_callback.calls == [2, 3, 4, 5]


def test_collector_skips_non_K_multiples(mock_task, mock_db, checkpoint_callback):
    """K=3 时,iteration=4,5 不触发(不是 3 的倍数)。"""
    collector = _make_collector(mock_task, mock_db, checkpoint_callback)
    _advance_iterations(collector, 5)

    # 只在 iteration=3 触发
    assert checkpoint_callback.calls == [3]


def test_collector_K_2_triggers_at_2_4_6(mock_task, mock_db, checkpoint_callback):
    """K=2 时,iteration=2,4,6 触发。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={**DEFAULT_AGENT_POLICY, "checkpoint_interval": 2, "allow_interrupt": True, "max_interrupts_per_round": 100},
    )
    _advance_iterations(collector, 8)
    assert checkpoint_callback.calls == [2, 4, 6]


# ============================================================
# _ACPCollector:allow_interrupt / 无策略 禁用
# ============================================================

def test_collector_disabled_when_no_policy(mock_task, mock_db, checkpoint_callback):
    """无 agent_policy(传 None)时,检查点完全不触发。"""
    # 直接构造,不通过 _make_collector(后者会把 None 填成默认策略)
    collector = _ACPCollector(
        mock_task, mock_db, round_idx=1,
        agent_policy=None,
        agent_type="cli",
        checkpoint_callback=checkpoint_callback,
    )
    _advance_iterations(collector, 10)
    assert checkpoint_callback.calls == []


def test_collector_disabled_when_allow_interrupt_false(mock_task, mock_db, checkpoint_callback):
    """allow_interrupt=False 时,检查点不触发(但仍可观察,本测试验证完全不触发)。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={**DEFAULT_AGENT_POLICY, "checkpoint_interval": 3, "allow_interrupt": False},
    )
    _advance_iterations(collector, 10)
    assert checkpoint_callback.calls == []


def test_collector_disabled_when_no_callback(mock_task, mock_db):
    """无 checkpoint_callback 时,_maybe_trigger_checkpoint 直接返回(不报错)。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback=None,
        agent_policy={**DEFAULT_AGENT_POLICY, "checkpoint_interval": 3, "allow_interrupt": True},
    )
    # 不应抛异常
    _advance_iterations(collector, 6)


# ============================================================
# _ACPCollector:CLI vs 内置 K 值解析
# ============================================================

def test_collector_uses_cli_specific_K(mock_task, mock_db, checkpoint_callback):
    """agent_type=cli 时,优先用 checkpoint_interval_cli。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={
            **DEFAULT_AGENT_POLICY,
            "checkpoint_interval": 3,
            "checkpoint_interval_cli": 2,
            "allow_interrupt": True,
            "max_interrupts_per_round": 100,
        },
        agent_type="cli",
    )
    _advance_iterations(collector, 6)
    # K_cli=2,触发点 iteration=2,4
    assert checkpoint_callback.calls == [2, 4]


def test_collector_uses_builtin_specific_K(mock_task, mock_db, checkpoint_callback):
    """agent_type=builtin 时,优先用 checkpoint_interval_builtin。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={
            **DEFAULT_AGENT_POLICY,
            "checkpoint_interval": 3,
            "checkpoint_interval_builtin": 4,
            "allow_interrupt": True,
            "max_interrupts_per_round": 100,
        },
        agent_type="builtin",
    )
    _advance_iterations(collector, 10)
    # K_builtin=4,触发点 iteration=4,8
    assert checkpoint_callback.calls == [4, 8]


def test_collector_falls_back_to_unified_when_specific_null(mock_task, mock_db, checkpoint_callback):
    """agent_type=cli 但 checkpoint_interval_cli=None → 用统一 K。"""
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={
            **DEFAULT_AGENT_POLICY,
            "checkpoint_interval": 3,
            "checkpoint_interval_cli": None,
            "allow_interrupt": True,
            "max_interrupts_per_round": 100,
        },
        agent_type="cli",
    )
    _advance_iterations(collector, 8)
    # 用统一 K=3,触发点 iteration=3,6
    assert checkpoint_callback.calls == [3, 6]


# ============================================================
# _ACPCollector:max_interrupts 上限(collector 内部 _interrupt_count)
# ============================================================

def test_collector_respects_max_interrupts_via_internal_count(mock_task, mock_db, checkpoint_callback):
    """collector._interrupt_count 达到 max 时,_maybe_trigger_checkpoint 跳过。

    注:实际生产中 _interrupt_count 由 _checkpoint_callback 内的全局计数器
    (increment_interrupt_count)更新,但 collector 自己的 _interrupt_count
    不会被自动更新。本测试手动模拟 collector._interrupt_count 的增长,
    验证 _maybe_trigger_checkpoint 的检查逻辑正确。
    """
    collector = _make_collector(
        mock_task, mock_db, checkpoint_callback,
        agent_policy={**DEFAULT_POLICY_WITH_K1, "max_interrupts_per_round": 2},
    )
    # K=1,max=2:iteration=2,3 触发,iteration=4 应被 max 阻止
    # 但 collector._interrupt_count 不会自动增长,需要手动模拟
    # 在每次 callback 后手动增加 collector._interrupt_count
    def cb_with_increment(iteration, snapshot):
        checkpoint_callback(iteration, snapshot)
        collector._interrupt_count += 1
    collector._checkpoint_callback = cb_with_increment

    _advance_iterations(collector, 6)
    # iteration=2,3 触发(2 次),iteration=4,5 因 _interrupt_count=2 >= max=2 跳过
    assert checkpoint_callback.calls == [2, 3]


# ============================================================
# orchestrator finally 块清理逻辑(直接调用清理函数)
# ============================================================

def test_cleanup_clears_interrupts_and_count_together():
    """模拟 orchestrator finally 块:push + increment 后,clear_* 应同时清理。

    orchestrator.py 的 finally 块顺序调用:
        clear_interrupts(task.id)
        clear_interrupt_count(task.id)
    验证:push + increment 后,两个 clear 都执行,队列与计数器都归零。
    """
    task_id = f"cleanup-test-{uuid.uuid4()}"

    try:
        # 准备:队列有 2 条中断,计数器 round 1 有 2 次、round 2 有 1 次
        push_interrupt(task_id, query="q1", reason="r1", iteration=3)
        push_interrupt(task_id, query="q2", reason="r2", iteration=6)
        increment_interrupt_count(task_id, round_idx=1)
        increment_interrupt_count(task_id, round_idx=1)
        increment_interrupt_count(task_id, round_idx=2)

        assert has_pending_interrupts(task_id) is True
        assert get_interrupt_count(task_id, round_idx=1) == 2
        assert get_interrupt_count(task_id, round_idx=2) == 1

        # 执行清理(模拟 orchestrator finally 块)
        clear_interrupts(task_id)
        clear_interrupt_count(task_id)

        # 验证:队列与计数器都归零
        assert has_pending_interrupts(task_id) is False
        assert drain_interrupts(task_id) == []
        assert get_interrupt_count(task_id, round_idx=1) == 0
        assert get_interrupt_count(task_id, round_idx=2) == 0
    finally:
        # 兜底清理(避免测试间污染)
        clear_interrupts(task_id)
        clear_interrupt_count(task_id)


def test_cleanup_is_idempotent():
    """清理函数幂等:重复调用不报错,空队列/空计数器也能清理。"""
    task_id = f"idempotent-test-{uuid.uuid4()}"
    try:
        # 空队列/空计数器重复清理
        clear_interrupts(task_id)
        clear_interrupts(task_id)
        clear_interrupt_count(task_id)
        clear_interrupt_count(task_id)

        assert has_pending_interrupts(task_id) is False
        assert get_interrupt_count(task_id, round_idx=1) == 0
    finally:
        clear_interrupts(task_id)
        clear_interrupt_count(task_id)


def test_cleanup_one_task_does_not_affect_another():
    """清理 task A 不应影响 task B 的队列/计数器(隔离性)。"""
    task_a = f"task-a-{uuid.uuid4()}"
    task_b = f"task-b-{uuid.uuid4()}"

    try:
        # 两个 task 都有数据
        push_interrupt(task_a, query="qa", reason="ra", iteration=3)
        push_interrupt(task_b, query="qb", reason="rb", iteration=3)
        increment_interrupt_count(task_a, round_idx=1)
        increment_interrupt_count(task_b, round_idx=1)

        # 清理 task_a
        clear_interrupts(task_a)
        clear_interrupt_count(task_a)

        # task_b 不受影响
        assert has_pending_interrupts(task_a) is False
        assert has_pending_interrupts(task_b) is True
        assert get_interrupt_count(task_a, round_idx=1) == 0
        assert get_interrupt_count(task_b, round_idx=1) == 1

        # task_b 队列内容仍可 drain
        items_b = drain_interrupts(task_b)
        assert len(items_b) == 1
        assert items_b[0]["query"] == "qb"
    finally:
        clear_interrupts(task_a)
        clear_interrupts(task_b)
        clear_interrupt_count(task_a)
        clear_interrupt_count(task_b)


# ============================================================
# 辅助常量
# ============================================================

# K=1 的策略(便于测试 max_interrupts):每迭代都评估
DEFAULT_POLICY_WITH_K1 = {
    **DEFAULT_AGENT_POLICY,
    "checkpoint_interval": 1,
    "allow_interrupt": True,
}
