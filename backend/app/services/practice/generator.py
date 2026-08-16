"""题目生成:把审计任务的真实发现(Result)改编为客观练习题

流程:
1. 取任务 Results(上限 max_findings 条,防 LLM 成本失控)
2. 逐条 finding 调 LLM 生成 1~3 题:
   - system prompt 按用户学习主题切换(security/architecture/coding)
   - 工作区可用时挂只读迷你工具循环(read_file / search_code / find_files),
     LLM 自主补全源码上下文提高出题质量;沙箱已清理且用户开启
     「出题前恢复工作区」时先重新 clone 恢复
3. json_repair 容错解析 + 字段校验,失败重试 1 次,仍失败丢弃该 finding
4. 知识点 get_or_create(优先 CWE 编号)+ 同用户 sha256 去重
5. 落库为 draft(记录出题时主题),前端预览确认后转 active

出题模型解析:优先 task.llm_config_id(UserLLMConfig),失败回退 env 默认。
"""
import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

from json_repair import repair_json
from sqlalchemy.orm import Session

from app.llm.client import LLMClient
from app.models.practice import (
    DEFAULT_LEARNING_TOPIC,
    LEARNING_TOPIC_ARCHITECTURE,
    LEARNING_TOPIC_CODING,
    LEARNING_TOPIC_SECURITY,
    KnowledgePoint,
    PracticeSettings,
    Question,
    QuestionStatus,
    QuestionType,
)
from app.models.task import Result, Task
from app.models.user_llm_config import UserLLMConfig
from app.services.practice.difficulty import clamp_difficulty
from app.tools import sandbox_tools

logger = logging.getLogger(__name__)

# 单条 finding 最多生成题数
MAX_QUESTIONS_PER_FINDING = 3
# 解析失败重试次数
PARSE_RETRY = 1
# 出题工具循环最大轮次(每轮可含多次并行工具调用;超限后强制无工具出题)
MAX_TOOL_ROUNDS = 4
# 单次工具结果回传 LLM 的截断阈值(防上下文爆炸)
_MAX_TOOL_RESULT_CHARS = 3000

# ============================================================
# 提示词:按学习主题切换出题视角(通用规则段共享)
# ============================================================

_SECURITY_TOPIC_HEAD = """你是一名网络安全培训出题专家。基于给定的真实代码审计发现,改编出用于安全培训的客观题。

## 出题视角
- 漏洞识别:该代码片段存在哪种漏洞
- 成因判断:漏洞根因与触发条件
- 修复选择:正确的修复方式与安全编码实践
- knowledge_key 优先用 CWE 编号(如 "CWE-89");无对应 CWE 时用英文短标识(如 "hardcoded_secrets")"""

_ARCHITECTURE_TOPIC_HEAD = """你是一名软件架构培训出题专家。基于给定的真实代码分析发现,从架构设计视角改编出客观题。

## 出题视角
- 模块边界与职责划分、分层是否合理、依赖方向
- 设计模式的应用与误用、技术选型权衡(一致性/性能/可维护性)
- 耦合与内聚、扩展性、可测试性缺陷及其改进方案
- 即使发现本身是安全/质量问题,也应从架构成因或设计改进角度出题
- knowledge_key 用英文短标识(如 "layering"、"circular_dependency"、"observer_pattern")"""

_CODING_TOPIC_HEAD = """你是一名通用编码能力培训出题专家。基于给定的真实代码分析发现,改编出考察通用编码能力的客观题。

## 出题视角
- bug 识别与边界条件、异常与错误处理
- 代码坏味道与可读性、性能问题(如 N+1 查询、不必要的重复计算)
- 语言特性正确用法与工程最佳实践(测试、命名、API 设计)
- knowledge_key 用英文短标识(如 "null_safety"、"exception_handling"、"n_plus_one_query")"""

_TOPIC_PROMPT_HEADS = {
    LEARNING_TOPIC_SECURITY: _SECURITY_TOPIC_HEAD,
    LEARNING_TOPIC_ARCHITECTURE: _ARCHITECTURE_TOPIC_HEAD,
    LEARNING_TOPIC_CODING: _CODING_TOPIC_HEAD,
}

