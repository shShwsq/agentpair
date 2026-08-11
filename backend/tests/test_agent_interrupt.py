"""agent_interrupt 单元测试:per-task 中断队列 + 打断计数器(纯内存,不连 DB)。

覆盖:
- push/drain/has_pending/clear 行为
- drain 后队列清空(不会重复处理)
- 不存在的 task drain 返回空列表(惰性创建)
- 打断计数器:per-task + per-round 分别计数
- 清理函数:clear_interrupts / clear_interrupt_count 互不影响
"""
import uuid

import pytest

from app.agent_interrupt import (
    clear_interrupt_count,
    clear_interrupts,
    drain_interrupts,
    get_interrupt_count,
    has_pending_interrupts,
    increment_interrupt_count,
    push_interrupt,
)


@pytest.fixture
def task_id():
    """每个测试用唯一 task_id,避免污染全局 _queues / _counters 字典。"""
    return f"test-interrupt-{uuid.uuid4()}"


# ============================================================
# 队列行为:push / drain / has_pending / clear
# ============================================================

def test_drain_empty_returns_empty_list(task_id):
    """未 push 过的 task drain 应返回空列表(惰性创建队列)。"""
    assert drain_interrupts(task_id) == []
    assert has_pending_interrupts(task_id) is False


def test_push_then_drain_returns_items_in_order(task_id):
    """push 多条后 drain 应按 push 顺序返回。"""
    push_interrupt(task_id, query="q1", reason="r1", iteration=3)
    push_interrupt(task_id, query="q2", reason="r2", iteration=6)

    items = drain_interrupts(task_id)
    assert len(items) == 2
    assert items[0]["query"] == "q1"
    assert items[0]["reason"] == "r1"
    assert items[0]["iteration"] == 3
    assert items[1]["query"] == "q2"
    assert items[1]["iteration"] == 6
    # created_at 应是 ISO 字符串
    assert "T" in items[0]["created_at"]


def test_drain_clears_queue(task_id):
    """drain 后队列清空,再次 drain 返回空。"""
    push_interrupt(task_id, query="q", reason="r", iteration=3)
    assert has_pending_interrupts(task_id) is True

    first = drain_interrupts(task_id)
    assert len(first) == 1

    assert has_pending_interrupts(task_id) is False
    second = drain_interrupts(task_id)
    assert second == []


def test_has_pending_reflects_push_state(task_id):
    """has_pending 应反映 push 前后的状态。"""
    assert has_pending_interrupts(task_id) is False
    push_interrupt(task_id, query="q", reason="r", iteration=3)
    assert has_pending_interrupts(task_id) is True


def test_clear_interrupts_removes_queue(task_id):
    """clear_interrupts 应清空队列并移除 task 注册。"""
    push_interrupt(task_id, query="q", reason="r", iteration=3)
    push_interrupt(task_id, query="q2", reason="r2", iteration=6)
    assert has_pending_interrupts(task_id) is True

    clear_interrupts(task_id)
    assert has_pending_interrupts(task_id) is False
    assert drain_interrupts(task_id) == []


def test_push_accepts_uuid_task_id(task_id):
    """push 应接受 UUID 类型 task_id(后端 task.id 是 UUID)。"""
    uid = uuid.uuid4()
    push_interrupt(uid, query="q", reason="r", iteration=3)
    # 用 str 形式也能 drain(后端有时传 str,有时传 UUID)
    items = drain_interrupts(str(uid))
    assert len(items) == 1
    assert items[0]["query"] == "q"


def test_drain_accepts_uuid_task_id(task_id):
    """drain 应接受 UUID 类型 task_id。"""
    uid = uuid.uuid4()
    push_interrupt(str(uid), query="q", reason="r", iteration=3)
    items = drain_interrupts(uid)
    assert len(items) == 1


# ============================================================
# 打断计数器:per-task + per-round
# ============================================================

def test_counter_starts_at_zero(task_id):
    """未 increment 过的 counter 应返回 0。"""
    assert get_interrupt_count(task_id, round_idx=1) == 0
    assert get_interrupt_count(task_id, round_idx=2) == 0


def test_counter_increments_per_round(task_id):
    """counter 应按 round 分别计数,互不影响。"""
    assert increment_interrupt_count(task_id, round_idx=1) == 1
    assert increment_interrupt_count(task_id, round_idx=1) == 2
    assert increment_interrupt_count(task_id, round_idx=2) == 1

    assert get_interrupt_count(task_id, round_idx=1) == 2
    assert get_interrupt_count(task_id, round_idx=2) == 1


def test_clear_interrupt_count_resets_all_rounds(task_id):
    """clear_interrupt_count 应清空所有 round 的计数。"""
    increment_interrupt_count(task_id, round_idx=1)
    increment_interrupt_count(task_id, round_idx=2)
    increment_interrupt_count(task_id, round_idx=2)

    clear_interrupt_count(task_id)
    assert get_interrupt_count(task_id, round_idx=1) == 0
    assert get_interrupt_count(task_id, round_idx=2) == 0


def test_clear_interrupts_does_not_affect_counter(task_id):
    """clear_interrupts 只清队列,不清计数器(两者独立)。"""
    push_interrupt(task_id, query="q", reason="r", iteration=3)
    increment_interrupt_count(task_id, round_idx=1)

    clear_interrupts(task_id)
    # 队列清空,但计数器仍在
    assert has_pending_interrupts(task_id) is False
    assert get_interrupt_count(task_id, round_idx=1) == 1


def test_clear_interrupt_count_does_not_affect_queue(task_id):
    """clear_interrupt_count 只清计数器,不清队列(两者独立)。"""
    push_interrupt(task_id, query="q", reason="r", iteration=3)
    increment_interrupt_count(task_id, round_idx=1)

    clear_interrupt_count(task_id)
    # 计数器清空,但队列仍在
    assert get_interrupt_count(task_id, round_idx=1) == 0
    assert has_pending_interrupts(task_id) is True


# ============================================================
# 多 task 隔离
# ============================================================

def test_queues_isolated_between_tasks():
    """不同 task 的队列应完全隔离,push/drain 互不影响。"""
    tid_a = f"task-a-{uuid.uuid4()}"
    tid_b = f"task-b-{uuid.uuid4()}"

    push_interrupt(tid_a, query="qa", reason="ra", iteration=3)
    push_interrupt(tid_b, query="qb", reason="rb", iteration=3)

    items_a = drain_interrupts(tid_a)
    assert len(items_a) == 1
    assert items_a[0]["query"] == "qa"

    items_b = drain_interrupts(tid_b)
    assert len(items_b) == 1
    assert items_b[0]["query"] == "qb"

    # 再次 drain 都为空
    assert drain_interrupts(tid_a) == []
    assert drain_interrupts(tid_b) == []


def test_counters_isolated_between_tasks():
    """不同 task 的计数器应完全隔离。"""
    tid_a = f"task-ct-a-{uuid.uuid4()}"
    tid_b = f"task-ct-b-{uuid.uuid4()}"

    increment_interrupt_count(tid_a, round_idx=1)
    increment_interrupt_count(tid_a, round_idx=1)
    increment_interrupt_count(tid_b, round_idx=1)

    assert get_interrupt_count(tid_a, round_idx=1) == 2
    assert get_interrupt_count(tid_b, round_idx=1) == 1
