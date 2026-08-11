"""user_agent 检查点评估:迭代边界的轻量方向纠正

场景:react_agent(含内置 react_agent 和外部 CLI agent)执行过程中,
每 K 个迭代边界,user_agent 做一次轻量评估,判断方向是否明显跑偏。
若跑偏,生成追问指令入中断队列,react_agent 下一迭代注入(软中断)。

与 user_agent.py 的完整评估区别:
- 完整评估(round 边界):判断 covered/missing,决定是否追问或结束,输出结构化结果
- 检查点评估(迭代边界):只判断"方向是否明显跑偏",不做 covered/missing,
  只在明显跑偏时打断,避免频繁打断影响 react_agent 工作

配置解析:resolve_agent_policy 合并用户级默认 + 任务级覆盖,
get_effective_interval 按执行器类型解析实际生效的 K 值。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.event_bus import publish
from app.llm.client import LLMClient
from app.models.task import Conversation, Task

logger = logging.getLogger(__name__)


# ============================================================
# 默认策略 + 配置解析
# ============================================================

DEFAULT_AGENT_POLICY: dict[str, Any] = {
    "checkpoint_interval": 3,  # 统一 K 值,每 K 个迭代评估一次
    "checkpoint_interval_builtin": None,  # 高级:内置专用(null=用统一值)
    "checkpoint_interval_cli": None,  # 高级:CLI 专用(null=用统一值)
    "allow_interrupt": True,  # user_agent 是否能打断 react_agent
    "max_interrupts_per_round": 2,  # 每轮最多打断次数(防死锁)
    "allow_verify": False,  # user_agent 是否能调用 verifier_agent(需任务配了 test_env_url)
    "verifier_auth_mode_default": "per_action",  # 验证授权默认模式(任务级可覆盖)
}


def resolve_agent_policy(task: Task, db: Session) -> dict[str, Any]:
    """合并用户级默认 + 任务级覆盖,返回最终生效的策略

    优先级:任务级覆盖(task.params["_agent_policy"]) > 用户级默认
    (UserPreference.agent_policy) > DEFAULT_AGENT_POLICY

    任务的 user_id 可能为 None(匿名任务),此时只用默认值 + 任务级覆盖。
    """
    defaults = dict(DEFAULT_AGENT_POLICY)

    # 加载用户级默认(若用户已登录且配置了 agent_policy)
    if task.user_id is not None:
        try:
            from app.models.user_preference import UserPreference
            user_pref = (
                db.query(UserPreference)
                .filter(UserPreference.user_id == task.user_id)
                .first()
            )
            if user_pref and user_pref.agent_policy:
                defaults.update(user_pref.agent_policy)
        except Exception as e:
            logger.warning(
                f"[task={task.id}] 加载用户级 agent_policy 失败(用默认): {e}"
            )

    # 合并任务级覆盖
    overrides = (task.params or {}).get("_agent_policy") or {}
    merged = {**defaults, **overrides}
    return merged


def get_effective_interval(policy: dict[str, Any], executor: str) -> int:
    """根据执行器类型解析实际生效的 K 值

    - executor == "builtin":优先 checkpoint_interval_builtin,为 None 时用 checkpoint_interval
    - 其他(CLI agent):优先 checkpoint_interval_cli,为 None 时用 checkpoint_interval
    """
    base_k = int(policy.get("checkpoint_interval", 3))
    if executor == "builtin":
        specific = policy.get("checkpoint_interval_builtin")
        return int(specific) if specific else base_k
    else:
        specific = policy.get("checkpoint_interval_cli")
        return int(specific) if specific else base_k


# ============================================================
# 检查点评估 System Prompt
# ============================================================

CHECKPOINT_SYSTEM_PROMPT = """你是 user_agent(用户代理智能体),正在实时观察 react_agent(执行智能体)的执行过程。

## 你的职责
判断 react_agent 当前方向是否明显跑偏,是否需要打断纠正。

## 重要约束
- 这是**轻量评估**,不需要做 covered/missing 判断(那个在 round 边界做)
- 只在方向**明显跑偏**时打断,避免频繁打断影响 react_agent 工作
- 打断要有充分理由,如:
  - 偏离用户意图(在分析无关的代码/功能)
  - 重复无效操作(反复搜索相同关键词)
  - 遗漏关键维度(明显应该检查但未涉及的维度)
  - 钻牛角尖(在某个细节上耗费过多迭代)
