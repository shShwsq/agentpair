"""user_agent:用户代理智能体(阶段 4 核心创新)

角色:扮演挑剔的用户,对照场景 checklist 评估 react_agent 的执行结果,
针对未覆盖的类别追问 react_agent 再跑一轮。

设计要点:
- user_agent 不直接调工具(不 clone/read/search),只做评估和追问
- 它的判断依据是场景提供的 checklist
- 输出结构化 JSON:covered / missing / followup_query / done
- done=true 表示覆盖完整,user_agent 认为任务可以结束

流程:
1. user_agent 第一次执行,只有用户原始意图,没有 react_agent 结果
   → 输出初始任务描述给 react_agent
2. react_agent 跑一轮,返回 results + summary
3. user_agent 对照 checklist 评估 react_agent 的 summary:
   - 哪些类别覆盖了
   - 哪些类别漏了
   - 针对漏的类别构造 followup_query
4. 若 missing 为空或 done=true,任务结束
5. 否则把 followup_query 发给 react_agent 再跑一轮
6. 循环 3-5,最多 MAX_ROUNDS 轮(防止无限循环)

通用化:user_agent 的 prompt 从场景取,不绑定任何具体场景语义

阶段 7+:LLM 调用流式,reasoning/content 实时推送 thinking_delta 事件,
前端可见 user_agent 的思考过程。
"""
import json
import logging
import uuid
from typing import Any
from uuid import UUID

from app.event_bus import publish
from app.llm.client import LLMClient
from app.scenarios.base import get_scenario

logger = logging.getLogger(__name__)


# 最大追问轮次(防止死循环)
MAX_ROUNDS = 4


def _load_checklist(scenario_id: str) -> list[dict[str, Any]]:
    """从场景加载 checklist"""
    scenario = get_scenario(scenario_id)
    return scenario.checklist


def _format_checklist_for_prompt(checklist: list[dict[str, Any]]) -> str:
    """把 checklist 格式化成 prompt 友好的文本(通用,只取标准字段)"""
    lines = []
    for cat in checklist:
        lines.append(f"- id: {cat['id']}, 名称: {cat['name']}")
        lines.append(f"  描述: {cat.get('description', '')}")
        lines.append("  必查子项:")
        for item in cat.get("checklist", []):
            lines.append(f"    * {item}")
    return "\n".join(lines)


# ============================================================
# user_agent 执行入口
# ============================================================


def run_user_agent(
    user_intent: str,
    react_agent_summaries: list[dict[str, Any]],
    task_id: UUID | str,
    round_idx: int = 0,
    scenario_id: str = "code_security_audit",
    client: LLMClient | None = None,
) -> dict[str, Any]:
    """执行一次 user_agent 评估

    参数:
        user_intent: 用户原始意图(如"审计这个仓库: https://...")
        client: 可选的 LLMClient(阶段 6:从用户配置构造),None 时回退到 env 默认
        react_agent_summaries: react_agent 之前几轮的执行结果列表
            每个元素:{"round": 1, "results": [...], "summary": "..."}
        task_id: 任务 ID(必填,用于推送 thinking_delta 事件)
        round_idx: 当前协作轮次(用于推送 thinking_delta 事件)
        scenario_id: 场景标识,用于加载 prompt 和 checklist

    返回:user_agent 的结构化输出
        {
            "covered": [...],
            "missing": [...],
            "reasoning": str,
            "followup_query": str,
            "done": bool
        }
    """
    scenario = get_scenario(scenario_id)
    checklist = _load_checklist(scenario_id)
    checklist_text = _format_checklist_for_prompt(checklist)

    # 从场景取 system prompt,替换 checklist 占位符
    system_prompt = scenario.user_agent_prompt.replace("{checklist_text}", checklist_text)

    # 构造 user 消息:包含用户意图 + react_agent 之前的所有摘要
    if not react_agent_summaries:
        # 第一轮:user_agent 还没看到 react_agent 结果,直接给初始指令
        user_msg = (
            f"用户原始意图:{user_intent}\n\n"
            f"这是任务开始,react_agent 还没执行。"
            f"请输出你的初始评估:应该覆盖哪些类别?"
            f"输出 followup_query 给 react_agent 的第一轮指令。done=false。"
        )
    else:
        # 后续轮次:把 react_agent 的结果给 user_agent 评估
        rounds_text = []
        for i, r in enumerate(react_agent_summaries, 1):
            results_summary = _summarize_results(r.get("results", []))
            rounds_text.append(
                f"### 第 {i} 轮 react_agent 结果\n"
                f"results({len(r.get('results', []))} 个):\n{results_summary}\n"
                f"summary: {r.get('summary', '(无 summary)')}"
            )
        user_msg = (
            f"用户原始意图:{user_intent}\n\n"
            f"以下是 react_agent 已执行的 {len(react_agent_summaries)} 轮结果:\n\n"
            + "\n\n".join(rounds_text)
            + "\n\n请评估覆盖情况,决定是否追问或结束。"
        )

    # 调 LLM(流式)
    client = client or LLMClient()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    # 流式调用:边收 token 边推 thinking_delta
    content = _stream_user_agent_llm(
        client, messages, task_id=task_id, round_idx=round_idx
    )

    # 解析 JSON(LLM 可能输出带 ```json ``` 包裹的)
    try:
        result = _parse_json_response(content)
    except Exception as e:
        logger.error(f"user_agent 输出解析失败: {e},raw: {content[:500]}")
        # 兜底:从 checklist 取所有 category id 作为 missing,让流程继续
        all_ids = [cat["id"] for cat in checklist]
        result = {
            "covered": [],
            "missing": all_ids,
            "reasoning": f"user_agent 输出解析失败,兜底全部 missing: {e}",
            "followup_query": "请重新执行,覆盖所有类别。",
            "done": False,
        }

    return result


