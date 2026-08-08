"""任务完成后自动归纳写入长期记忆(钩子)。

由 orchestrator 在任务成功完成段调用。失败兜底:任何异常都不影响任务完成状态。
借鉴 react_agent._llm_compress_history 的"关 thinking 加速 + 截断 + 降级"范式。

写入策略:
- 项目记忆:按 repo_url 归一化找到/创建 Project,把 LLM 归纳的 project_memory_update
  (结构化 list[{category,item}])按类别去重合并到 memory_content(## 类别 + - 条目格式,
  超长先删 Legacy Notes 块再尾部截断)
- 全局记忆:同理把 global_memory_update 合并到 UserMemory.content
- 旧格式(\n---\n 分隔的纯文本)读取时整体归入 ## Legacy Notes 块,零迁移兼容。

并发兜底:Project 表 UNIQUE(user_id, repo_url_normalized) 约束,并发 INSERT 抛
IntegrityError,catch 后 rollback 回查已建行。
"""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.llm.client import LLMClient
from app.models.project import Project
from app.models.task import Conversation, Task
from app.models.user_memory import UserMemory
from app.services.repo_url import normalize_repo_url

logger = logging.getLogger(__name__)

# 记忆合并后的存储上限(超长先删 Legacy 块,再尾部截断保留新内容)
MAX_PROJECT_MEM_STORE = 8000
MAX_GLOBAL_MEM_STORE = 10000

# 记忆类别固定枚举(按此顺序输出)
PROJECT_CATEGORIES = [
    "Hard Constraints",
    "Known Issues",
    "Audit Directions",
    "Tech Stack",
    "Lessons Learned",
]
GLOBAL_CATEGORIES = [
    "Hard Constraints",
    "Tech Stack",
    "Preferences",
    "Lessons Learned",
]

# 归纳 prompt(要求输出严格 JSON,带类别结构)
_SUMMARIZE_PROMPT = """You are a memory curator. Based on the task execution records below, extract durable knowledge that will help future tasks of the same kind.

[Repository]
{repo_url}

[User intent]
{user_intent}

[react_agent per-round summaries]
{react_summaries}

[user_agent final evaluation]
{ua_reasoning}

Rules:
- Write in English. Preserve language-specific Chinese terms, user quotes, and UI strings verbatim (do NOT translate them).
- Each item must be a single concise line. No multi-paragraph prose.
- Only include genuinely reusable knowledge (constraints, known pitfalls, audit directions, tech stack facts, preferences, lessons). Skip one-off task details.

Categorize each item. Allowed categories:
- project_memory_update: {project_categories}
- global_memory_update: {global_categories}

Output STRICT JSON (no markdown fences). Use empty arrays if nothing new.
{{
  "project_memory_update": [
    {{"category": "Hard Constraints", "item": "..."}},
    {{"category": "Known Issues", "item": "..."}}
  ],
  "global_memory_update": [
    {{"category": "Preferences", "item": "..."}}
  ]
}}
"""


def summarize_and_save_memory(
    task: Task, db: Session, client: LLMClient | None,
) -> None:
    """任务完成后归纳写入记忆。

    前置条件:task.status == COMPLETED(由调用方保证,失败任务不调)。
    user_id 为 None(匿名任务)或无 repo_url → 跳过。
    任何异常都 catch + log,不影响任务完成。
    """
    if task.user_id is None:
        return
    try:
        params = task.params or {}
        repo_url = params.get("repo_url")
        if not repo_url:
            # 无仓库的任务不写项目记忆(MVP 跳过,避免无仓库归属的归纳)
            return

        react_summaries = _load_react_summaries_text(db, task.id)
        ua_reasoning = _load_final_ua_eval(db, task.id)
        if not react_summaries and not ua_reasoning:
            return

        llm = client or LLMClient()
        # 关思考加速(归纳是简单任务,不需要深度思考)
        original_thinking = llm.enable_thinking
        llm.enable_thinking = False
        try:
            prompt = _SUMMARIZE_PROMPT.format(
                repo_url=repo_url,
                user_intent=(task.user_input or "")[:1000],
                react_summaries=react_summaries[:8000],
                ua_reasoning=ua_reasoning[:2000],
                project_categories=", ".join(PROJECT_CATEGORIES),
                global_categories=", ".join(GLOBAL_CATEGORIES),
            )
            collected: list[str] = []
            for chunk in llm.chat_stream(
                [{"role": "user", "content": prompt}], max_tokens=2048,
            ):
                if chunk.content_delta:
                    collected.append(chunk.content_delta)
                if chunk.finish_reason in ("stop", "length"):
                    break
            content = "".join(collected).strip()
        finally:
            llm.enable_thinking = original_thinking

        update = _parse_summary_json(content)
        if not update:
            return

        # 项目记忆按类别合并
        proj_items = _clean_items(update.get("project_memory_update"))
        if proj_items:
            proj = _get_or_create_project(db, task.user_id, repo_url)
            if proj is not None:
                existing = (proj.memory_content or "").strip()
                merged = _merge_structured(
                    existing, proj_items, PROJECT_CATEGORIES, MAX_PROJECT_MEM_STORE,
                )
                if merged != existing:  # 有变化才写(完全去重则跳过)
                    proj.memory_content = merged
                    proj.last_summary_at = datetime.now(timezone.utc)
                    db.commit()
                    logger.info(
                        f"[task={task.id}] 已更新项目记忆 (project={proj.id})"
                    )

        # 全局记忆按类别合并
        global_items = _clean_items(update.get("global_memory_update"))
        if global_items:
            mem = (
                db.query(UserMemory)
                .filter(UserMemory.user_id == task.user_id)
                .first()
            )
            if mem is None:
                mem = UserMemory(user_id=task.user_id, content="")
                db.add(mem)
            existing = (mem.content or "").strip()
            merged = _merge_structured(
                existing, global_items, GLOBAL_CATEGORIES, MAX_GLOBAL_MEM_STORE,
            )
            if merged != existing:
                mem.content = merged
                db.commit()
                logger.info(
                    f"[task={task.id}] 已更新全局记忆 (user={task.user_id})"
                )
    except Exception as e:
        logger.warning(
            f"[task={task.id}] 归纳写入记忆失败(忽略,不影响任务完成): {e}"
        )


