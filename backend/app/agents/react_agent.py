"""react_agent:基于 ReAct 模式的执行智能体

阶段 4 重构:
- system prompt 从场景取,不再硬编码
- submit_findings → submit_results,字段通用化(title/content/metadata)
- 工具列表从场景白名单取
- 落库到 Result 表(带 round_idx)
- 不再管理 task 状态(由 orchestrator 控制),只负责跑一轮返回结果
- 不再关闭沙箱(由 orchestrator 控制,多轮复用)

阶段 7+:LLM 调用全部流式
- 通过 LLMClient.chat_stream() 拿 StreamChunk
- reasoning_delta / content_delta / tool_call_deltas 都通过 event_bus 推给前端
- 工具调用参数跨 chunk 累积,完整后才执行
"""
import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.event_bus import publish
from app.llm.client import LLMClient
from app.models.task import Conversation, Task
from app.scenarios.base import get_scenario
from app.tools.schema import execute_tool, get_tools_for_scenario, set_current_task

logger = logging.getLogger(__name__)


# 最大迭代轮次,防止死循环
MAX_ITERATIONS = 30
# 连续相同工具调用的容忍次数,超过就打破循环
MAX_SAME_CALLS = 3


def run_react_agent(
    task: Task,
    db: Session,
    round_idx: int = 1,
    followup_query: str | None = None,
    client: LLMClient | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """跑一轮 react_agent

    参数:
        task: 任务对象
        db: 数据库会话
        round_idx: 当前协作轮次(1 开始)
        followup_query: 追问指令。None 表示第一轮(用 task.user_input)
        client: 可选的 LLMClient(阶段 6:从用户配置构造),None 时回退到 env 默认

    返回:(results 列表, summary 文本)
        results: [{"title": str, "content": str, "metadata": dict}]
        summary: react_agent 的总结说明

    注意:本函数不管理 task 状态(不标记 COMPLETED),不关闭沙箱
    """
    # 设置当前任务上下文(供沙箱工具复用会话 + skill 工具按场景过滤)
    task_id_str = str(task.id)
    set_current_task(task_id_str, task.scenario)

    # 获取场景配置
    scenario = get_scenario(task.scenario)
    system_prompt = scenario.react_agent_prompt
    tools = get_tools_for_scenario(task.scenario)

    # 构造初始 user 消息
    if followup_query is None:
        # 第一轮:用 task.user_input
        user_msg = task.user_input
        params = task.params or {}
        if params.get("repo_url"):
            user_msg += f"\n仓库地址: {params['repo_url']}"
        if params.get("branch"):
            user_msg += f"\n分支: {params['branch']}"
    else:
        # 追问轮:不重新 clone,基于已有仓库继续
        # 注入 repo_path + 已有结果摘要,让 LLM 有完整上下文(每轮 messages 是重新构造的)
        from app.tools import sandbox_tools

        ws_info = sandbox_tools.get_workspace_info(task_id_str)
        repo_path_hint = ""
        if ws_info and ws_info.get("repo_path"):
            repo_path_hint = (
                f"\n仓库路径(已 clone,直接用这个路径调 read_file/search_code/list_files): "
                f"{ws_info['repo_path']}"
            )

        # 已提交的结果摘要(让 LLM 知道之前查了什么,避免重复)
        results_hint = ""
        if task.results:
            prev_titles = [r.get("title", "?") for r in task.results[:10]]
            results_hint = (
                f"\n之前已完成的审计项: {', '.join(prev_titles)}"
                + (f" 等 {len(task.results)} 项" if len(task.results) > 10 else "")
            )

        user_msg = (
            f"基于之前的审计结果,现在请针对以下问题继续检查(不需要重新 clone 仓库):"
            f"{repo_path_hint}{results_hint}\n\n"
            f"{followup_query}"
        )

    # 记录 user 指令到对话
    _add_conversation(
        db, task, round_idx=round_idx,
        role="user", type="question",
        content=user_msg,
    )

    # 创建 LLM 客户端(优先用注入的,否则回退到 env 默认)
    client = client or LLMClient()

    # ReAct 循环
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    summary = ""
    recent_calls: list[str] = []

    # plan 状态(代码维护,参考 LangGraph Plan-and-Execute)
    # 初始为空,LLM 首次思考输出 <plan> 后填充;之后每轮由代码推进 in_progress + LLM 确认 done
    current_plan: list[dict] = []

    for iteration in range(1, MAX_ITERATIONS + 1):
        logger.info(f"[task={task.id}] react_agent 第 {round_idx} 轮 / 迭代 {iteration}")

        # 流式调用 LLM,累积 reasoning / content / tool_calls
        # 同时通过 event_bus 实时推送 thinking_delta 给前端
        reasoning_full, content_full, tool_calls_full, _conv_id = _stream_llm_response(
            client, task, db, round_idx, iteration, messages, tools
        )

        # 把 assistant 消息加进上下文(用于下一轮 LLM 调用)
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if content_full:
            assistant_msg["content"] = content_full
        if tool_calls_full:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments_str"],
                    },
                }
                for tc in tool_calls_full
            ]
        messages.append(assistant_msg)

        # 落库思考(content + reasoning),供刷新页面后查看
        # 不推送 SSE!流式卡片已经完整展示了 content + reasoning,
        # 再推 conversation 事件会重复。迟到订阅者通过 GET /tasks/{id} 快照拿完整对话。
        if content_full or reasoning_full:
            _add_conversation(
                db, task, round_idx=round_idx,
                role="react_agent", type="thinking",
                content=content_full,
                reasoning=reasoning_full,
                publish_event=False,
            )
            # 提取计划清单(复杂任务时 react_agent 会在 content 里输出 <plan>...</plan>)
            # 合并 LLM 显式更新到 current_plan(信任 LLM 的 done 标注),
            # 然后推送合并后的完整 plan(覆盖式更新,前端始终看到最新状态)
            llm_plan = _extract_plan(content_full)
            if llm_plan:
                current_plan = _merge_plan(current_plan, llm_plan)
                publish(task.id, "plan", {
                    "round_idx": round_idx,
                    "steps": current_plan,
                })

        # 没有工具调用 → agent 认为做完了
        if not tool_calls_full:
            logger.info(f"[task={task.id}] react_agent 结束(无更多工具调用)")
            if content_full:
                summary = content_full
            break

        # 执行所有工具调用(按出现顺序)
        for tc in tool_calls_full:
            fn_name = tc["name"]
            try:
                fn_args = json.loads(tc["arguments_str"] or "{}")
            except json.JSONDecodeError:
                fn_args = {}

            # 记录工具调用签名(循环检测)
            call_sig = f"{fn_name}:{json.dumps(fn_args, sort_keys=True)}"
            recent_calls.append(call_sig)

            # plan 状态推进:根据工具名推断当前在执行哪个 step,标 in_progress
            # 粗粒度(代码可判),done 标注交给 LLM 在下一轮思考时确认
            if current_plan:
                step_id = _infer_step_from_tool(fn_name, current_plan)
                if step_id is not None:
                    for s in current_plan:
                        if s["id"] == step_id:
                            if s["status"] == "pending":
                                s["status"] = "in_progress"
                                # 推送更新(状态刚变化)
                                publish(task.id, "plan", {
                                    "round_idx": round_idx,
                                    "steps": current_plan,
                                })
                            break

            # 工具意图:人类可读的一句话说明,合并到 content 首行(前端拆分渲染)
            intent = _build_tool_intent(fn_name, fn_args)
            call_detail = f"调用 {fn_name}({json.dumps(fn_args, ensure_ascii=False)[:200]})"
            _add_conversation(
                db, task, round_idx=round_idx,
                role="react_agent", type="tool_call",
                content=f"{intent}\n{call_detail}",
            )

            # 普通工具:执行(submit_results 已移除,react_agent 不再提交结构化结果)
            try:
                result = execute_tool(fn_name, fn_args)
                result_str = json.dumps(result, ensure_ascii=False, default=str)
                _add_conversation(
                    db, task, round_idx=round_idx,
                    role="react_agent", type="tool_result",
                    content=result_str[:500],
                )
                # 完整结果传给 LLM(工具自身已控制返回量:
                # read_file 默认 200 行、search_code 默认 50 匹配等)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_str,
                })
            except Exception as e:
                err_msg = f"工具执行失败: {e}"
                logger.error(f"[task={task.id}] {err_msg}")
                _add_conversation(
                    db, task, round_idx=round_idx,
                    role="react_agent", type="tool_result",
                    content=err_msg,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": err_msg,
                })

        # plan 提醒注入:把当前 plan 状态作为 system 消息加到 messages,
        # 让 LLM 在下一轮思考时看到进度,决定是否标 done(细粒度状态确认)
        # 用可替换的 system 消息(不累积,避免 messages 膨胀):
        # 找到上一轮注入的 plan 提醒就替换,否则追加新的
        if current_plan:
            reminder = _format_plan_reminder(current_plan)
            if reminder:
                # 查找并替换已有的 plan 提醒消息(避免累积)
                replaced = False
                for i in range(len(messages) - 1, -1, -1):
                    msg = messages[i]
                    if msg.get("role") == "system" and msg.get("content", "").startswith(
                        "[系统提醒] 当前计划清单状态"
                    ):
                        msg["content"] = reminder
                        replaced = True
                        break
                if not replaced:
                    messages.append({"role": "system", "content": reminder})

        # 循环检测
        if (
            len(recent_calls) >= MAX_SAME_CALLS
            and len(set(recent_calls[-MAX_SAME_CALLS:])) == 1
        ):
            logger.warning(
                f"[task={task.id}] 检测到连续 {MAX_SAME_CALLS} 次相同调用,打破循环"
            )
            _add_conversation(
                db, task, round_idx=round_idx,
                role="react_agent", type="thinking",
                content=f"检测到连续重复调用 {MAX_SAME_CALLS} 次,强制转入总结",
            )
            messages.append({
                "role": "user",
                "content": (
                    "系统提示:你陷入了重复调用循环。"
                    "请停止调用工具,用自然语言总结当前已确认的发现。"
                ),
            })
    else:
        # 循环跑满了,让 react_agent 输出自然语言总结
        logger.warning(f"[task={task.id}] react_agent 达到最大迭代次数")
        messages.append({
            "role": "user",
            "content": (
                "系统提示:已达最大迭代次数。请用自然语言总结本轮审计的发现,"
                "包括已确认的漏洞、已检查的范围、未完成的检查项。"
            ),
        })
        try:
            reasoning_full, content_full, tool_calls_full, _conv_id = _stream_llm_response(
                client, task, db, round_idx, MAX_ITERATIONS, messages, tools
            )
            # content 作为 summary(自然语言总结)
            if content_full:
                summary = content_full
        except Exception as e:
            logger.error(f"[task={task.id}] 最终总结失败: {e}")

    # react_agent 不再落库 results(由 user_agent 在 done 时调
    # scenario.extract_results 提取并落库)
    if not summary:
        summary = f"第 {round_idx} 轮完成"

    return [], summary


