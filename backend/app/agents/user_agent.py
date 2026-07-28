"""user_agent:用户代理智能体(阶段 4 核心创新)

角色:扮演挑剔的用户,对照 checklist 评估 react_agent 的审计结果,
针对未覆盖的类别追问 react_agent 再跑一轮。

设计要点:
- user_agent 不直接调工具(不 clone/read/search),只做评估和追问
- 它的判断依据是 checklist.json(7 大类必查子项)
- 输出结构化 JSON:covered / missing / followup_query / done
- done=true 表示覆盖完整,user_agent 认为审计可以结束

流程:
1. user_agent 第一次执行,只有用户原始意图,没有 react_agent 结果
   → 输出初始任务描述给 react_agent
2. react_agent 跑一轮,返回 findings + summary
3. user_agent 对照 checklist 评估 react_agent 的 summary:
   - 哪些类别覆盖了
   - 哪些类别漏了
   - 针对漏的类别构造 followup_query
4. 若 missing 为空或 done=true,审计结束
5. 否则把 followup_query 发给 react_agent 再跑一轮
6. 循环 3-5,最多 MAX_ROUNDS 轮(防止无限循环)
"""
import json
import logging
from pathlib import Path
from typing import Any

from app.llm.client import LLMClient

logger = logging.getLogger(__name__)


# 最大追问轮次(防止死循环)
MAX_ROUNDS = 4


# user_agent 的系统提示词
USER_AGENT_PROMPT = """你是一个"用户代理"智能体,你的角色是扮演一个挑剔的安全工程师,
正在审视 react_agent(代码审计智能体)的审计结果,决定是否需要它继续追问。

## 你的职责
1. 对照 checklist(7 大安全类别),评估 react_agent 的审计是否覆盖完整
2. 针对未覆盖或覆盖不足的类别,构造具体的追问请求,让 react_agent 再跑一轮
3. 当 checklist 全部覆盖且结论明确时,宣布审计完成

## 关键原则
- 你**不直接审计代码**,只评估 react_agent 的结果
- 你要像一个资深安全工程师 review 初级工程师的工作
- 你要"挑剔":宁可多问一轮,不要漏掉一个类别
- 追问要具体:不要说"再查查认证",要说"检查 JWT 验证是否缺失 verify=False,以及 IDOR(未检查资源归属)"
- 若 react_agent 已经覆盖某类别并给出明确结论(有/无),不要无意义追问
- 若 react_agent 的结论模糊("可能有问题"但没有具体文件/行号),算作未覆盖

## 输出格式(严格 JSON,不要任何 markdown 代码块)
{
  "covered": ["injection", "deps"],
  "missing": ["auth", "ssrf"],
  "reasoning": "injection 类已查到 SQL 注入,deps 已查 CVE。auth 未提及,ssrf 未提及。",
  "followup_query": "请检查认证与授权模块:1) JWT 验证是否缺失 verify=False;2) 是否存在 IDOR(DELETE/PUT 只检查登录不检查所属)。同时检查 SSRF:requests.get 是否接收用户可控 URL。",
  "done": false
}

字段说明:
- covered: 已覆盖的类别 id 列表(checklist 里的 id)
- missing: 未覆盖或覆盖不足的类别 id 列表
- reasoning: 你的判断依据(简短)
- followup_query: 给 react_agent 的追问指令(若 done=true,此字段可省略)
- done: 是否审计完成(missing 为空且所有类别结论明确时为 true)

## checklist(7 大类别)
{checklist_text}

## 何时返回 done=true
- covered 包含全部 7 个类别
- 且每个类别 react_agent 都给出明确结论(有漏洞/无漏洞/无法确定)
- 不要追求"绝对完美",7 个类别都覆盖了就结束

## 何时返回 done=false
- missing 非空,或某类别结论模糊
- followup_query 要具体到检查点,不要笼统说"再查查"
"""