# ============================================================
# 辅助函数
# ============================================================


def _clean_items(raw) -> list[dict]:
    """清洗 LLM 输出的条目列表:过滤非法结构,规范化 category。

    - raw 必须是 list,每项是 dict 且含非空 item 字符串。
    - category 不在枚举内(由调用方通过 categories 校验)时,这里只做基本清洗,
      category 的兜底归入在 _merge_structured 中处理。
    """
    if not isinstance(raw, list):
        return []
    cleaned: list[dict] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        item_text = (it.get("item") or "").strip()
        cat = (it.get("category") or "").strip()
        if not item_text or not cat:
            continue
        cleaned.append({"category": cat, "item": item_text})
    return cleaned


def _load_react_summaries_text(db: Session, task_id) -> str:
    """加载本任务 react_agent 各轮总结,拼成文本。

    查 Conversation(role=react_agent, type=thinking),按 round_idx 升序,
    每轮取最后一条 content 作为该轮 summary。
    借鉴 orchestrator._load_react_summaries 的查询逻辑。
    """
    convs = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.role == "react_agent",
            Conversation.type == "thinking",
        )
        .order_by(Conversation.round_idx.asc(), Conversation.created_at.asc())
        .all()
    )
    summaries_by_round: dict[int, str] = {}
    for c in convs:
        if c.content:
            summaries_by_round[c.round_idx] = c.content
    if not summaries_by_round:
        return ""
    parts = []
    for ridx in sorted(summaries_by_round.keys()):
        parts.append(f"### Round {ridx} react_agent summary\n{summaries_by_round[ridx]}")
    return "\n\n".join(parts)


def _load_final_ua_eval(db: Session, task_id) -> str:
    """加载 user_agent 最终评估文本。

    优先查 role=user_agent, type=summary 的最后一条 content(含"最终评估");
    若无 summary,回退到最后一条 type=evaluation 的 reasoning(含 covered/missing/判断)。
    """
    summary = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.role == "user_agent",
            Conversation.type == "summary",
        )
        .order_by(Conversation.created_at.desc())
        .first()
    )
    if summary and summary.content:
        return summary.content

    ev = (
        db.query(Conversation)
        .filter(
            Conversation.task_id == task_id,
            Conversation.role == "user_agent",
            Conversation.type == "evaluation",
        )
        .order_by(
            Conversation.round_idx.desc(), Conversation.created_at.desc()
        )
        .first()
    )
    if ev:
        return ev.reasoning or ev.content or ""
    return ""


def _parse_summary_json(content: str) -> dict | None:
    """解析 LLM 输出的归纳 JSON,容忍 markdown 包裹。

    期望结构:
    {{"project_memory_update": [{{"category":..., "item":...}}, ...],
      "global_memory_update": [...]}}
    解析失败返回 None(调用方跳过写入)。条目清洗由 _clean_items 负责。
    """
    text = content.strip()
    if not text:
        return None
    # 去掉 markdown 代码块包裹
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(
            f"归纳 JSON 解析失败(跳过写入): {e}, raw: {content[:300]}"
        )
    return None


