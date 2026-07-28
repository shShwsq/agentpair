"""react_agent:基于 ReAct 模式的代码安全审计智能体

阶段 1 实现:
- Thought → Action → Observation 循环
- 通过 OpenAI function-calling 调用工具
- 工具集:clone_repo / read_file / search_code
- 结果以结构化 Finding 形式输出

阶段 4 起:user_agent 会在外部接管「评估 + 追问」,react_agent 只负责单轮执行
"""
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.llm.client import LLMClient
from app.models.task import Conversation, Finding, Task, TaskStatus
from app.tools import sandbox_tools
from app.tools.schema import TOOL_DEFINITIONS, execute_tool, set_current_task

logger = logging.getLogger(__name__)


# 最大迭代轮次,防止死循环
MAX_ITERATIONS = 30
# 连续相同工具调用的容忍次数,超过就打破循环
MAX_SAME_CALLS = 3


SYSTEM_PROMPT = """你是一个专业的代码安全审计智能体,使用 ReAct 模式工作:思考 → 调用工具 → 观察结果 → 继续思考。

## 任务
审计用户指定的 GitHub 仓库,查找安全漏洞。

## 工作流程
1. 先调用 clone_repo 克隆仓库
2. **依赖审计**:read_file 读取依赖清单(requirements.txt / package.json / go.mod / pom.xml 等),对每个依赖调 query_cve 查已知 CVE
3. **代码审计**:用 search_code 搜索各类危险模式(注入、硬编码密钥、反序列化、SSRF 等)
4. 对搜到的可疑点用 read_file 查看上下文,判断是否真的是漏洞
5. **SAST 补充**:若沙箱可用,调 run_semgrep 跑自动化静态分析(mock 模式会返回提示,跳过即可)
6. 汇总所有确认的漏洞,调用 submit_findings 提交

## 审计要点(参考 OWASP Top 10 + CWE Top 25)
- 注入类:SQL 拼接、命令注入、模板注入(eval/exec/cursor.execute/os.system)
- 认证与授权:硬编码密码、弱密码哈希、JWT 验证缺失
- 反序列化:pickle/yaml.load/marshal/eval
- SSRF:requests.get(urllib.request.urlopen 中用户可控 URL)
- 硬编码密钥:API key、token、password 字面量
- 路径穿越:文件操作拼接用户输入
- 不安全加密:弱算法(MD5/SHA1 用于密码)、ECB 模式、硬编码 IV
- **已知漏洞**:依赖库的 CVE(通过 query_cve 查询,而非自己判断)

## 工具使用要点
- **query_cve**:对每个依赖调一次,不要批量查。每个 Finding 来自一次调用
- **run_semgrep**:若返回 note 提示 mock 模式不可用,直接跳过,继续后续步骤
- **search_code**:优先搜高危模式,一次搜一个类别,不要把所有模式塞到一个正则里

## 输出规范
所有发现必须通过 submit_findings 工具提交,每个 Finding 包含:
- category: CWE 编号(如 "CWE-89" SQL 注入、"CWE-1035" 已知漏洞依赖)
- severity: info / low / medium / high / critical
- file_path: 文件路径(CVE 类发现写依赖清单文件,如 requirements.txt)
- line_range: 行号或行号范围(如 "42" 或 "42-45")
- description: 漏洞描述
- remediation: 修复建议

CVE 类发现的 category 用 "CWE-1035"(Using Components with Known Vulnerabilities),
description 写明 CVE id 和受影响版本,remediation 写升级到哪个版本。

## 注意
- 不要漏报,但也不要误报。看上下文判断是否真的可利用
- 如果某个模式在测试代码里(如 tests/、*_test.py),通常不算漏洞,但仍需报告为 info
- 仓库 clone 后,先用 search_code 搜索一遍高危模式,再逐个 read_file 确认
- **禁止重复 read 同一个文件**!如果已经读过某个文件,不要再读一遍。换一个文件读,或转入提交阶段
- 单次审计控制在 20 轮以内,确认 3-8 个可疑点后立即 submit_findings 收尾
- 若仓库无明显漏洞,也必须 submit_findings(传空数组),并在 description 里说明已查范围
"""