# ============================================================
# 流式 LLM 调用:边收 token 边推送 thinking_delta 给前端
# ============================================================


def _stream_llm_response(
    client: LLMClient,
    task: Task,
    db: Session,
    round_idx: int,
    iteration: int,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> tuple[str, str, list[dict[str, Any]], str]:
    """流式调用 LLM,实时推送 thinking_delta 事件

    返回 (reasoning_full, content_full, tool_calls_full, conv_id)
        - reasoning_full: 完整思考链(供日志/调试,不入 Conversation 表)
        - content_full: 完整回答内容(落库但不再推 conversation 事件,避免和流式卡片重复)
        - tool_calls_full: 完整工具调用列表
            [{"id": str, "name": str, "arguments_str": str, "index": int}]
        - conv_id: 这次 LLM 调用的标识(供调试/日志,前端不再用于去重)
    """
    # 这次 LLM 调用的临时 conv_id(前端按此 key 累积 thinking_delta)
    conv_id = str(uuid.uuid4())
    task_id = task.id

    reasoning_full = ""
    content_full = ""
    # 工具调用累积:index → {id, name, arguments_str}
    tool_calls_acc: dict[int, dict[str, Any]] = {}

    # 推送流开始事件(前端可以创建占位项,显示"正在生成...")
    publish(task_id, "thinking_delta", {
        "conv_id": conv_id,
        "round_idx": round_idx,
        "role": "react_agent",
        "phase": "start",
        "delta": "",
        "iteration": iteration,
    })

    try:
        for chunk in client.chat_stream(messages, tools=tools, tool_choice="auto", max_tokens=4096):
            # 思考链增量
            if chunk.reasoning_delta:
                reasoning_full += chunk.reasoning_delta
                publish(task_id, "thinking_delta", {
                    "conv_id": conv_id,
                    "round_idx": round_idx,
                    "role": "react_agent",
                    "phase": "reasoning",
                    "delta": chunk.reasoning_delta,
                    "iteration": iteration,
                })

            # 正式回答增量
            if chunk.content_delta:
                content_full += chunk.content_delta
                publish(task_id, "thinking_delta", {
                    "conv_id": conv_id,
                    "round_idx": round_idx,
                    "role": "react_agent",
                    "phase": "content",
                    "delta": chunk.content_delta,
                    "iteration": iteration,
                })

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
                        # 后续 chunk 可能补 id / name(理论上第一个 chunk 就有,但保险)
                        if tc_delta.id and not tool_calls_acc[idx]["id"]:
                            tool_calls_acc[idx]["id"] = tc_delta.id
                        if tc_delta.name and not tool_calls_acc[idx]["name"]:
                            tool_calls_acc[idx]["name"] = tc_delta.name
                    # 累积 arguments 片段
                    if tc_delta.arguments_fragment:
                        tool_calls_acc[idx]["arguments_str"] += tc_delta.arguments_fragment

            # finish_reason 出现,流结束
            if chunk.finish_reason:
                logger.debug(
                    f"[task={task.id}] react_agent 流式结束,finish={chunk.finish_reason}, "
                    f"reasoning={len(reasoning_full)}字符, content={len(content_full)}字符, "
                    f"tool_calls={len(tool_calls_acc)}"
                )
    except Exception as e:
        logger.exception(f"[task={task.id}] react_agent 流式调用失败")
        # 推送错误 delta
        publish(task_id, "thinking_delta", {
            "conv_id": conv_id,
            "round_idx": round_idx,
            "role": "react_agent",
            "phase": "error",
            "delta": f"[流式调用失败: {e}]",
            "iteration": iteration,
        })
        raise

    # 推送流结束事件
    publish(task_id, "thinking_delta", {
        "conv_id": conv_id,
        "round_idx": round_idx,
        "role": "react_agent",
        "phase": "end",
        "delta": "",
        "iteration": iteration,
    })

    # 按 index 排序输出
    tool_calls_full = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())]
    return reasoning_full, content_full, tool_calls_full, conv_id


