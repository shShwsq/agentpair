"""user_agent:用户代理智能体(阶段 4 核心创新)

角色:扮演挑剔的用户,对照覆盖度清单(checklist)评估 react_agent 的执行结果,
针对未覆盖的类别追问 react_agent 再跑一轮。

场景降级后的设计(关键变更):
- 不再从场景读取固定 checklist。第 0 轮时,user_agent 根据用户意图动态生成
  checklist(覆盖度维度),用户可编辑确认后,后续轮次按此 checklist 评估。
- prompt 通用化,不再场景特化。详见 USER_AGENT_SYSTEM_PROMPT。
- 协作轮(round_idx>=1)从 task.checklist 读取已确认的 checklist。

设计要点:
- user_agent 不直接调工具(不 clone/read/search),只做评估和追问
- 输出结构化 JSON:covered / missing / followup_query / done
  + round 0 额外输出 checklist(动态生成的覆盖度维度)
  + done=true 时输出 grouping(结果分组声明)与 results(结构化结果)
- done=true 表示覆盖完整,user_agent 认为任务可以结束

流程:
1. 第 0 轮:user_agent 根据用户意图生成 checklist + 初始 followup_query
   → orchestrator 推送 checklist 给用户编辑,阻塞等待
   → 用户确认后,checklist 落库 task.checklist
2. react_agent 跑一轮,返回 summary
3. user_agent 对照 checklist 评估 react_agent 的 summary:
   - 哪些类别覆盖了 / 哪些类别漏了
   - 针对漏的类别构造 followup_query
4. 若 missing 为空或 done=true,任务结束(done 时输出 results + grouping)
5. 否则把 followup_query 发给 react_agent 再跑一轮
6. 循环 3-5,最多 MAX_ROUNDS 轮

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
from app.models.task import Conversation, Task

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

# 单次评估中最多调用 verifier_agent 的次数(防止无限验证)
MAX_VERIFY_CALLS = 3

# verify 工具定义(user_agent 可选调用,仅当 task.verifier_enabled 且 policy.allow_verify 时启用)
_VERIFY_TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "verify",
        "description": (
            "在已部署的测试环境动态验证 react_agent 发现的安全问题是否真实可利用。"
            "传入需要验证的安全发现描述,系统会自动构造 PoC 发送到测试环境验证。"
            "验证完成后你会收到验证结果(已确认/未确认/误报 + 证据),据此调整评估。"
            "适用于:静态分析疑似但不确定的漏洞、需要实际触发确认的注入/认证绕过等。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verification_request": {
                    "type": "string",
                    "description": (
                        "需要验证的安全发现描述,应包含:漏洞类型、代码位置、攻击思路。"
                        "如'验证 src/api/users.py 第 42 行的 SQL 注入:用户输入 username "
                        "未参数化直接拼接到 SQL,尝试用 ' OR 1=1-- 验证'"
                    ),
                },
            },
            "required": ["verification_request"],
        },
    },
}


# ============================================================
# 通用 system prompt(场景降级后,不再从场景读取)
# ============================================================

USER_AGENT_SYSTEM_PROMPT = """你是 user_agent(用户代理智能体),扮演一位严谨的技术评审。

## 你的职责
你负责评估 react_agent(执行智能体)的工作是否充分覆盖了任务应有的维度。
你不直接执行代码审计/审查,而是判断 react_agent 的总结是否覆盖完整,
针对遗漏维度追问,直到覆盖完整后宣布结束并整理结构化结果。

## 覆盖度清单(checklist)
{checklist_section}

## 工作流程
1. **第 0 轮(初始评估)**:根据用户意图,生成覆盖度清单(checklist),
   定义本任务应覆盖哪些维度。同时输出初始 followup_query 指导 react_agent 第一轮执行。
2. **协作轮(第 1 轮起)**:对照 checklist 评估 react_agent 的总结,
   标记已覆盖(covered)和未覆盖(missing)的维度,
   针对未覆盖维度构造 followup_query 追问。
3. **结束**:所有维度覆盖完整(done=true)或已达最大轮次时,
   整理结构化结果(results)并声明结果分组方式(grouping)。

## 输出格式(严格 JSON)

### 第 0 轮(初始评估)输出:
```json
{
  "checklist": [
    {"id": "dim_id", "name": "维度名称", "description": "维度说明", "checklist": ["子项1", "子项2"]}
  ],
  "covered": [],
  "missing": [],
  "reasoning": "生成 checklist 的理由 + 初始指令说明",
  "followup_query": "给 react_agent 的初始执行指令",
  "done": false,
  "ask_user": false,
  "questions": []
}
```

