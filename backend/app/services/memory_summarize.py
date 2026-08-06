"""任务完成后自动归纳写入长期记忆(钩子)。

由 orchestrator 在任务成功完成段调用。失败兜底:任何异常都不影响任务完成状态。
借鉴 react_agent._llm_compress_history 的"关 thinking 加速 + 截断 + 降级"范式。

写入策略:
- 项目记忆:按 repo_url 归一化找到/创建 Project,把 LLM 归纳的 project_memory_update
  增量合并到 memory_content 末尾(用 \n---\n 分隔,超长保留尾部)
- 全局记忆:把 LLM 归纳的 global_memory_update 增量合并到 UserMemory.content

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

# 记忆合并后的存储上限(超长保留尾部,新内容更重要)
MAX_PROJECT_MEM_STORE = 8000
MAX_GLOBAL_MEM_STORE = 10000

# 归纳 prompt(要求输出严格 JSON)
_SUMMARIZE_PROMPT = """请基于以下任务执行记录,归纳出对未来同类任务有指导意义的内容。

[项目仓库]
{repo_url}

[用户意图]
{user_intent}

[react_agent 各轮总结]
{react_summaries}

[user_agent 最终评估]
{ua_reasoning}

请输出严格 JSON(不要 markdown 代码块包裹):
{{
  "project_memory_update": "针对该项目的已知问题/审计方向/历史发现摘要(增量,与已有记忆合并;无新增则输出空串)",
  "global_memory_update": "跨项目通用的经验/约定(如用户偏好特定输出格式、通用审计方法论);无新增则输出空串"
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

        # 项目记忆增量合并
        proj_update = (update.get("project_memory_update") or "").strip()
        if proj_update:
            proj = _get_or_create_project(db, task.user_id, repo_url)
            if proj is not None:
                existing = (proj.memory_content or "").strip()
                if proj_update not in existing:  # 简单去重(完全包含则跳过)
                    proj.memory_content = _merge_with_limit(
                        existing, proj_update, MAX_PROJECT_MEM_STORE,
                    )
                    proj.last_summary_at = datetime.now(timezone.utc)
                    db.commit()
                    logger.info(
                        f"[task={task.id}] 已更新项目记忆 (project={proj.id})"
                    )

        # 全局记忆增量合并
        global_update = (update.get("global_memory_update") or "").strip()
        if global_update:
            mem = (
                db.query(UserMemory)
                .filter(UserMemory.user_id == task.user_id)
                .first()
            )
            if mem is None:
                mem = UserMemory(user_id=task.user_id, content="")
                db.add(mem)
            existing = (mem.content or "").strip()
            if global_update not in existing:
                mem.content = _merge_with_limit(
                    existing, global_update, MAX_GLOBAL_MEM_STORE,
                )
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
        parts.append(f"### 第 {ridx} 轮 react_agent 总结\n{summaries_by_round[ridx]}")
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

    借鉴 user_agent._parse_json_response 的容忍逻辑。
    解析失败返回 None(调用方跳过写入)。
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


def _merge_with_limit(existing: str, new_content: str, max_chars: int) -> str:
    """增量合并:新内容追加到旧内容末尾,用分隔符隔开,超长保留尾部。

    语义:新内容更重要(反映最新任务),所以超长时截断头部(旧内容),保留尾部(新内容)。
    """
    if not existing:
        return new_content[:max_chars]
    merged = f"{existing}\n---\n{new_content}"
    if len(merged) <= max_chars:
        return merged
    # 超长:保留尾部 max_chars 字符,头部加截断标记
    return "[...早期记忆已截断...]\n" + merged[-(max_chars - 30):]