# ============================================================
# 辅助函数
# ============================================================


def _build_tool_intent(fn_name: str, fn_args: dict) -> str:
    """根据工具名+参数生成人类可读的意图说明

    用于工具调用卡片标题,让用户一眼看出"这个工具调用打算做什么"。
    纯模板映射,不调 LLM。未知工具回退到工具名。
    """
    if fn_name == "clone_repo":
        return f"克隆仓库 {fn_args.get('repo_url', '?')}"
    if fn_name == "list_files":
        subdir = fn_args.get("subdir", "")
        return f"查看目录结构: {subdir or '根目录'}"
    if fn_name == "read_file":
        return f"读取文件 {fn_args.get('file_path', '?')}"
    if fn_name == "search_code":
        return f"搜索代码: {fn_args.get('pattern', '?')}"
    if fn_name == "query_cve":
        return (
            f"查询 {fn_args.get('package_name', '?')}@"
            f"{fn_args.get('version', '?')} 的已知漏洞"
        )
    if fn_name == "run_semgrep":
        return "运行 Semgrep 静态分析"
    if fn_name == "list_skills":
        return "查看可用技能列表"
    if fn_name == "skill":
        return f"获取技能指令: {fn_args.get('skill_name', '?')}"
    return f"调用 {fn_name}"