### 协作轮输出:
```json
{
  "covered": ["dim_id1"],
  "missing": ["dim_id2"],
  "reasoning": "评估理由:为什么这些已覆盖,那些未覆盖",
  "followup_query": "针对 missing 维度的追问指令(空字符串若 done)",
  "done": false,
  "ask_user": false,
  "questions": []
}
```

### done=true 时的输出(附加 results + grouping):
```json
{
  "covered": ["所有维度id"],
  "missing": [],
  "reasoning": "最终评估理由",
  "followup_query": "",
  "done": true,
  "ask_user": false,
  "questions": [],
  "results": [
    {"title": "结果标题", "content": "结果详细内容", "metadata": {"自定义字段": "值"}}
  ],
  "grouping": {"field": "metadata中的分组字段名", "values": [{"value": "值", "label": "显示名", "color": "颜色key"}]}
}
```
grouping 可为 null(不分组,平铺展示)。

### questions 问题对象格式(ask_user=true 时):
```json
"questions": [
  {"type": "text", "question": "问题文本(必填,放 question 字段)", "placeholder": "输入提示(可选)", "required": false},
  {"type": "choice", "question": "问题文本", "options": [{"value": "val1", "label": "选项显示名"}], "multi": false}
]
```
注意:不需要输出 id(系统自动生成);问题文本必须放在 question 字段。

## checklist 生成原则(第 0 轮)
- 根据用户意图自适应:安全审计任务生成安全维度(注入/认证/反序列化等),
  代码审查任务生成质量维度(可读性/正确性/性能等),其他任务按语义生成。
- 3-8 个维度为宜,每个维度含 3-6 个子项(checklist)。
- 维度 id 用英文下划线命名(如 injection / readability),name 用中文。
- 维度应覆盖该任务类型的主要关注点,不遗漏重要类别。

## 评估原则
- 基于 react_agent 的总结判断覆盖情况,不要臆测未提及的维度已覆盖。
- missing 列表为空是 done 的必要条件,但非充分条件——还需结果质量足够。
- followup_query 应具体可执行,指明 react_agent 需要补充哪些维度的分析。
- 保持覆盖度判断连续性:之前已标 covered 的维度,本轮若 react_agent 未推翻,继续保持。

## 结果整理原则(done=true 时)
- results 从 react_agent 各轮总结中提取结构化发现。
- 每条 result 含 title(简短标题)、content(详细内容)、metadata(自定义字段)。
- grouping 声明前端如何分组展示:field 指定 metadata 中的分组字段,
  values 列出分组枚举(含显示名和颜色)。无明确分组维度时 grouping=null。