# 工作区可用时注入的工具说明段(工具实际可用与否与 sandbox 存活状态一致)
_TOOL_SECTION = """## 源码查阅工具(仓库已 clone,可调用)
发现描述里代码不完整时,你可以先调工具查阅真实源码再出题:
- read_file(file_path, max_lines?, offset?):读仓库文件(带行号,分页)
- search_code(pattern, file_glob?, output_mode?):正则搜索代码;
  output_mode="files_with_matches" 可快速定位含关键词的文件
- find_files(pattern):按 glob 递归查文件路径(如 **/*.py)
工具轮数有限,查不到就基于现有信息出题。凡提供或读到源码的,
题干与 code_snippet 必须引用真实源码,不得虚构。"""

# 三主题共享的输出规则段
_COMMON_RULES = """## 通用要求
1. 出 1~3 道题,题型限定:
   - single_choice(单选):如「该代码片段存在哪种问题」「正确的改进方式是」
   - true_false(判断):选项固定为 ["正确", "错误"],题干为一个可判定真伪的陈述
2. 题干必须引用发现中的真实代码/场景,不要泛泛而谈;代码片段单独放 code_snippet
3. 干扰项要有迷惑性但明确错误,正确答案唯一
4. explanation 讲清原理与改进要点,100 字以内
5. difficulty 评估难度(1-5 整数):1=概念识别,3=需理解代码逻辑,5=需深入细节
6. knowledge_name:知识点中文展示名(如 "SQL 注入")
7. 元信息中若标注 verified: false 或判定为误报的发现,不要出题,直接返回空数组 []

只输出 JSON 数组,不要任何其他文字。每个元素结构:
{"qtype": "single_choice|true_false", "stem": "...", "code_snippet": "..."或null,
 "options": ["...", "..."], "answer_idx": 0, "explanation": "...",
 "difficulty": 3, "knowledge_key": "...", "knowledge_name": "..."}"""


def build_system_prompt(topic: str, workspace_available: bool) -> str:
    """按学习主题拼出题 system prompt;工作区可用时附工具说明段"""
    head = _TOPIC_PROMPT_HEADS.get(topic) or _TOPIC_PROMPT_HEADS[LEARNING_TOPIC_SECURITY]
    sections = [head]
    if workspace_available:
        sections.append(_TOOL_SECTION)
    sections.append(_COMMON_RULES)
    return "\n\n".join(sections)


# ============================================================
# 出题工具循环(只读三工具;repo_path/task_id 由宿主注入,LLM 不感知)
# ============================================================

