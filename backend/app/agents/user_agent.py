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

阶段 8(用户澄清):第 0 轮初始评估时,user_agent 若认为用户意图不清晰,
可输出 ask_user=true + questions 列表,orchestrator 推送给前端弹窗,
用户填答后拼回 user_intent 重新评估。最多 MAX_ASKS 轮提问。
"""
import json
import logging
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.event_bus import publish
from app.llm.client import LLMClient
from app.models.task import Conversation
from app.scenarios.base import get_scenario

logger = logging.getLogger(__name__)


# 最大追问轮次(防止死循环)
MAX_ROUNDS = 4

# 第 0 轮初始评估时,最多向用户提问的次数(含首次)
MAX_ASKS = 2

# 跨轮记忆传递:user_agent 之前各轮评估的单条最大字符数与总字符数上限
# 与 react_agent 的对应常量保持一致,避免两边不一致
MAX_HISTORY_MSG_CHARS = 3000
MAX_HISTORY_TOTAL_CHARS = 12000

# 固定追加的"是否有其他补充"问题(由后端追加,LLM 不负责生成)
SUPPLEMENT_QUESTION_ID = "_supplement"
SUPPLEMENT_QUESTION = {
    "id": SUPPLEMENT_QUESTION_ID,
    "type": "text",
    "question": "是否有其他补充?(可选)",
    "placeholder": "如有其他需求或上下文,请在此填写",
    "required": False,
}


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
    db: Session | None = None,
    round_idx: int = 0,
    scenario_id: str = "code_security_audit",
    client: LLMClient | None = None,
    ask_round: int = 0,
    repo_context: str | None = None,
) -> dict[str, Any]:
    """执行一次 user_agent 评估

    参数:
        user_intent: 用户原始意图(如"审计这个仓库: https://...")
        client: 可选的 LLMClient(阶段 6:从用户配置构造),None 时回退到 env 默认
        react_agent_summaries: react_agent 之前几轮的执行结果列表
            每个元素:{"round": 1, "summary": "..."}(results 字段已移除,
            react_agent 只输出自然语言总结,user_agent 从 summary 评估)
        task_id: 任务 ID(必填,用于推送 thinking_delta 事件)
        db: 数据库会话(可选)。传入时用于加载 user_agent 自己之前各轮的评估记录,
            让 user_agent 跨轮记住 covered/missing 判断,避免反复摇摆。
            None 时不加载历史(向后兼容)。
        round_idx: 当前协作轮次(用于推送 thinking_delta 事件)
        scenario_id: 场景标识,用于加载 prompt 和 checklist
        ask_round: 第 0 轮初始评估时的提问轮次(0=首次评估,1=用户回答后重新评估)
            仅 round_idx=0 且 ask_round < MAX_ASKS 时,user_agent 被允许输出
            ask_user=true 触发用户澄清。
        repo_context: 修复 9。第 0 轮专用,orchestrator 主动 clone 后的仓库结构
            上下文。非空时注入到 round 0 的 user_msg(供 user_agent 给更精准的
            初始指令/提问),但不拼到 user_intent(避免膨胀协作轮次的输入)。
            协作轮(round_idx>=1)应传 None。

    返回:user_agent 的结构化输出
        {
            "covered": [...],
            "missing": [...],
            "reasoning": str,
            "followup_query": str,
            "done": bool,
            "ask_user": bool,           # 阶段 8 新增
            "questions": [...],         # ask_user=true 时提供
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
        # 阶段 8:第 0 轮初始评估时,允许 user_agent 提问澄清用户意图
        can_ask = ask_round < MAX_ASKS
        ask_hint = ""
        if can_ask:
            ask_hint = (
                f"\n\n[当前可向用户提问] 这是第 {ask_round + 1} 次评估,"
                f"最多可提问 {MAX_ASKS} 次。如果用户意图不清晰(如缺少仓库地址、"
                f"审计范围模糊、目标不明确),你可以输出 ask_user=true + questions "
                f"列表向用户提问。问题应聚焦于让你能给出有效的 followup_query。"
                f"\n注意:questions 中**不要**包含\"是否有其他补充\"问题,系统会自动追加。"
                f"\n若意图已清晰,直接输出 followup_query,ask_user=false。"
            )
        else:
            ask_hint = (
                "\n\n[已达提问上限] 用户意图已澄清或已达最大提问次数,"
                "请基于现有意图输出 followup_query,ask_user=false。"
            )
        user_msg = (
            f"用户原始意图:{user_intent}\n\n"
            f"这是任务开始,react_agent 还没执行。"
            f"请输出你的初始评估:应该覆盖哪些类别?"
            f"输出 followup_query 给 react_agent 的第一轮指令。done=false。"
            + ask_hint
        )
        # 修复 9:repo_context 仅注入到 round 0 的 user_msg(不拼到 user_intent,
        # 避免被协作轮 user_agent 反复带入)。供 user_agent 参考仓库结构给更精准指令
        if repo_context:
            user_msg += (
                "\n\n[已预克隆仓库结构,供你参考给出初始指令]\n" + repo_context
            )
    else:
        # 后续轮次:把 react_agent 的自然语言总结给 user_agent 评估(不允许再提问)
        # 注意:react_agent 只输出自然语言 summary,不再有结构化 results 字段
        rounds_text = []
        for i, r in enumerate(react_agent_summaries, 1):
            summary = r.get("summary", "(无 summary)")
            rounds_text.append(
                f"### 第 {i} 轮 react_agent 自然语言总结\n{summary}"
            )

        # 跨轮记忆注入:user_agent 看到自己之前各轮的评估记录,
        # 避免在 covered/missing 之间反复摇摆(第 2 轮起注入)
        history_prefix = ""
        if db is not None and round_idx >= 2:
            history_prefix = _build_user_agent_history(db, task_id, round_idx)

        user_msg_parts = [
            f"用户原始意图:{user_intent}\n",
        ]
        if history_prefix:
            user_msg_parts.append(history_prefix)
        user_msg_parts.append(
            f"\n以下是 react_agent 已执行的 {len(react_agent_summaries)} 轮自然语言总结:\n\n"
            + "\n\n".join(rounds_text)
            + "\n\n请评估覆盖情况,决定是否追问或结束。"
            + "\n\n[当前不允许提问] react_agent 已开始执行,ask_user 必须为 false。"
        )
        if history_prefix:
            user_msg_parts.append(
                "\n[记忆提示] 上面已附上你之前各轮的评估记录,请保持覆盖度判断的连续性:"
                "之前已标 covered 的类别,本轮若 react_agent 未推翻结论,继续保持 covered,"
                "不要无意义反复追问。"
            )
        user_msg = "\n".join(user_msg_parts)

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
        # 兜底:直接宣布完成,避免无意义重跑把所有类别再来一遍(浪费 token)
        # results 留空,orchestrator 落库 0 个结果;reasoning 记录失败原因供回查
        last_summary = (
            react_agent_summaries[-1].get("summary", "")
            if react_agent_summaries else ""
        )
        result = {
            "covered": [],
            "missing": [],
            "reasoning": (
                f"user_agent 输出解析失败({e}),直接结束避免无意义重跑。"
                f"最后一条 react_agent 总结:\n{last_summary[:500]}"
            ),
            "followup_query": "",
            "done": True,
            "ask_user": False,
        }

    # 后置约束:非第 0 轮或已达提问上限,强制关闭 ask_user
    if result.get("ask_user") and (round_idx > 0 or ask_round >= MAX_ASKS):
        logger.warning(
            f"[task={task_id}] user_agent 试图提问但已被禁止"
            f"(round_idx={round_idx}, ask_round={ask_round}),强制关闭"
        )
        result["ask_user"] = False
        if not result.get("followup_query"):
            result["followup_query"] = user_intent

    # 校验 questions 结构(ask_user=true 时必须有)
    if result.get("ask_user"):
        questions = result.get("questions") or []
        if not isinstance(questions, list) or not questions:
            logger.warning(
                f"[task={task_id}] user_agent ask_user=true 但 questions 为空,关闭提问"
            )
            result["ask_user"] = False
        else:
            # 规范化:确保每个问题有 id/type/question;过滤掉 LLM 误加的"补充"问题
            normalized = []
            for q in questions:
                if not isinstance(q, dict):
                    continue
                if q.get("id") == SUPPLEMENT_QUESTION_ID:
                    continue  # 系统固定追加,LLM 不应生成
                q.setdefault("id", f"q_{len(normalized) + 1}")
                q_type = q.get("type", "text")
                if q_type not in ("choice", "text"):
                    q_type = "text"
                q["type"] = q_type
                q.setdefault("question", "(未提供问题)")
                if q_type == "choice":
                    if not isinstance(q.get("options"), list) or not q["options"]:
                        # 选择题无选项,降级为填空题
                        q["type"] = "text"
                    else:
                        # 规范化 options
                        norm_opts = []
                        for opt in q["options"]:
                            if isinstance(opt, str):
                                norm_opts.append({"value": opt, "label": opt})
                            elif isinstance(opt, dict):
                                opt.setdefault("value", opt.get("label", ""))
                                opt.setdefault("label", opt["value"])
                                norm_opts.append(opt)
                        q["options"] = norm_opts
                    q.setdefault("multi", False)
                if q_type == "text":
                    q.setdefault("placeholder", "")
                    q.setdefault("required", False)
                normalized.append(q)
            if not normalized:
                result["ask_user"] = False
            else:
                result["questions"] = normalized

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


def _build_user_agent_history(
    db: Session, task_id, current_round_idx: int,
) -> str:
    """加载 user_agent 自己之前各轮的评估记录,作为前缀注入 user_msg

    同一任务内,user_agent 每次调用都是无状态的(messages 只含 system + 当前 user)。
    若不做记忆传递,user_agent 看不到自己之前几轮的 covered/missing 判断,
    可能在 covered/missing 之间反复摇摆,或忘记之前已认定的覆盖情况。

    本函数从 Conversation 表加载 round_idx < current_round_idx 的
    user_agent type=evaluation 记录,提取其 reasoning(完整评估含 covered/
    missing/判断/追问),拼接成文本。单条截断到 MAX_HISTORY_MSG_CHARS,
    整体超 MAX_HISTORY_TOTAL_CHARS 时按"重要性"保留(修复 5):
      - 优先保留 missing 非空的轮次(还有未覆盖项,对决策更有参考价值)
      - 其次保留 done=false 的轮次
      - 同优先级内 FIFO 丢最早轮次

    返回字符串(可能为空)。current_round_idx < 2 时返回空(第 1 轮之前
    只有初始评估,刚输出过,注入意义不大)。
    """
    if current_round_idx < 2:
        return ""

    convs = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.round_idx < current_round_idx,
            Conversation.role == "user_agent",
            Conversation.type == "evaluation",
        )
        .order_by(Conversation.round_idx, Conversation.created_at)
        .all()
    )
    if not convs:
        return ""

    # 逐轮构造记忆段
    segments: list[str] = []
    # 同时记录每段的"重要性"(用于超限时裁剪):missing 非空 > done=false > 其他
    priorities: list[int] = []
    for c in convs:
        # reasoning 是 _record_user_agent 写入的 full_eval(含 covered/missing/判断/追问)
        text = c.reasoning or c.content or ""
        if not text:
            continue
        text = text[:MAX_HISTORY_MSG_CHARS]

        # 解析重要性(从 reasoning 文本粗判)
        # full_eval 格式:"已覆盖: [...]\n未覆盖: [...]\n判断: ..."
        priority = 0
        if "未覆盖: []" not in text and "未覆盖: []" not in text.replace(" ", ""):
            # missing 列表非空 → 最高优先级
            if "未覆盖:" in text:
                missing_part = text.split("未覆盖:")[1].split("\n")[0]
                if missing_part.strip() and missing_part.strip() != "[]":
                    priority = 2
        if priority == 0 and "→ 宣布完成" not in text:
            # done=false → 中等优先级
            priority = 1

        segments.append(f"=== 第 {c.round_idx} 轮 user_agent 评估 ===\n{text}")
        priorities.append(priority)

    if not segments:
        return ""

    # 整体超限时按优先级裁剪:优先级低的先丢;同优先级 FIFO 丢最早
    total = sum(len(s) for s in segments)
    while total > MAX_HISTORY_TOTAL_CHARS and len(segments) > 1:
        # 找最低优先级中最早的一条
        min_priority = min(priorities)
        drop_idx = priorities.index(min_priority)
        dropped = segments.pop(drop_idx)
        priorities.pop(drop_idx)
        total -= len(dropped)

    return "[你之前各轮的评估记录(保持覆盖度判断连续性)]\n" + "\n\n".join(segments)


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