def run_react_agent(task: Task, db: Session) -> None:
    """执行单轮 react_agent 审计

    阶段 2:工具在沙箱里执行(mock 模式下走本地文件系统)
    阶段 4 起:user_agent 会调用本函数多次,每次带不同的追问请求
    """
    # 标记任务运行中
    task.status = TaskStatus.RUNNING
    task.current_stage = "react_agent 启动中"
    db.commit()

    # 设置当前任务上下文(供沙箱工具复用会话)
    task_id_str = str(task.id)
    set_current_task(task_id_str)

    try:
        # 记录用户提问
        _add_conversation(
            db, task,
            role="user",
            type="question",
            content=f"请审计这个仓库: {task.repo_url}" + (
                f"\n分支: {task.branch}" if task.branch else ""
            ) + (
                f"\n范围: {task.scope}" if task.scope else ""
            ),
        )

        # 创建 LLM 客户端
        client = LLMClient()

        # ReAct 循环
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"请审计这个仓库的安全漏洞: {task.repo_url}"},
        ]

        # 把 submit_findings 工具加进去(内部工具,不在 TOOL_DEFINITIONS 里)
        tools = TOOL_DEFINITIONS + [_submit_findings_tool_def()]

        findings_collected: list[dict[str, Any]] = []
        repo_path: str | None = None
        # 记录最近几次的工具调用签名,用于检测循环
        recent_calls: list[str] = []

        for iteration in range(1, MAX_ITERATIONS + 1):
            task.current_stage = f"react_agent 第 {iteration} 轮思考"
            db.commit()

            logger.info(f"[task={task.id}] react_agent 第 {iteration} 轮")

            # 调用 LLM
            response = client.chat(
                messages,
                tools=tools,
                tool_choice="auto",
                max_tokens=4096,
            )
            msg = response.choices[0].message

            # 把 assistant 消息加进去(包含可能的 tool_calls)
            messages.append(_message_to_dict(msg))

            # 记录思考到对话(如果有 content)
            if msg.content:
                _add_conversation(
                    db, task,
                    role="react_agent",
                    type="thinking",
                    content=msg.content,
                )

            # 没有工具调用 → agent 认为做完了
            if not msg.tool_calls:
                logger.info(f"[task={task.id}] react_agent 结束(无更多工具调用)")
                break

            # 执行所有工具调用
            for tool_call in msg.tool_calls:
                fn_name = tool_call.function.name
                try:
                    fn_args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError as e:
                    fn_args = {}
                    logger.error(f"[task={task.id}] 工具参数解析失败: {e}")

                # 记录工具调用签名(用于循环检测)
                call_sig = f"{fn_name}:{json.dumps(fn_args, sort_keys=True)}"
                recent_calls.append(call_sig)

                # 记录工具调用到对话
                _add_conversation(
                    db, task,
                    role="react_agent",
                    type="tool_call",
                    content=f"调用 {fn_name}({json.dumps(fn_args, ensure_ascii=False)[:200]})",
                )

                # 特殊工具:submit_findings
                if fn_name == "submit_findings":
                    findings_collected = fn_args.get("findings", [])
                    _add_conversation(
                        db, task,
                        role="react_agent",
                        type="tool_result",
                        content=f"已提交 {len(findings_collected)} 个发现",
                    )
                    # 告诉 agent 已收到
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": f"已收到 {len(findings_collected)} 个发现",
                    })
                    continue

                # 普通工具:执行
                try:
                    result = execute_tool(fn_name, fn_args)
                    # 记录结果到对话(截断长结果)
                    result_str = json.dumps(result, ensure_ascii=False, default=str)
                    _add_conversation(
                        db, task,
                        role="react_agent",
                        type="tool_result",
                        content=result_str[:500],
                    )
                    # 把结果塞回 messages(OpenAI 要求 tool role)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_str[:4000],  # 截断,防超长
                    })
                    # 缓存 repo_path 供后续工具用
                    if fn_name == "clone_repo" and isinstance(result, dict):
                        repo_path = result.get("path")
                except Exception as e:
                    err_msg = f"工具执行失败: {e}"
                    logger.error(f"[task={task.id}] {err_msg}")
                    _add_conversation(
                        db, task,
                        role="react_agent",
                        type="tool_result",
                        content=err_msg,
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": err_msg,
                    })

            # 循环检测:检查最近 MAX_SAME_CALLS 次调用是否完全相同
            if (
                len(recent_calls) >= MAX_SAME_CALLS
                and len(set(recent_calls[-MAX_SAME_CALLS:])) == 1
            ):
                same_sig = recent_calls[-1]
                logger.warning(
                    f"[task={task.id}] 检测到连续 {MAX_SAME_CALLS} 次相同调用,打破循环: {same_sig}"
                )
                _add_conversation(
                    db, task,
                    role="react_agent",
                    type="thinking",
                    content=f"检测到连续重复调用 {MAX_SAME_CALLS} 次,强制结束当前轮次,转入结果汇总",
                )
                # 注入提示,引导 LLM 提交结果
                messages.append({
                    "role": "user",
                    "content": (
                        "系统提示:你刚刚连续多次调用同一个工具且参数相同,看起来陷入了循环。"
                        "请停止继续调用相同工具,**立即调用 submit_findings** 提交当前已发现的所有漏洞。"
                        "如果没有发现漏洞,也调用 submit_findings 传空数组并说明原因。"
                    ),
                })
        else:
            # 循环跑满了还没结束
            logger.warning(f"[task={task.id}] react_agent 达到最大迭代次数 {MAX_ITERATIONS}")
            # 注入提示,强制提交
            messages.append({
                "role": "user",
                "content": (
                    "系统提示:已达到最大迭代次数,请**立即调用 submit_findings** "
                    "提交当前已发现的所有漏洞。若没有发现漏洞,传空数组并说明原因。"
                ),
            })
            # 再给一次机会调用 submit_findings
            try:
                final_response = client.chat(
                    messages, tools=tools, tool_choice="auto", max_tokens=4096
                )
                final_msg = final_response.choices[0].message
                if final_msg.tool_calls:
                    for tc in final_msg.tool_calls:
                        if tc.function.name == "submit_findings":
                            try:
                                findings_collected = json.loads(
                                    tc.function.arguments or "{}"
                                ).get("findings", [])
                            except json.JSONDecodeError:
                                findings_collected = []
                            break
            except Exception as e:
                logger.error(f"[task={task.id}] 最终提交失败: {e}")

        # 落库 findings
        for f in findings_collected:
            finding = Finding(
                task_id=task.id,
                category=f.get("category", "CWE-Unknown"),
                severity=f.get("severity", "info"),
                file_path=f.get("file_path"),
                line_range=f.get("line_range"),
                description=f.get("description", ""),
                remediation=f.get("remediation"),
                verified="unverified",
            )
            db.add(finding)

        # 标记完成
        task.status = TaskStatus.COMPLETED
        task.current_stage = f"审计完成,共发现 {len(findings_collected)} 个漏洞"
        from datetime import datetime, timezone

        task.completed_at = datetime.now(timezone.utc)
        db.commit()

        _add_conversation(
            db, task,
            role="react_agent",
            type="finding",
            content=f"审计完成,共发现 {len(findings_collected)} 个漏洞",
        )

    except Exception as e:
        logger.exception(f"[task={task.id}] react_agent 执行失败")
        task.status = TaskStatus.FAILED
        task.error_message = str(e)[:1000]
        task.current_stage = "执行失败"
        db.commit()
        _add_conversation(
            db, task,
            role="react_agent",
            type="error",
            content=f"执行失败: {e}",
        )
    finally:
        # 任务结束,关闭沙箱会话(释放资源)
        try:
            sandbox_tools.close_session(task_id_str)
        except Exception as cleanup_err:
            logger.warning(f"[task={task.id}] 关闭沙箱失败: {cleanup_err}")


