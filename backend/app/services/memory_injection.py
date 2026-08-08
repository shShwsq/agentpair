"""记忆注入服务:把 UserPreference / UserMemory / Project.memory_content
拼成 prompt 段,供 user_agent / react_agent 注入 system_prompt。

无状态函数,每次调用现查 DB。token 受控(各段上限 2000 字符,超出尾部截断)。

注入策略:
- user_agent:User Profile + 全局长期记忆(影响评判标准与 checklist 生成)
- react_agent:分项目记忆(影响审计方向,优先检查已知问题)

user_id 为 None(匿名任务)或无配置 → 返回空串(不注入),保证匿名任务不受影响。
"""
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.user_memory import UserMemory
from app.models.user_preference import UserPreference
from app.services.repo_url import normalize_repo_url

# 各段字符上限(超出尾部截断 + 加截断标记)
MAX_PREF_CHARS = 2000
MAX_GLOBAL_MEM_CHARS = 2000
MAX_PROJECT_MEM_CHARS = 2000


def _truncate(text: str, max_chars: int) -> str:
    """超长截断,尾部加截断标记"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[...truncated...]"


def build_user_agent_memory_section(
    db: Session, user_id, repo_url: str | None = None,
) -> str:
    """构造 user_agent 的"User Profile + 全局长期记忆 + 项目记忆精简版"段。

    user_id 为 None(匿名任务)或无任何配置 → 返回空串(不注入)。
    repo_url 非空时追加当前项目的记忆精简版(影响 checklist 生成与评估覆盖度)。
    注入到 user_agent system prompt 末尾,影响评判标准与 checklist 生成。

    user_agent 不在沙箱,无法 read_file 查阅完整记忆,故只注入精简版
    (memory_summary;为空回退 memory_content 截断)。
    """
    if user_id is None:
        return ""

    parts: list[str] = []

    # User Profile
    pref = (
        db.query(UserPreference)
        .filter(UserPreference.user_id == user_id)
        .first()
    )
    if pref:
        pref_lines: list[str] = []
        p = pref.preferences or {}
        if p.get("output_language"):
            pref_lines.append(f"- Output language: {p['output_language']}")
        if p.get("focus_areas"):
            areas = p["focus_areas"]
            if isinstance(areas, list) and areas:
                pref_lines.append(
                    f"- Focus areas: {', '.join(str(a) for a in areas)}"
                )
        if p.get("style"):
            pref_lines.append(f"- Evaluation style: {p['style']}")
        if pref.custom_prompt and pref.custom_prompt.strip():
            pref_lines.append(f"- Additional notes: {pref.custom_prompt.strip()}")
        if pref_lines:
            parts.append(
                "## User Profile (follow when generating checklist and evaluation)\n"
                + _truncate("\n".join(pref_lines), MAX_PREF_CHARS)
            )

    # 全局长期记忆(内容已含 ## 类别 + - 条目结构,用引导语作外层避免层级冲突)
    mem = db.query(UserMemory).filter(UserMemory.user_id == user_id).first()
    if mem and mem.content and mem.content.strip():
        parts.append(
            "The following is long-term memory accumulated across tasks, "
            "organized by category (follow when generating checklist and evaluation):\n"
            + _truncate(mem.content.strip(), MAX_GLOBAL_MEM_CHARS)
        )

    # 项目记忆精简版(影响 checklist 生成与评估覆盖度;user_agent 不在沙箱,只注入精简版)
    if repo_url:
        norm = normalize_repo_url(repo_url)
        if norm:
            proj = (
                db.query(Project)
                .filter(
                    Project.user_id == user_id,
                    Project.repo_url_normalized == norm,
                )
                .first()
            )
            if proj:
                summary = (proj.memory_summary or "").strip()
                if not summary:  # 兼容未生成 summary 的旧数据
                    summary = _truncate(
                        (proj.memory_content or "").strip(), MAX_PROJECT_MEM_CHARS
                    )
                if summary:
                    parts.append(
                        "The following is a summary of known issues and historical "
                        "memory for this project, organized by category "
                        "(reference when generating checklist and evaluation coverage):\n"
                        + summary
                    )

    if not parts:
        return ""
    return "\n\n".join(parts)


def build_react_agent_memory_section(
    db: Session, user_id, repo_url: str | None,
) -> str:
    """构造 react_agent 的"分项目记忆"段。

    优先注入精简版 memory_summary(LLM 生成,≤注入上限);为空时回退 memory_content 截断
    (兼容未生成 summary 的旧数据)。末尾附完整记忆文件路径提示,引导 agent 用 read_file
    查阅突破字数限制的完整记忆。

    user_id 为 None / repo_url 为空 / 无对应 Project / 记忆为空 → 返回空串。
    注入到 react_agent system prompt 末尾,影响审计方向(优先检查已知问题)。
    """
    if user_id is None or not repo_url:
        return ""

    norm = normalize_repo_url(repo_url)
    if not norm:
        return ""

    proj = (
        db.query(Project)
        .filter(
            Project.user_id == user_id,
            Project.repo_url_normalized == norm,
        )
        .first()
    )
    if not proj:
        return ""

    # 优先用精简版(已 ≤ MAX_PROJECT_MEM_CHARS,无需截断);为空回退完整内容截断
    summary = (proj.memory_summary or "").strip()
    if summary:
        memory_text = summary
    else:
        memory_text = _truncate(
            (proj.memory_content or "").strip(), MAX_PROJECT_MEM_CHARS
        )
    if not memory_text:
        return ""

    header = (
        "The following is your known issues and historical memory for this project, "
        "organized by category. Prioritize checking Hard Constraints and Known Issues:"
    )
    if proj.alias:
        header += f"\nProject alias: {proj.alias}"
    # 完整记忆已写入沙箱文件,提示 agent 可 read_file 查阅突破字数限制
    memory_text += (
        "\n\nFull memory available via read_file "
        "/home/user/.agent_memory/project_memory.md"
    )
    return header + "\n" + memory_text


def build_global_memory_section(db: Session, user_id) -> str:
    """构造全局长期记忆段(注入 react_agent / CLI 执行侧)。

    跨项目通用经验(Hard Constraints / Tech Stack / Lessons Learned 等),
    影响执行方式。执行侧(react_agent / CLI)在沙箱里干活,这类"怎么做"的知识
    直接影响执行正确性,故注入执行侧而非仅 user_agent。

    user_id 为 None(匿名任务)或无全局记忆 → 返回空串(不注入)。
    截断到 MAX_GLOBAL_MEM_CHARS(与 user_agent 一致),不写沙箱文件
    (全局记忆通常不超长,且无需跨字数限制查阅)。
    """
    if user_id is None:
        return ""
    mem = db.query(UserMemory).filter(UserMemory.user_id == user_id).first()
    if not mem or not mem.content or not mem.content.strip():
        return ""
    return (
        "The following is general experience accumulated across tasks, "
        "organized by category (follow during execution):\n"
        + _truncate(mem.content.strip(), MAX_GLOBAL_MEM_CHARS)
    )