## 动态验证(可选,有 verify 工具时)
如果任务配置了测试环境,你可以调用 `verify` 工具动态验证 react_agent 发现的安全问题:
- 对静态分析疑似但不确定的漏洞,调 verify 发送 PoC 到测试环境确认
- 验证结果会作为 tool_result 返回给你,据此在 results 中标注"已确认可利用"或"误报"
- 不要对每个发现都验证,只验证关键的、不确定的;已明确的问题不需要验证
- 验证结果应反映在 results 的 metadata 中(如加 verified: true/false 字段)
"""


def _format_checklist_for_prompt(checklist: list[dict[str, Any]] | None) -> str:
    """把已确认的 checklist 格式化成 prompt 友好的文本

    协作轮(round_idx>=1)使用:从 task.checklist 读取已确认的清单注入 prompt。
    第 0 轮时 checklist 为 None,prompt 提示 LLM 自行生成。
    """
    if not checklist:
        return (
            "本轮尚未有 checklist。请你根据用户意图**动态生成**覆盖度清单,\n"
            "定义本任务应覆盖哪些维度(3-8 个),每个维度含子项。"
        )
    lines = ["以下是已确认的覆盖度清单(用户可能已编辑),你需对照它评估覆盖情况:"]
    for cat in checklist:
        lines.append(f"- id: {cat.get('id', '?')}, 名称: {cat.get('name', '?')}")
        lines.append(f"  描述: {cat.get('description', '')}")
        items = cat.get("checklist", [])
        if items:
            lines.append("  子项:")
            for item in items:
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
    scenario_id: str = "general",
    client: LLMClient | None = None,
    ask_round: int = 0,
    repo_context: str | None = None,
    task_checklist: list[dict[str, Any]] | None = None,
    user_id: UUID | None = None,
    repo_url: str | None = None,
    task: Task | None = None,
    agent_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行一次 user_agent 评估

    参数:
        user_intent: 用户原始意图(如"审计这个仓库: https://...")
        client: 可选的 LLMClient(阶段 6:从用户配置构造),None 时回退到 env 默认
        react_agent_summaries: react_agent 之前几轮的执行结果列表
            每个元素:{"round": 1, "summary": "..."}
        task_id: 任务 ID(必填,用于推送 thinking_delta 事件)
        db: 数据库会话(可选)。传入时用于加载 user_agent 自己之前各轮的评估记录,
            让 user_agent 跨轮记住 covered/missing 判断,避免反复摇摆。
        round_idx: 当前协作轮次(0=初始评估,1+=协作轮)
        scenario_id: 场景标识(场景降级后仅作模板标识,不再驱动 prompt/checklist)
        ask_round: 第 0 轮初始评估时的提问轮次(0=首次评估,1=用户回答后重新评估)
        repo_context: 第 0 轮专用,orchestrator 主动 clone 后的仓库结构上下文。
        task_checklist: 已确认的覆盖度清单(场景降级后从 task.checklist 读取)。
            round_idx=0 时传 None(LLM 动态生成);round_idx>=1 时传已确认清单。
        task: 任务对象(可选)。传入时用于读取 verifier 配置(test_env_url / verifier_enabled)。
        agent_policy: agent 策略(可选)。含 allow_verify 开关,控制是否启用 verify 工具。

    返回:user_agent 的结构化输出
        {
            "covered": [...],
            "missing": [...],
            "reasoning": str,
            "followup_query": str,
            "done": bool,
            "ask_user": bool,
            "questions": [...],
            "checklist": [...],         # 仅 round 0 输出(动态生成)
            "results": [...],           # 仅 done=true 时输出
            "grouping": {...} | null,   # 仅 done=true 时输出
        }
    """
    # 场景降级后:用通用 prompt,checklist 从 task_checklist 注入
    checklist_text = _format_checklist_for_prompt(task_checklist)
    system_prompt = USER_AGENT_SYSTEM_PROMPT.replace("{checklist_section}", checklist_text)

    # 长期记忆注入:User Profile + 全局记忆 + 项目记忆精简版
    # (仅当有内容时,追加到 system prompt 末尾)
    # user_id 为 None(匿名任务)或无配置时 build_*_section 返回空串,不影响原 prompt
    if db is not None and user_id is not None:
        from app.services.memory_injection import build_user_agent_memory_section
        _memory_section = build_user_agent_memory_section(db, user_id, repo_url)
        if _memory_section:
            system_prompt = system_prompt + "\n\n" + _memory_section

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

    # 判断是否启用 verify 工具
    # 条件:task 配了 verifier_enabled + agent_policy.allow_verify + 有 test_env_url
    verify_enabled = (
        task is not None
        and task.verifier_enabled
        and bool(task.test_env_url)
        and (agent_policy or {}).get("allow_verify", False)
    )
    tools = [_VERIFY_TOOL_DEFINITION] if verify_enabled else None

    # LLM 调用循环:处理 verify 工具调用(验证结果回灌后再调 LLM 输出 JSON 评估)
    content = ""
    verify_count = 0
    reasoning_parts: list[str] = []  # 各次流式调用的真实思考链(含 verify 循环)
    while True:
        content, tool_calls, reasoning_chunk = _stream_user_agent_llm(
            client, messages, task_id=task_id, round_idx=round_idx, tools=tools
        )
        if reasoning_chunk:
            reasoning_parts.append(reasoning_chunk)

        # 无工具调用 → content 是 JSON 评估结果,跳出循环
        if not tool_calls:
            break

        # 有工具调用:处理每个 verify 调用
        # 把 assistant 消息(含 tool_calls)加回 messages
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
        assistant_msg["tool_calls"] = [
            {
                "id": tc["id"] or f"call_{tc['index']}",
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments_str"]},
            }
            for tc in tool_calls
        ]
        messages.append(assistant_msg)

        for tc in tool_calls:
            if tc["name"] != "verify":
                # 未知工具调用:返回错误让 LLM 知道
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"] or f"call_{tc['index']}",
                    "content": f"[不支持的工具: {tc['name']}]",
                })
                continue

            if verify_count >= MAX_VERIFY_CALLS:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"] or f"call_{tc['index']}",
                    "content": f"已达验证次数上限({MAX_VERIFY_CALLS}),跳过本次验证。",
                })
                continue

            verify_count += 1
            try:
                args = json.loads(tc["arguments_str"]) if tc["arguments_str"] else {}
            except json.JSONDecodeError:
                args = {}
            verification_request = args.get("verification_request", "")

            # 调用 verifier_agent 执行动态验证
            logger.info(
                f"[task={task_id}] user_agent 调用 verify(第 {verify_count} 次),"
                f"目标: {verification_request[:200]}"
            )
            try:
                from app.agents.verifier_agent import run_verifier_agent
                verify_result = run_verifier_agent(
                    task, db, verification_request, client, round_idx
                )
            except Exception as e:
                logger.exception(f"[task={task_id}] verifier_agent 执行失败")
                verify_result = f"[验证失败: {e}]"

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"] or f"call_{tc['index']}",
                "content": verify_result,
            })

        # 循环回去:LLM 看到验证结果后,要么再调 verify,要么输出 JSON 评估

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
                # 问题文本:LLM 可能把文本写到 text/content 等替代字段,兼容提取
                # (曾因 prompt 未定义问题结构,LLM 用 text 字段导致前端显示"(未提供问题)")
                q_text = (
                    q.get("question") or q.get("text")
                    or q.get("content") or q.get("title")
                )
                q["question"] = str(q_text).strip() if q_text else "(未提供问题)"
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

    # 落库真实思考链(供前端刷新后还原思考卡片,与 react_agent thinking 同机制)。
    # 不推 SSE:流式期间已通过 thinking_delta 在流式卡片展示,推送会重复。
    # 结构化评估记录仍由 orchestrator._record_user_agent 落库(跨轮记忆依赖)。
    reasoning_full = "\n\n".join(p for p in reasoning_parts if p.strip())
    if db is not None and task is not None and reasoning_full:
        try:
            db.add(Conversation(
                task_id=task.id,
                round_idx=round_idx,
                role="user_agent",
                type="thinking",
                content="",
                reasoning=reasoning_full,
            ))
            db.commit()
        except Exception as e:
            logger.warning(f"[task={task_id}] 落库 user_agent 思考链失败(忽略): {e}")

    return result