# ============================================================
# submit_findings 工具(内部,不在 TOOL_FUNCTIONS 里)
# ============================================================


def _submit_findings_tool_def() -> dict[str, Any]:
    """submit_findings 工具定义

    这是 react_agent 提交最终发现的内部工具,不执行任何外部操作,
    只是把发现收集起来,由 run_react_agent 在循环结束后统一落库
    """
    return {
        "type": "function",
        "function": {
            "name": "submit_findings",
            "description": "提交所有发现的安全漏洞。审计完成后必须调用此工具",
            "parameters": {
                "type": "object",
                "properties": {
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "category": {
                                    "type": "string",
                                    "description": "CWE 编号,如 CWE-89(SQL 注入)、CWE-798(硬编码密钥)",
                                },
                                "severity": {
                                    "type": "string",
                                    "enum": ["info", "low", "medium", "high", "critical"],
                                },
                                "file_path": {"type": "string"},
                                "line_range": {"type": "string"},
                                "description": {"type": "string"},
                                "remediation": {"type": "string"},
                            },
                            "required": ["category", "severity", "description"],
                        },
                    }
                },
                "required": ["findings"],
            },
        },
    }


# ============================================================
# 辅助函数
# ============================================================


def _add_conversation(db: Session, task: Task, *, role: str, type: str, content: str) -> None:
    """记录一条对话"""
    db.add(Conversation(task_id=task.id, role=role, type=type, content=content))
    db.commit()


def _message_to_dict(msg: Any) -> dict[str, Any]:
    """把 OpenAI message 对象转成 dict(便于塞回 messages)"""
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