def _load_checklist() -> dict[str, Any]:
    """加载 checklist.json"""
    path = Path(__file__).parent / "checklist.json"
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _format_checklist_for_prompt(checklist: dict[str, Any]) -> str:
    """把 checklist 格式化成 prompt 友好的文本"""
    lines = []
    for cat in checklist["categories"]:
        lines.append(f"- id: {cat['id']}, 名称: {cat['name']}, CWE: {cat['cwe']}")
        lines.append(f"  描述: {cat['description']}")
        lines.append("  必查子项:")
        for item in cat["checklist"]:
            lines.append(f"    * {item}")
    return "\n".join(lines)


# ============================================================
# user_agent 执行入口
# ============================================================


def run_user_agent(
    user_intent: str,
    react_agent_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    """执行一次 user_agent 评估

    参数:
        user_intent: 用户原始意图(如"审计这个仓库: https://...")
        react_agent_summaries: react_agent 之前几轮的执行结果列表
            每个元素:{"round": 1, "findings": [...], "summary": "..."}

    返回:user_agent 的结构化输出
        {
            "covered": [...],
            "missing": [...],
            "reasoning": str,
            "followup_query": str,
            "done": bool
        }
    """
    checklist = _load_checklist()
    checklist_text = _format_checklist_for_prompt(checklist)

    # 构造 system prompt
    system_prompt = USER_AGENT_PROMPT.replace("{checklist_text}", checklist_text)

    # 构造 user 消息:包含用户意图 + react_agent 之前的所有摘要
    if not react_agent_summaries:
        # 第一轮:user_agent 还没看到 react_agent 结果,直接给初始指令
        user_msg = (
            f"用户原始意图:{user_intent}\n\n"
            f"这是审计开始,react_agent 还没执行。"
            f"请输出你的初始评估:应该覆盖哪些类别?"
            f"输出 followup_query 给 react_agent 的第一轮指令。done=false。"
        )
    else:
        # 后续轮次:把 react_agent 的结果给 user_agent 评估
        rounds_text = []
        for i, r in enumerate(react_agent_summaries, 1):
            findings_summary = _summarize_findings(r.get("findings", []))
            rounds_text.append(
                f"### 第 {i} 轮 react_agent 结果\n"
                f"findings({len(r.get('findings', []))} 个):\n{findings_summary}\n"
                f"summary: {r.get('summary', '(无 summary)')}"
            )
        user_msg = (
            f"用户原始意图:{user_intent}\n\n"
            f"以下是 react_agent 已执行的 {len(react_agent_summaries)} 轮结果:\n\n"
            + "\n\n".join(rounds_text)
            + "\n\n请评估覆盖情况,决定是否追问或结束。"
        )

    # 调 LLM
    client = LLMClient()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    response = client.chat(messages, max_tokens=2048)
    content = response.choices[0].message.content or ""

    # 解析 JSON(LLM 可能输出带 ```json ``` 包裹的)
    try:
        result = _parse_json_response(content)
    except Exception as e:
        logger.error(f"user_agent 输出解析失败: {e},raw: {content[:500]}")
        # 兜底:返回未覆盖,让流程继续
        result = {
            "covered": [],
            "missing": ["injection", "auth", "deserialization", "ssrf",
                        "path_traversal", "crypto", "deps"],
            "reasoning": f"user_agent 输出解析失败,兜底全部 missing: {e}",
            "followup_query": "请重新审计,覆盖所有 7 个安全类别。",
            "done": False,
        }

    return result


# ============================================================
# 辅助函数
# ============================================================


def _summarize_findings(findings: list[dict[str, Any]]) -> str:
    """把 findings 列表摘要成简短文本"""
    if not findings:
        return "(无发现)"
    lines = []
    for f in findings:
        cat = f.get("category", "?")
        sev = f.get("severity", "?")
        fp = f.get("file_path", "?")
        lr = f.get("line_range", "")
        desc = f.get("description", "")[:80]
        lines.append(f"  - [{sev}] {cat} {fp}:{lr} - {desc}")
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
