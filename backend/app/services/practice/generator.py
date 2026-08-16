"""题目生成:把审计任务的真实发现(Result)改编为客观练习题

流程:
1. 取任务 Results(上限 max_findings 条,防 LLM 成本失控)
2. 逐条 finding 调 LLM 生成 1~3 题(漏洞识别 / 成因判断 / 修复选择)
3. json_repair 容错解析 + 字段校验,失败重试 1 次,仍失败丢弃该 finding
4. 知识点 get_or_create(优先 CWE 编号)+ 同用户 sha256 去重
5. 落库为 draft,前端预览确认后转 active

出题模型解析:优先 task.llm_config_id(UserLLMConfig),失败回退 env 默认。
"""
import hashlib
import logging
from typing import Any

from json_repair import repair_json
from sqlalchemy.orm import Session

from app.llm.client import LLMClient
from app.models.practice import KnowledgePoint, Question, QuestionStatus, QuestionType
from app.models.task import Result, Task
from app.models.user_llm_config import UserLLMConfig
from app.services.practice.difficulty import clamp_difficulty

logger = logging.getLogger(__name__)

# 单条 finding 最多生成题数
MAX_QUESTIONS_PER_FINDING = 3
# 解析失败重试次数
PARSE_RETRY = 1

GENERATE_SYSTEM_PROMPT = """你是一名网络安全培训出题专家。基于给定的真实代码审计发现,改编出用于安全培训的客观题。

要求:
1. 出 1~3 道题,题型限定:
   - single_choice(单选):如「该代码片段存在哪种漏洞」「正确的修复方式是」
   - true_false(判断):选项固定为 ["正确", "错误"],题干为一个可判定真伪的陈述
2. 题干必须引用发现中的真实代码/场景,不要泛泛而谈;代码片段单独放 code_snippet
3. 干扰项要有迷惑性但明确错误,正确答案唯一
4. explanation 讲清原理与修复要点,100 字以内
5. difficulty 评估难度(1-5 整数):1=概念识别,3=需理解代码逻辑,5=需深入利用/修复细节
6. knowledge_key:知识点唯一键,优先用 CWE 编号(如 "CWE-89");无对应 CWE 时用英文短标识(如 "hardcoded_secrets")
7. knowledge_name:知识点中文展示名(如 "SQL 注入")

只输出 JSON 数组,不要任何其他文字。每个元素结构:
{"qtype": "single_choice|true_false", "stem": "...", "code_snippet": "..."或null,
 "options": ["...", "..."], "answer_idx": 0, "explanation": "...",
 "difficulty": 3, "knowledge_key": "CWE-89", "knowledge_name": "SQL 注入"}
"""

_FINDING_TEMPLATE = """以下是代码审计任务的一条真实发现:

【标题】{title}

【详细描述】
{content}

【元信息】{metadata}

请基于这条发现出题(1~3 道)。"""


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


def _call_llm(client: LLMClient, finding_text: str) -> str:
    """同步累积 chat_stream 的 content"""
    buf: list[str] = []
    for chunk in client.chat_stream(
        [
            {"role": "system", "content": GENERATE_SYSTEM_PROMPT},
            {"role": "user", "content": finding_text},
        ],
        max_tokens=4096,
    ):
        if chunk.content_delta:
            buf.append(chunk.content_delta)
    return "".join(buf)


def generate_questions_for_task(
    db: Session,
    task: Task,
    user_id,
    max_findings: int = 10,
    client: LLMClient | None = None,
) -> tuple[list[Question], int]:
    """为任务的 Results 生成 draft 题目

    返回 (新建题目列表, 被跳过的 finding 数)。
    """
    if client is None:
        client = resolve_llm_client(db, task)

    findings = (
        db.query(Result).filter(Result.task_id == task.id).order_by(Result.created_at).all()
    )[:max_findings]

    # 已有 dedup_hash(同用户),避免重复入库
    existing_hashes = {
        row[0] for row in db.query(Question.dedup_hash).filter(
            Question.user_id == user_id
        ).all()
    }

    created: list[Question] = []
    skipped = 0
    for finding in findings:
        meta = finding.metadata_ or {}
        prompt = _FINDING_TEMPLATE.format(
            title=finding.title,
            content=(finding.content or "")[:4000],
            metadata=meta,
        )

        questions: list[dict] = []
        for attempt in range(PARSE_RETRY + 1):
            try:
                content = _call_llm(client, prompt)
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
            )
            db.add(question)
            created.append(question)

    db.commit()
    for q in created:
        db.refresh(q)
    return created, skipped