- 不确定是否该打断时,选择不打断(让 react_agent 继续)

## 输出格式(严格 JSON)
```json
{
  "interrupt": false,
  "reason": "当前方向正确,继续",
  "query": null
}
```

或打断时:
```json
{
  "interrupt": true,
  "reason": "react_agent 在深挖 SQL 注入,但用户意图是检查认证授权,且已发现 3 个同类问题,应转向检查认证绕过",
  "query": "已经发现 3 个 SQL 注入问题,现在转向检查认证与授权相关问题"
}
```

字段说明:
- interrupt: 是否打断(true 时 query 必填)
- reason: 判断理由(展示给用户看)
- query: 打断时的追问指令(注入 react_agent 作为 user 消息),null 表示不打断
"""


# ============================================================
# 检查点评估主函数
# ============================================================

def run_user_agent_checkpoint(
    task: Task,
    db: Session,
    round_idx: int,
    iteration: int,
    react_snapshot: dict[str, Any],
    client: LLMClient | None,
) -> dict[str, Any]:
    """轻量评估:判断 react_agent 当前方向是否明显跑偏

    参数:
        task: 任务对象
        db: 数据库会话
        round_idx: 当前协作轮次
        iteration: 当前迭代序号
        react_snapshot: react_agent 当前迭代的快照,含:
            - thinking_summary: str  当前迭代的思考摘要(~500字符)
            - tool_intent: str  最近工具调用的意图(一句话)
            - tool_result_summary: str  最近工具结果摘要(~500字符)
            - plan_status: list[dict]  当前 plan 状态
        client: LLMClient(为 None 时用默认)

    返回:
        {"interrupt": bool, "reason": str, "query": str | None}
    """
    client = client or LLMClient()

    # 构造 user 消息:把 react_agent 的快照 + 用户意图放进去
    thinking = (react_snapshot.get("thinking_summary") or "")[:500]
    tool_intent = react_snapshot.get("tool_intent") or "(无工具调用)"
    tool_result = (react_snapshot.get("tool_result_summary") or "")[:500]
    plan_status = react_snapshot.get("plan_status") or []

    plan_text = ""
    if plan_status:
        status_sym = {"pending": "○", "in_progress": "◌", "done": "✓"}
        plan_lines = []
        for s in plan_status:
            sym = status_sym.get(s.get("status", ""), "○")
            plan_lines.append(f"{sym} {s.get('text', '')}")
        plan_text = "\n".join(plan_lines)
    else:
        plan_text = "(无 plan)"

    user_msg = (
        f"用户原始意图:{task.user_input[:500]}\n\n"
        f"当前协作轮次:第 {round_idx} 轮,第 {iteration} 次迭代\n\n"
        f"react_agent 当前思考:\n{thinking}\n\n"
        f"最近工具调用:{tool_intent}\n\n"
        f"最近工具结果:\n{tool_result}\n\n"
        f"当前 plan 状态:\n{plan_text}\n\n"
        f"请判断 react_agent 当前方向是否明显跑偏,是否需要打断纠正。"
    )

    messages = [
        {"role": "system", "content": CHECKPOINT_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    # 流式调用(推送 thinking_delta,前端展示检查点评估过程)
    conv_id = str(uuid.uuid4())
    content_full = _stream_checkpoint_llm(
        client, messages, task_id=task.id, round_idx=round_idx, iteration=iteration,
        conv_id=conv_id,
    )

    # 解析 JSON
    try:
        result = _parse_checkpoint_json(content_full)
    except Exception as e:
        logger.warning(
            f"[task={task.id}] 检查点评估 JSON 解析失败(iteration={iteration}): {e}"
        )
        result = {
            "interrupt": False,
            "reason": f"检查点评估解析失败,默认不打断({e})",
            "query": None,
        }

    # 校验:interrupt=true 时 query 必填
    if result.get("interrupt") and not result.get("query"):
        logger.warning(
            f"[task={task.id}] 检查点评估 interrupt=true 但 query 为空,降级为不打断"
        )
        result["interrupt"] = False
        result["reason"] = result.get("reason", "") + "(query 为空,降级为不打断)"

    # 落库 + 推送 agent_checkpoint 事件
    _record_checkpoint(
        db, task, round_idx, iteration, result, reasoning=content_full
    )

    return result


# ============================================================
# 辅助函数
# ============================================================

def _stream_checkpoint_llm(
    client: LLMClient,
    messages: list[dict[str, str]],
    *,
    task_id,
    round_idx: int,
    iteration: int,
    conv_id: str,
) -> str:
    """流式调用检查点评估 LLM,推送 thinking_delta 事件

    复用 user_agent 的 thinking_delta 推送模式,但 role 标记为 user_agent,
    方便前端在对话流中区分展示。
    """
    content_full = ""

    publish(task_id, "thinking_delta", {
        "conv_id": conv_id,
        "round_idx": round_idx,
        "role": "user_agent",
        "phase": "start",
        "delta": "",
        "iteration": iteration,
    })

    try:
        for chunk in client.chat_stream(messages, max_tokens=1024):
            if chunk.reasoning_delta:
                publish(task_id, "thinking_delta", {
                    "conv_id": conv_id,
                    "round_idx": round_idx,
                    "role": "user_agent",
                    "phase": "reasoning",
                    "delta": chunk.reasoning_delta,
                    "iteration": iteration,
                })
            if chunk.content_delta:
                content_full += chunk.content_delta
            if chunk.finish_reason:
                logger.debug(
                    f"[task={task_id}] 检查点评估流式结束(iteration={iteration}),"
                    f"content={len(content_full)}字符"
                )
    except Exception as e:
        logger.exception(f"[task={task_id}] 检查点评估流式调用失败")
        publish(task_id, "thinking_delta", {
            "conv_id": conv_id,
            "round_idx": round_idx,
            "role": "user_agent",
            "phase": "error",
            "delta": f"[检查点评估失败: {e}]",
            "iteration": iteration,
        })
        raise

    publish(task_id, "thinking_delta", {
        "conv_id": conv_id,
        "round_idx": round_idx,
        "role": "user_agent",
        "phase": "end",
        "delta": "",
        "iteration": iteration,
    })

    return content_full


def _parse_checkpoint_json(content: str) -> dict[str, Any]:
    """解析检查点评估的 JSON 输出

    LLM 可能输出带 ```json ``` 包裹的,需要容错处理。
    """
    # 去除 markdown 代码块包裹
    text = content.strip()
    if text.startswith("```"):
        # 去掉首行 ```json 或 ```
        lines = text.split("\n")
        lines = lines[1:]  # 去首行
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]  # 去尾行
        text = "\n".join(lines).strip()

    result = json.loads(text)

    # 字段校验 + 默认值
    return {
        "interrupt": bool(result.get("interrupt", False)),
        "reason": str(result.get("reason", "")),
        "query": result.get("query") if result.get("query") else None,
    }


def _record_checkpoint(
    db: Session,
    task: Task,
    round_idx: int,
    iteration: int,
    result: dict[str, Any],
    *,
    reasoning: str = "",
) -> None:
    """落库检查点评估结果 + 推送 agent_checkpoint 事件

    落库为 Conversation(role=user_agent, type=evaluation),content 记录
    评估摘要,reasoning 记录完整 JSON 输出供回查。
    """
    interrupt = result.get("interrupt", False)
    reason = result.get("reason", "")
    query = result.get("query")

    if interrupt:
        content = (
            f"[检查点评估 · 第{round_idx}轮迭代{iteration}] 打断\n"
            f"理由:{reason}\n"
            f"追问指令:{query}"
        )
    else:
        content = (
            f"[检查点评估 · 第{round_idx}轮迭代{iteration}] 继续\n"
            f"理由:{reason}"
        )

    conv = Conversation(
        task_id=task.id,
        round_idx=round_idx,
        role="user_agent",
        type="evaluation",
        content=content,
        reasoning=reasoning,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)

    # 推送 agent_checkpoint 事件(前端展示检查点评估卡片)
    publish(task.id, "agent_checkpoint", {
        "round_idx": round_idx,
        "iteration": iteration,
        "interrupt": interrupt,
        "reason": reason,
        "query": query,
    })

    # 同时推 conversation 事件(让前端对话流也能展示)
    publish(task.id, "conversation", {
        "id": str(conv.id),
        "task_id": str(task.id),
        "round_idx": round_idx,
        "role": "user_agent",
        "type": "evaluation",
        "content": content,
        "reasoning": reasoning,
    })
