"""用户交互管理:user_agent 请求用户澄清时的阻塞-唤醒机制

场景:user_agent 在第 0 轮初始评估时,如果认为用户意图不清晰,
可以输出 ask_user=true + questions 列表,orchestrator 把 questions
推送给前端,后台线程阻塞等待用户填答;用户提交答案后,API 端点
唤醒后台线程,把答案拼回 user_intent 重新评估。

设计:
- 每个 task 同时只能有一个待回答的问题(单轮提问)
- 用 threading.Event 阻塞后台线程,无限等待(用户随时回来填答)
- pending 的问题和已提交的答案分别存全局 dict
- 任务结束/失败时调用 cleanup 释放内存

线程安全:用 threading.Lock 保护两个 dict。
适用单机部署;多实例部署需换 Redis 等共享存储。
"""
from __future__ import annotations

import logging
import threading
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


class _PendingQuestion:
    """单个 task 的待回答问题状态"""

    def __init__(self) -> None:
        # 后台线程阻塞在此 Event 上,API 提交答案时 set()
        self.event: threading.Event = threading.Event()
        # 用户提交的答案(API 端点写入,后台线程读取)
        # 结构:[{"question_id": str, "value": str | list[str]}, ...]
        self.answers: list[dict[str, Any]] | None = None
        # 问题快照(orchestrator 推送时存,API 校验/前端恢复时读)
        # 结构:{"ask_round": int, "questions": [...], "reasoning": str}
        self.question_payload: dict[str, Any] | None = None
        # 锁:保护 answers / question_payload 的读写
        self.lock = threading.Lock()


# 全局注册表:task_id(str)→ _PendingQuestion
_pending: dict[str, _PendingQuestion] = {}
_pending_lock = threading.Lock()


def _get_or_create(task_id: str) -> _PendingQuestion:
    """获取(或创建)task 的待回答问题状态"""
    with _pending_lock:
        if task_id not in _pending:
            _pending[task_id] = _PendingQuestion()
        return _pending[task_id]


def set_pending_question(
    task_id: str | UUID,
    question_payload: dict[str, Any],
) -> _PendingQuestion:
    """设置 task 的待回答问题(后台线程调用)

    在后台线程阻塞前调用:把 questions 存起来供 API/前端查询,
    同时创建新的 Event 准备 wait。

    参数:
        task_id: 任务 ID
        question_payload: {"ask_round": int, "questions": [...], "reasoning": str}

    返回:_PendingQuestion,后台线程随后调 .event.wait() 阻塞
    """
    task_id_str = str(task_id)
    pq = _get_or_create(task_id_str)
    with pq.lock:
        # 重置状态:新的一轮提问
        pq.event = threading.Event()
        pq.answers = None
        pq.question_payload = question_payload
    return pq


def wait_for_answers(task_id: str | UUID) -> list[dict[str, Any]]:
    """阻塞等待用户提交答案(后台线程调用)

    无限等待,直到 submit_answers 被调用。任务被取消时由调用方处理。
    返回答案列表;若 question_payload 被外部清除(任务取消),返回空列表。

    注意:question_payload 的清理已在 submit_answers 中完成(消除竞态),
    这里只清 answers,避免后台线程长期持有已读数据。
    """
    task_id_str = str(task_id)
    pq = _get_or_create(task_id_str)
    # 无限等待
    pq.event.wait()
    with pq.lock:
        answers = pq.answers or []
        # question_payload 已在 submit_answers 中清理,这里只清 answers
        pq.answers = None
    return answers


def submit_answers(
    task_id: str | UUID,
    answers: list[dict[str, Any]],
) -> bool:
    """提交用户答案,唤醒后台线程(API 端点调用)

    返回 True 表示成功唤醒;False 表示当前 task 没有待回答问题
    (可能用户重复提交,或任务已结束)。
    """
    task_id_str = str(task_id)
    with _pending_lock:
        pq = _pending.get(task_id_str)
    if pq is None:
        return False
    with pq.lock:
        if pq.question_payload is None:
            # 没有待回答问题
            return False
        if pq.event.is_set():
            # 已经被唤醒过(重复提交)
            return False
        pq.answers = answers
        # 立即清理 question_payload:避免 submit 返回后、后台线程唤醒清理前
        # 的时间窗口内,前端 get_pending_question 拿到旧 payload 重复弹窗。
        # (原先清理放在 wait_for_answers 里,依赖后台线程被调度,存在竞态)
        pq.question_payload = None
    pq.event.set()
    logger.info(f"[task={task_id_str}] 收到用户答案,{len(answers)} 项")
    return True


def get_pending_question(task_id: str | UUID) -> dict[str, Any] | None:
    """查询 task 当前的待回答问题(前端恢复弹窗用)

    返回 question_payload(含 ask_round/questions/reasoning);无待回答问题返回 None。
    """
    task_id_str = str(task_id)
    with _pending_lock:
        pq = _pending.get(task_id_str)
    if pq is None:
        return None
    with pq.lock:
        if pq.question_payload is None:
            return None
        # 返回副本,避免外部修改
        return dict(pq.question_payload)


def clear_pending_question(task_id: str | UUID) -> None:
    """清除 task 的待回答问题(任务结束/失败时调用)

    若后台线程还在 wait,会因 Event 永不 set 而继续阻塞——所以这里也 set() 一下,
    让 wait_for_answers 返回空列表,后台线程能继续走清理流程。
    """
    task_id_str = str(task_id)
    with _pending_lock:
        pq = _pending.pop(task_id_str, None)
    if pq is not None:
        with pq.lock:
            pq.question_payload = None
            if pq.answers is None:
                pq.answers = []
        # 唤醒可能正在阻塞的线程
        pq.event.set()


def has_pending_question(task_id: str | UUID) -> bool:
    """task 是否有待回答问题(快速判断,不加锁)"""
    task_id_str = str(task_id)
    with _pending_lock:
        pq = _pending.get(task_id_str)
    if pq is None:
        return False
    with pq.lock:
        return pq.question_payload is not None