def _get_or_create_project(
    db: Session, user_id, repo_url: str,
) -> Project | None:
    """按归一化 repo_url 查 Project,无则建。

    并发兜底:UNIQUE(user_id, repo_url_normalized) 约束下,并发 INSERT 会抛
    IntegrityError,catch 后 rollback 并回查已建行。
    """
    norm = normalize_repo_url(repo_url)
    if not norm:
        return None

    proj = (
        db.query(Project)
        .filter(
            Project.user_id == user_id,
            Project.repo_url_normalized == norm,
        )
        .first()
    )
    if proj is not None:
        return proj

    new_proj = Project(
        user_id=user_id,
        repo_url_normalized=norm,
        repo_url_raw=repo_url,
        alias=None,
        note=None,
        memory_content="",
    )
    db.add(new_proj)
    try:
        db.commit()
        db.refresh(new_proj)
        return new_proj
    except IntegrityError:
        # 并发:另一线程已建同样 repo_url,回查
        db.rollback()
        return (
            db.query(Project)
            .filter(
                Project.user_id == user_id,
                Project.repo_url_normalized == norm,
            )
            .first()
        )


def _has_category_headers(text: str) -> bool:
    """判断文本是否已是新格式(含 ## 类别 标题行)。"""
    for line in text.splitlines():
        if line.startswith("## "):
            return True
    return False


def _parse_structured(text: str) -> tuple[dict[str, list[str]], str | None]:
    """解析新格式 ## 类别 + - 条目。

    返回 (blocks, legacy_raw):
    - blocks: {category: [item_text, ...]}(不含 Legacy Notes 块)
    - legacy_raw: ## Legacy Notes 块的原始内容(原样保留),无则 None
    """
    blocks: dict[str, list[str]] = {}
    legacy_raw: str | None = None

    current_cat: str | None = None
    legacy_lines: list[str] = []

    for line in text.splitlines():
        if line.startswith("## "):
            # 收尾上一个 Legacy 块
            if current_cat == "Legacy Notes" and legacy_lines:
                legacy_raw = "\n".join(legacy_lines).strip()
            current_cat = line[3:].strip()
            legacy_lines = []
            if current_cat != "Legacy Notes":
                blocks.setdefault(current_cat, [])
        elif current_cat == "Legacy Notes":
            legacy_lines.append(line)
        elif line.startswith("- ") and current_cat and current_cat != "Legacy Notes":
            blocks[current_cat].append(line[2:].strip())
        # 其它行(空行等)忽略

    if current_cat == "Legacy Notes" and legacy_lines:
        legacy_raw = "\n".join(legacy_lines).strip()

    return blocks, legacy_raw


def _merge_structured(
    existing: str,
    new_items: list[dict],
    categories: list[str],
    max_chars: int,
) -> str:
    """按类别合并结构化记忆条目。

    解析 existing:
    - 新格式(含 ## 类别)→ 按类别分块解析 - 条目;## Legacy Notes 块原样保留。
    - 旧格式(无 ## 标题)→ 整体归入 Legacy Notes 块(\n---\n 替换为 \n\n 清理)。

    合并 new_items:按 category 归入对应块(非法 category 兜底归入 Lessons Learned),
    同块内相同 item 精确去重。按固定类别顺序重写输出,Legacy Notes 块放最末。

    超长处理:先删 Legacy Notes 块;若仍超长,尾部截断并加标记。
    """
    existing_stripped = (existing or "").strip()

    blocks: dict[str, list[str]] = {}
    legacy_raw: str | None = None

    if existing_stripped:
        if _has_category_headers(existing_stripped):
            blocks, legacy_raw = _parse_structured(existing_stripped)
        else:
            # 旧格式:整体归入 Legacy,清理 \n---\n 分隔符
            legacy_raw = existing_stripped.replace("\n---\n", "\n\n").strip()

    # 合并新条目(去重 + category 兜底)
    for it in new_items:
        cat = it["category"]
        text = it["item"]
        if cat not in categories:
            cat = "Lessons Learned"
        blocks.setdefault(cat, [])
        if text not in blocks[cat]:
            blocks[cat].append(text)

    # 按固定类别顺序重写
    parts: list[str] = []
    for cat in categories:
        items = blocks.get(cat)
        if items:
            parts.append(f"## {cat}\n" + "\n".join(f"- {it}" for it in items))
    # Legacy 块放最末
    if legacy_raw:
        parts.append(f"## Legacy Notes\n{legacy_raw}")

    merged = "\n\n".join(parts)
    if len(merged) <= max_chars:
        return merged

    # 超长:先删 Legacy 块重试
    if legacy_raw:
        parts_no_legacy = [p for p in parts if not p.startswith("## Legacy Notes")]
        merged_no_legacy = "\n\n".join(parts_no_legacy)
        if len(merged_no_legacy) <= max_chars:
            return merged_no_legacy
        merged = merged_no_legacy

    # 仍超长:尾部截断(新内容更重要,保留尾部)
    return "[...truncated...]\n" + merged[-(max_chars - 30):]
