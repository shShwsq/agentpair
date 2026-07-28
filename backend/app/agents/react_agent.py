"""react_agent:基于 ReAct 模式的执行智能体

阶段 4 重构:
- system prompt 从场景取,不再硬编码
- submit_findings → submit_results,字段通用化(title/content/metadata)
- 工具列表从场景白名单取
- 落库到 Result 表(带 round_idx)
- 不再管理 task 状态(由 orchestrator 控制),只负责跑一轮返回结果
- 不再关闭沙箱(由 orchestrator 控制,多轮复用)
"""
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.llm.client import LLMClient
from app.models.task import Conversation, Result, Task
from app.scenarios.base import get_scenario
from app.tools import sandbox_tools
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
) -> tuple[list[dict[str, Any]], str]:
    """跑一轮 react_agent

    参数:
        task: 任务对象
        db: 数据库会话
        round_idx: 当前协作轮次(1 开始)
        followup_query: 追问指令。None 表示第一轮(用 task.user_input)

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
        user_msg = (
            f"基于之前的审计结果,现在请针对以下问题继续检查(不需要重新 clone 仓库):\n\n"
            f"{followup_query}"
        )

    # 记录 user 指令到对话
    _add_conversation(
        db, task, round_idx=round_idx,
        role="user", type="question",
        content=user_msg,
    )

    # 创建 LLM 客户端
    client = LLMClient()

    # ReAct 循环
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    results_collected: list[dict[str, Any]] = []
    summary = ""
    recent_calls: list[str] = []

    for iteration in range(1, MAX_ITERATIONS + 1):
        logger.info(f"[task={task.id}] react_agent 第 {round_idx} 轮 / 迭代 {iteration}")

        # 调用 LLM
        response = client.chat(
            messages, tools=tools, tool_choice="auto", max_tokens=4096
        )
        msg = response.choices[0].message

        # 把 assistant 消息加进去
        messages.append(_message_to_dict(msg))

        # 记录思考到对话
        if msg.content:
            _add_conversation(
                db, task, round_idx=round_idx,
                role="react_agent", type="thinking",
                content=msg.content,
            )

        # 没有工具调用 → agent 认为做完了
        if not msg.tool_calls:
            logger.info(f"[task={task.id}] react_agent 结束(无更多工具调用)")
            if msg.content:
                summary = msg.content
            break

        # 执行所有工具调用
        for tool_call in msg.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                fn_args = {}

            # 记录工具调用签名(循环检测)
            call_sig = f"{fn_name}:{json.dumps(fn_args, sort_keys=True)}"
            recent_calls.append(call_sig)

            _add_conversation(
                db, task, round_idx=round_idx,
                role="react_agent", type="tool_call",
                content=f"调用 {fn_name}({json.dumps(fn_args, ensure_ascii=False)[:200]})",
            )

            # 特殊工具:submit_results
            if fn_name == "submit_results":
                results_raw = fn_args.get("results", [])
                summary = fn_args.get("summary", "")
                # 用场景的 format_result 格式化
                for raw in results_raw:
                    formatted = scenario.format_result(raw)
                    results_collected.append(formatted)
                _add_conversation(
                    db, task, round_idx=round_idx,
                    role="react_agent", type="submit",
                    content=f"提交 {len(results_collected)} 个结果。summary: {summary[:300]}",
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": f"已收到 {len(results_collected)} 个结果",
                })
                continue

            # 普通工具:执行
            try:
                result = execute_tool(fn_name, fn_args)
                result_str = json.dumps(result, ensure_ascii=False, default=str)
                _add_conversation(
                    db, task, round_idx=round_idx,
                    role="react_agent", type="tool_result",
                    content=result_str[:500],
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result_str[:4000],
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
                    "tool_call_id": tool_call.id,
                    "content": err_msg,
                })

        # submit_results 被调用后,结束循环
        if results_collected:
            break

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
                content=f"检测到连续重复调用 {MAX_SAME_CALLS} 次,强制转入结果提交",
            )
            messages.append({
                "role": "user",
                "content": (
                    "系统提示:你陷入了重复调用循环。"
                    "请立即调用 submit_results 提交当前结果。"
                    "若无结果,传空数组并在 summary 说明原因。"
                ),
            })
    else:
        # 循环跑满了,强制提交
        logger.warning(f"[task={task.id}] react_agent 达到最大迭代次数")
        messages.append({
            "role": "user",
            "content": "系统提示:已达最大迭代次数,请立即调用 submit_results 提交。",
        })
        try:
            final_response = client.chat(
                messages, tools=tools, tool_choice="auto", max_tokens=4096
            )
            final_msg = final_response.choices[0].message
            if final_msg.tool_calls:
                for tc in final_msg.tool_calls:
                    if tc.function.name == "submit_results":
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                            for raw in args.get("results", []):
                                results_collected.append(scenario.format_result(raw))
                            summary = args.get("summary", "")
                        except json.JSONDecodeError:
                            pass
                        break
        except Exception as e:
            logger.error(f"[task={task.id}] 最终提交失败: {e}")

    # 落库 results(带 round_idx)
    for r in results_collected:
        result = Result(
            task_id=task.id,
            round_idx=round_idx,
            title=r.get("title", "(无标题)"),
            content=r.get("content", ""),
            metadata_=r.get("metadata"),
        )
        db.add(result)
    db.commit()

    if not summary:
        summary = f"第 {round_idx} 轮完成,提交 {len(results_collected)} 个结果"

    return results_collected, summary


# ============================================================
# 辅助函数
# ============================================================


def _add_conversation(
    db: Session, task: Task, *, round_idx: int, role: str, type: str, content: str
) -> None:
    """记录一条对话(带 round_idx)"""
    db.add(Conversation(
        task_id=task.id,
        round_idx=round_idx,
        role=role,
        type=type,
        content=content,
    ))
    db.commit()


def _message_to_dict(msg: Any) -> dict[str, Any]:
    """把 OpenAI message 对象转成 dict"""
    d: dict[str, Any] = {"role": msg.role}
    if msg.content:
        d["content"] = msg.content
    if msg.tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in msg.tool_calls
        ]
    return d