# ============================================================
# 流式 LLM 调用:user_agent 思考过程实时推送
# ============================================================


def _stream_user_agent_llm(
    client: LLMClient,
    messages: list[dict[str, Any]],
    *,
    task_id: UUID | str,
    round_idx: int = 0,
    tools: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, Any]], str]:
    """流式调用 user_agent 的 LLM,实时推送 thinking_delta 事件

    支持 verify 工具调用:tools 非空时传入 LLM,返回的 tool_calls 供调用方处理。
    无 tools 时行为与原来一致(只产出 reasoning + content)。

    返回 (content_full, tool_calls_full, reasoning_full)
        - content_full: 完整回答内容(JSON 格式的结构化评估结果)
        - tool_calls_full: 工具调用列表 [{"id", "name", "arguments_str", "index"}]
        - reasoning_full: 完整思考链(调用方落库,供前端刷新后还原思考卡片)
    """
    conv_id = str(uuid.uuid4())
    reasoning_full = ""
    content_full = ""
    tool_calls_acc: dict[int, dict[str, Any]] = {}

    # 推送流开始事件
    publish(task_id, "thinking_delta", {
        "conv_id": conv_id,
        "round_idx": round_idx,
        "role": "user_agent",
        "phase": "start",
        "delta": "",
    })

    try:
        for chunk in client.chat_stream(messages, tools=tools, tool_choice="auto", max_tokens=2048):
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
            if chunk.content_delta:
                content_full += chunk.content_delta

            # 工具调用增量(跨 chunk 累积)
            if chunk.tool_call_deltas:
                for tc_delta in chunk.tool_call_deltas:
                    idx = tc_delta.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc_delta.id or "",
                            "name": tc_delta.name or "",
                            "arguments_str": "",
                            "index": idx,
                        }
                    else:
                        if tc_delta.id and not tool_calls_acc[idx]["id"]:
                            tool_calls_acc[idx]["id"] = tc_delta.id
                        if tc_delta.name and not tool_calls_acc[idx]["name"]:
                            tool_calls_acc[idx]["name"] = tc_delta.name
                    if tc_delta.arguments_fragment:
                        tool_calls_acc[idx]["arguments_str"] += tc_delta.arguments_fragment

            if chunk.finish_reason:
                logger.debug(
                    f"[task={task_id}] user_agent 流式结束,finish={chunk.finish_reason}, "
                    f"reasoning={len(reasoning_full)}字符, content={len(content_full)}字符, "
                    f"tool_calls={len(tool_calls_acc)}"
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

    tool_calls_full = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
    return content_full, tool_calls_full, reasoning_full


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