_PRACTICE_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取仓库内文件内容(带行号,支持 offset 翻页)。发现描述缺少具体代码时,先读相关源文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "仓库内相对路径"},
                    "max_lines": {"type": "integer", "description": "本次最多返回行数,默认 100"},
                    "offset": {"type": "integer", "description": "从第几行开始读(1-based),默认 1"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "正则搜索仓库代码,定位相关函数、字符串或调用点",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "file_glob": {"type": "string", "description": "文件名过滤 glob(可选),如 *.py"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches"],
                        "description": "content=匹配行+行号;files_with_matches=仅文件路径(快速定位)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "按 glob 模式递归查找文件路径(不看内容),如 **/*.py",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式"},
                },
                "required": ["pattern"],
            },
        },
    },
]


def _execute_practice_tool(task_id: str, repo_path: str, name: str, args: dict) -> str:
    """执行出题工具循环的只读工具,返回截断后的 JSON 文本"""
    try:
        if name == "read_file":
            result = sandbox_tools.read_file(
                repo_path,
                str(args.get("file_path") or ""),
                max_lines=max(1, min(int(args.get("max_lines") or 100), 150)),
                offset=max(1, int(args.get("offset") or 1)),
                task_id=task_id,
            )
        elif name == "search_code":
            result = sandbox_tools.search_code(
                repo_path,
                str(args.get("pattern") or ""),
                file_glob=args.get("file_glob"),
                max_matches=max(1, min(int(args.get("max_matches") or 30), 50)),
                output_mode=str(args.get("output_mode") or "content"),
                task_id=task_id,
            )
        elif name == "find_files":
            result = sandbox_tools.find_files(
                repo_path, str(args.get("pattern") or ""), task_id=task_id,
            )
        else:
            return f"未知工具: {name}"
        return json.dumps(result, ensure_ascii=False, default=str)[:_MAX_TOOL_RESULT_CHARS]
    except Exception as e:
        return f"工具执行失败: {e}"


def _stream_one_round(
    client: LLMClient, messages: list[dict], tools: list[dict] | None,
) -> tuple[str, list[dict]]:
    """一轮 chat_stream:累积正式回复与工具调用增量(参数跨 chunk 拼接)"""
    content_parts: list[str] = []
    tool_calls_acc: dict[int, dict] = {}
    for chunk in client.chat_stream(messages, max_tokens=4096, tools=tools):
        if chunk.content_delta:
            content_parts.append(chunk.content_delta)
        for d in chunk.tool_call_deltas or []:
            slot = tool_calls_acc.setdefault(
                d.index, {"id": "", "name": "", "arguments_str": ""},
            )
            if d.id and not slot["id"]:
                slot["id"] = d.id
            if d.name and not slot["name"]:
                slot["name"] = d.name
            if d.arguments_fragment:
                slot["arguments_str"] += d.arguments_fragment
    tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
    return "".join(content_parts), tool_calls


def _call_llm(
    client: LLMClient, system_prompt: str, finding_text: str,
    task_id: str, repo_path: str,
) -> str:
    """出题 LLM 调用:工作区可用 → 有界工具循环;否则单次直出"""
    tools = _PRACTICE_TOOL_DEFINITIONS if repo_path else None
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": finding_text},
    ]
    for _ in range(MAX_TOOL_ROUNDS):
        content, tool_calls = _stream_one_round(client, messages, tools)
        if not tool_calls:
            return content
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
        assistant_msg["tool_calls"] = [
            {
                "id": tc["id"] or f"call_{i}",
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments_str"]},
            }
            for i, tc in enumerate(tool_calls)
        ]
        messages.append(assistant_msg)
        for i, tc in enumerate(tool_calls):
            try:
                args = json.loads(tc["arguments_str"]) if tc["arguments_str"] else {}
            except json.JSONDecodeError:
                args = {}
            result_str = _execute_practice_tool(task_id, repo_path, tc["name"], args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"] or f"call_{i}",
                "content": result_str,
            })
    # 超工具轮数:去掉工具强制收口出题
    content, _ = _stream_one_round(client, messages, None)
    return content


# ============================================================
# 工作区保障(沙箱已清理时按用户设置重新 clone)
# ============================================================


def _load_git_tokens(db: Session, user_id) -> dict[str, str]:
    """加载用户 git provider 的 access_token(与 orchestrator._load_git_tokens 同逻辑)"""
    if user_id is None:
        return {}
    try:
        from app.models.user_git_binding import UserGitBinding
        from app.security import decrypt_secret

        bindings = (
            db.query(UserGitBinding)
            .filter(
                UserGitBinding.user_id == user_id,
                UserGitBinding.access_token != "",
            )
            .all()
        )
        tokens: dict[str, str] = {}
        for b in bindings:
            try:
                tokens[b.provider] = decrypt_secret(b.access_token)
            except Exception as e:
                logger.warning("[practice] 解密 %s token 失败: %s", b.provider, e)
        return tokens
    except Exception as e:
        logger.warning("[practice] 加载 git token 失败: %s", e)
        return {}


def _ensure_workspace(
    db: Session, task: Task, settings_row: PracticeSettings | None,
) -> dict | None:
    """保障出题用的工作区,返回 workspace info(含 repo_path;不可用为 None)

    - 沙箱 session 存活 → 直接复用
    - 已清理 + 用户开启 restore_workspace_for_practice + 任务有 repo_url
      → 重新 clone 恢复(成功后标记 completed 纳入 1 小时 TTL 清理序列)
    - 恢复失败/条件不满足 → 静默降级(出题走无工具路径)
    """
    task_id_str = str(task.id)
    info = sandbox_tools.get_workspace_info(task_id_str)
    if info and info.get("repo_path"):
        return info
    params = task.params or {}
    repo_url = params.get("repo_url")
    if not repo_url or settings_row is None or not settings_row.restore_workspace_for_practice:
        return info
    logger.info("[task=%s] 出题前沙箱已清理,重新 clone 恢复工作区", task.id)
    try:
        sandbox_tools.clone_repo_with_fallback(
            repo_url,
            branch=params.get("branch"),
            task_id=task_id_str,
            git_tokens=_load_git_tokens(db, task.user_id),
        )
        # 恢复的 session 属于已完成任务:纳入 TTL 清理序列,避免常驻泄漏
        sandbox_tools.mark_task_completed(task_id_str)
        return sandbox_tools.get_workspace_info(task_id_str)
    except Exception as e:
        logger.warning("[task=%s] 出题前恢复工作区失败(降级为无工具出题): %s", task.id, e)
        return sandbox_tools.get_workspace_info(task_id_str)


# ============================================================
# 原有管线:模型解析 / 校验 / 去重 / 落库
# ============================================================


def resolve_llm_client(db: Session, task: Task) -> LLMClient:
    """按 task.llm_config_id 解析出题模型,失败回退 env 默认"""
    if task.llm_config_id:
        try:
            cfg_row = db.query(UserLLMConfig).filter(
                UserLLMConfig.user_id == task.user_id
            ).first()
            if cfg_row:
                for cfg in cfg_row.llm_configs or []:
                    if cfg.get("id") == task.llm_config_id:
                        return LLMClient.from_config_dict(cfg)
            logger.warning(
                "[practice] 未找到 llm_config_id=%s,回退 env 默认", task.llm_config_id
            )
        except Exception as e:
            logger.warning("[practice] 加载出题模型配置失败,回退 env 默认: %s", e)
    return LLMClient()


def compute_dedup_hash(stem: str, code_snippet: str | None) -> str:
    return hashlib.sha256(
        f"{stem.strip()}\n{(code_snippet or '').strip()}".encode("utf-8")
    ).hexdigest()


def _get_or_create_knowledge_point(
    db: Session, user_id, key: str, name: str
) -> KnowledgePoint:
    key = (key or "").strip() or "general"
    kp = db.query(KnowledgePoint).filter(
        KnowledgePoint.user_id == user_id,
        KnowledgePoint.key == key,
    ).first()
    if kp:
        return kp
    kp = KnowledgePoint(
        user_id=user_id,
        key=key,
        name=(name or "").strip() or key,
        category="cwe" if key.upper().startswith("CWE-") else "general",
    )
    db.add(kp)
    db.flush()
    return kp


def _normalize_raw_question(raw: Any, finding_meta: dict) -> dict | None:
    """校验并规范化 LLM 输出的单题结构,非法返回 None"""
    if not isinstance(raw, dict):
        return None

    qtype = str(raw.get("qtype") or "").strip()
    if qtype not in ("single_choice", "true_false"):
        return None

    stem = str(raw.get("stem") or "").strip()
    if not stem:
        return None

    options = raw.get("options")
    if qtype == "true_false":
        options = ["正确", "错误"]
    else:
        if not isinstance(options, list):
            return None
        options = [str(o).strip() for o in options if str(o).strip()]
        if len(options) < 2 or len(options) > 8:
            return None

    try:
        answer_idx = int(raw.get("answer_idx"))
    except (TypeError, ValueError):
        return None
    if not (0 <= answer_idx < len(options)):
        return None

    # 单选题全同选项无效
    if qtype == "single_choice" and len(set(options)) < 2:
        return None

    difficulty = raw.get("difficulty", 3)
    try:
        difficulty = clamp_difficulty(float(difficulty))
    except (TypeError, ValueError):
        difficulty = 3.0

    # knowledge_key 优先用 finding 元信息里的 CWE(比 LLM 输出可靠)
    cwe = str(finding_meta.get("cwe") or "").strip().upper()
    if cwe and not cwe.startswith("CWE-") and cwe.isdigit():
        cwe = f"CWE-{cwe}"
    knowledge_key = cwe or str(raw.get("knowledge_key") or "").strip() or "general"

    code_snippet = raw.get("code_snippet")
    code_snippet = str(code_snippet).strip() if code_snippet else None

    return {
        "qtype": qtype,
        "stem": stem,
        "code_snippet": code_snippet,
        "options": options,
        "answer_idx": answer_idx,
        "explanation": str(raw.get("explanation") or "").strip(),
        "difficulty": difficulty,
        "knowledge_key": knowledge_key,
        "knowledge_name": str(raw.get("knowledge_name") or "").strip(),
    }


def _parse_llm_questions(content: str, finding_meta: dict) -> list[dict]:
    """json_repair 容错解析 LLM 输出 → 规范题目列表"""
    text = (content or "").strip()
    if not text:
        return []
    try:
        result = repair_json(text, return_objects=True)
    except Exception:
        return []
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        return []
    questions = []
    for raw in result:
        q = _normalize_raw_question(raw, finding_meta)
        if q:
            questions.append(q)
    return questions[:MAX_QUESTIONS_PER_FINDING]


_FINDING_TEMPLATE = """以下是代码审计任务的一条真实发现:

【标题】{title}

【详细描述】
{content}

【元信息】{metadata}

请基于这条发现出题(1~3 道)。"""


def generate_questions_for_task(
    db: Session,
    task: Task,
    user_id,
    max_findings: int = 10,
    client: LLMClient | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[list[Question], int]:
    """为任务的 Results 生成 draft 题目

    返回 (新建题目列表, 被跳过的 finding 数)。
    progress_callback(done, total):每处理完一条 finding 回调(异步生成进度展示用)。
    """
    if client is None:
        client = resolve_llm_client(db, task)

    # 用户练习设置:学习主题决定提示词;是否允许出题前恢复工作区
    settings_row = db.query(PracticeSettings).filter(
        PracticeSettings.user_id == user_id
    ).first()
    topic = (
        settings_row.learning_topic if settings_row else DEFAULT_LEARNING_TOPIC
    )

    # 工作区:存活 → 挂工具循环;已清理 → 按设置尝试重新 clone
    ws_info = _ensure_workspace(db, task, settings_row)
    repo_path = (ws_info or {}).get("repo_path") or ""
    system_prompt = build_system_prompt(topic, workspace_available=bool(repo_path))
    task_id_str = str(task.id)

    findings = (
        db.query(Result).filter(Result.task_id == task.id).order_by(Result.created_at).all()
    )[:max_findings]
    total_findings = len(findings)

    # 已有 dedup_hash(同用户),避免重复入库
    existing_hashes = {
        row[0] for row in db.query(Question.dedup_hash).filter(
            Question.user_id == user_id
        ).all()
    }

    created: list[Question] = []
    skipped = 0
    for idx, finding in enumerate(findings):
        meta = finding.metadata_ or {}
        prompt = _FINDING_TEMPLATE.format(
            title=finding.title,
            content=(finding.content or "")[:4000],
            metadata=meta,
        )

        questions: list[dict] = []
        for attempt in range(PARSE_RETRY + 1):
            try:
                content = _call_llm(client, system_prompt, prompt, task_id_str, repo_path)
            except Exception as e:
                logger.warning(
                    "[practice] finding=%s LLM 调用失败(第 %d 次): %s",
                    finding.id, attempt + 1, e,
                )
                content = ""
            questions = _parse_llm_questions(content, meta)
            if questions:
                break

        if not questions:
            skipped += 1
            if progress_callback:
                progress_callback(idx + 1, total_findings)
            continue

        for q in questions:
            dedup_hash = compute_dedup_hash(q["stem"], q["code_snippet"])
            if dedup_hash in existing_hashes:
                continue
            existing_hashes.add(dedup_hash)

            kp = _get_or_create_knowledge_point(
                db, user_id, q["knowledge_key"], q["knowledge_name"]
            )
            question = Question(
                user_id=user_id,
                source_task_id=task.id,
                source_result_id=finding.id,
                knowledge_point_id=kp.id,
                qtype=QuestionType(q["qtype"]),
                stem=q["stem"],
                code_snippet=q["code_snippet"],
                options=q["options"],
                answer_idx=q["answer_idx"],
                explanation=q["explanation"],
                difficulty=q["difficulty"],
                status=QuestionStatus.DRAFT,
                dedup_hash=dedup_hash,
                learning_topic=topic,
            )
            db.add(question)
            created.append(question)

        if progress_callback:
            progress_callback(idx + 1, total_findings)

    db.commit()
    for q in created:
        db.refresh(q)
    return created, skipped