# ============================================================
# 流式 LLM 调用:user_agent 思考过程实时推送
# ============================================================


def _stream_user_agent_llm(
    client: LLMClient,
    messages: list[dict[str, str]],
    *,
    task_id: UUID | str,
    round_idx: int = 0,
) -> str:
    """流式调用 user_agent 的 LLM,实时推送 thinking_delta 事件

    user_agent 不调工具(无 tool_calls),只产出 reasoning + content。
    content 是 JSON 格式的结构化评估结果。

    返回完整的 content(供后续 JSON 解析)
    """
    conv_id = str(uuid.uuid4())
    reasoning_full = ""
    content_full = ""

    # 推送流开始事件
    publish(task_id, "thinking_delta", {
        "conv_id": conv_id,
        "round_idx": round_idx,
        "role": "user_agent",
        "phase": "start",
        "delta": "",
    })

    try:
        for chunk in client.chat_stream(messages, max_tokens=2048):
            # 思考链增量(推给前端流式卡片显示)
            if chunk.reasoning_delta:
                reasoning_full += chunk.reasoning_delta
                publish(task_id, "thinking_delta", {
                    "conv_id": conv_id,
                    "round_idx": round_idx,
                    "role": "user_agent",
                    "phase": "reasoning",
                    "delta": chunk.reasoning_delta,
                })

            # 正式回答增量(JSON 结构化评估结果)
            # 注意:content 是 JSON 原文,人类可读性差,且后端会把它格式化成
            # evaluation 卡片落库展示。这里只累积不推送,避免前端流式卡片
            # 重复显示同一份信息的 JSON 原文。
            if chunk.content_delta:
                content_full += chunk.content_delta

            if chunk.finish_reason:
                logger.debug(
                    f"[task={task_id}] user_agent 流式结束,finish={chunk.finish_reason}, "
                    f"reasoning={len(reasoning_full)}字符, content={len(content_full)}字符"
                )
    except Exception as e:
        logger.exception(f"[task={task_id}] user_agent 流式调用失败")
        publish(task_id, "thinking_delta", {
            "conv_id": conv_id,
            "round_idx": round_idx,
            "role": "user_agent",
            "phase": "error",
            "delta": f"[流式调用失败: {e}]",
        })
        raise

    # 推送流结束事件
    publish(task_id, "thinking_delta", {
        "conv_id": conv_id,
        "round_idx": round_idx,
        "role": "user_agent",
        "phase": "end",
        "delta": "",
    })

    return content_full


# ============================================================
# 辅助函数
# ============================================================


def _summarize_results(results: list[dict[str, Any]]) -> str:
    """把 results 列表摘要成简短文本(通用,不绑定场景字段)"""
    if not results:
        return "(无结果)"
    lines = []
    for r in results:
        title = r.get("title", "?")[:80]
        content = r.get("content", "")[:60]
        # 通用展示:若有 metadata,取其 keys 拼到行尾(不假设具体字段名)
        metadata = r.get("metadata") or {}
        meta_hint = ""
        if metadata:
            # 取前 3 个 key 做提示
            keys = list(metadata.keys())[:3]
            meta_hint = f" [{', '.join(keys)}]"
        lines.append(f"  - {title} - {content}{meta_hint}")
    return "\n".join(lines)


def _parse_json_response(content: str) -> dict[str, Any]:
    """解析 LLM 输出的 JSON,容忍 markdown 包裹"""
    text = content.strip()

    # 去掉 markdown 代码块包裹
    if text.startswith("```"):
        # 找到第一行和最后一行
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)

    return json.loads(text)