# 计划清单提取:<plan>...</plan> 块,逐行解析序号 + 可选状态标记 + 文本
_PLAN_BLOCK_RE = re.compile(r"<plan>\s*(.*?)\s*</plan>", re.DOTALL)
_PLAN_LINE_RE = re.compile(
    r"^\s*(?:\d+[.、)]\s*)?(?:\[([\w_]+)\]\s*)?(.+)$"
)


def _extract_plan(content: str) -> list[dict] | None:
    """从 thinking content 中提取 <plan>...</plan> 计划清单

    支持格式(状态标记可选,缺省 pending):
        <plan>
        1. [done] 克隆仓库并查看结构
        2. [in_progress] 审计依赖漏洞
        3. [pending] 审计注入类漏洞
        </plan>

    返回 [{"id": 1, "text": "...", "status": "pending|in_progress|done"}]
    无 plan 块或解析为空时返回 None。
    """
    m = _PLAN_BLOCK_RE.search(content)
    if not m:
        return None
    block = m.group(1)
    steps: list[dict] = []
    for i, line in enumerate(block.split("\n"), 1):
        line = line.strip()
        if not line:
            continue
        lm = _PLAN_LINE_RE.match(line)
        if not lm:
            continue
        status = lm.group(1) or "pending"
        text = lm.group(2).strip()
        if status not in ("pending", "in_progress", "done"):
            status = "pending"
        steps.append({"id": i, "text": text, "status": status})
    return steps if steps else None


# ---- plan 状态维护(代码驱动 + LLM 显式更新合并) ----
#
# 设计思路(参考 LangGraph Plan-and-Execute):
# - 代码维护权威 current_plan,不依赖 LLM 每轮重写
# - 工具调用前:根据 tool_name 推断当前 step,标 in_progress(粗粒度,代码可判)
# - 工具调用后:把当前 plan 状态作为 system 提醒注入 messages,
#              让 LLM 在下一轮思考时决定标 done(细粒度,需语义判断)
# - LLM 在 thinking 里输出新 <plan> 时:合并到 current_plan,
#              信任 LLM 的 done 标注(它有 tool_result 上下文,判断更准)
# - 双向同步:代码推进 in_progress,LLM 确认 done

# 工具名 → plan 步骤关键词映射(用于推断当前在执行哪个 step)
# key 是工具名,value 是匹配 step.text 的关键词列表(任一命中即匹配)
_TOOL_STEP_KEYWORDS: dict[str, list[str]] = {
    "clone_repo":      ["克隆", "clone", "仓库"],
    "list_files":      ["结构", "目录", "查看", "list"],
    "read_file":       ["读取", "依赖", "清单", "read"],
    "query_cve":       ["依赖", "cve", "漏洞"],
    "search_code":     ["注入", "密钥", "反序列化", "ssrf", "路径", "认证", "授权",
                         "审计", "代码审计", "search"],
    "run_semgrep":     ["semgrep", "sast", "静态分析"],
    "list_skills":     ["skill", "技能"],
    "skill":           ["skill", "技能"],
}


def _infer_step_from_tool(tool_name: str, plan_steps: list[dict]) -> int | None:
    """根据工具名推断当前在执行哪个 plan step,返回 step id

    匹配规则:tool_name 对应的关键词与 step.text 命中(大小写不敏感)。
    优先匹配 status=pending 的 step(即将开始),其次 in_progress 的 step(正在做)。
    无匹配返回 None。
    """
    keywords = _TOOL_STEP_KEYWORDS.get(tool_name)
    if not keywords or not plan_steps:
        return None

    kw_lower = [k.lower() for k in keywords]
    # 先找 pending 中匹配的(说明进入了新步骤)
    for s in plan_steps:
        if s["status"] != "pending":
            continue
        text_lower = s["text"].lower()
        if any(k in text_lower for k in kw_lower):
            return s["id"]
    # 再找 in_progress 中匹配的(说明还在做同一步骤)
    for s in plan_steps:
        if s["status"] != "in_progress":
            continue
        text_lower = s["text"].lower()
        if any(k in text_lower for k in kw_lower):
            return s["id"]
    return None


def _merge_plan(current: list[dict], llm_update: list[dict]) -> list[dict]:
    """把 LLM 显式输出的 plan 状态合并到 current_plan

    合并策略(信任 LLM 的 done 标注,它有 tool_result 上下文):
    - 按 step.text 匹配(忽略 id,LLM 可能重新编号)
    - LLM 标 done → current 对应 step 标 done
    - LLM 标 in_progress → current 对应 step 标 in_progress
    - LLM 未提及的 step → 保持 current 原状态(代码已推进的 in_progress 不丢)
    - LLM 新增的 step → 追加到 current 末尾
    返回合并后的 plan(新 list,不修改入参)。
    """
    if not llm_update:
        return list(current)
    if not current:
        return [dict(s) for s in llm_update]

    # 用 text 做 key 建索引(忽略首尾空白和大小写差异)
    def _key(text: str) -> str:
        return text.strip().lower()

    current_by_text = {_key(s["text"]): s for s in current}
    llm_by_text = {_key(s["text"]): s for s in llm_update}

    merged: list[dict] = []
    next_id = 1
    # 1. 遍历 current,按 LLM 更新状态(若 LLM 提到)
    for s in current:
        new_s = dict(s)
        new_s["id"] = next_id
        next_id += 1
        k = _key(s["text"])
        if k in llm_by_text:
            # LLM 显式标注了,信任 LLM(尤其是 done)
            new_s["status"] = llm_by_text[k]["status"]
        merged.append(new_s)

    # 2. 追加 LLM 新增的 step(current 里没有的)
    for s in llm_update:
        k = _key(s["text"])
        if k not in current_by_text:
            new_s = dict(s)
            new_s["id"] = next_id
            next_id += 1
            merged.append(new_s)

    return merged


def _format_plan_reminder(plan_steps: list[dict]) -> str:
    """格式化 plan 状态,作为 system 提醒注入 messages

    让 LLM 在下一轮思考时看到当前进度,决定是否标 done。
    """
    if not plan_steps:
        return ""
    lines = ["[系统提醒] 当前计划清单状态(已完成的请标记 [done],正在做的标 [in_progress]):"]
    status_symbol = {"pending": "○", "in_progress": "◌", "done": "✓"}
    for s in plan_steps:
        sym = status_symbol.get(s["status"], "○")
        lines.append(f"{sym} [{s['status']}] {s['text']}")
    lines.append("如果某个步骤已完成,请在回答开头的 <plan> 里将其标为 [done]。")
    return "\n".join(lines)


def _add_conversation(
    db: Session, task: Task, *, round_idx: int, role: str, type: str, content: str,
    reasoning: str | None = None,
    publish_event: bool = True,
) -> None:
    """记录一条对话(带 round_idx),可选推送事件给前端 SSE

    参数:
        reasoning: 思考链(仅 type=thinking 有,模型 reasoning_content 输出)
        publish_event: 是否推送 conversation 事件给前端。
            - True(默认):工具调用/结果/提交/用户指令/user_agent 评估等,
              前端通过 SSE 实时追加到对话列表
            - False:react_agent 的 type=thinking 不推 SSE,
              因为流式卡片已经完整展示了 content + reasoning,
              再推会重复。迟到订阅者通过 GET /tasks/{id} 快照拿完整对话。
    """
    conv = Conversation(
        task_id=task.id,
        round_idx=round_idx,
        role=role,
        type=type,
        content=content,
        reasoning=reasoning,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    if publish_event:
        publish(task.id, "conversation", {
            "id": str(conv.id),
            "round_idx": conv.round_idx,
            "role": conv.role,
            "type": conv.type,
            "content": conv.content,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
        })
