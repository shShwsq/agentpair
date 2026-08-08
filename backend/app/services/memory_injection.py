"""记忆注入服务:把 UserPreference / UserMemory / Project.memory_content
拼成 prompt 段,供 user_agent / react_agent 注入 system_prompt。

无状态函数,每次调用现查 DB。token 受控(各段上限 2000 字符,超出尾部截断)。

注入策略:
- user_agent:用户偏好 + 全局长期记忆(影响评判标准与 checklist 生成)
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
    return text[:max_chars] + "\n[...已截断...]"


def build_user_agent_memory_section(db: Session, user_id) -> str:
    """构造 user_agent 的"用户偏好 + 全局长期记忆"段。

    user_id 为 None(匿名任务)或无任何配置 → 返回空串(不注入)。
    注入到 user_agent system prompt 末尾,影响评判标准与 checklist 生成。
    """
    if user_id is None:
        return ""

    parts: list[str] = []

    # 用户偏好
    pref = (
        db.query(UserPreference)
        .filter(UserPreference.user_id == user_id)
        .first()
    )
    if pref:
        pref_lines: list[str] = []
        p = pref.preferences or {}
        if p.get("output_language"):
            pref_lines.append(f"- 输出语言: {p['output_language']}")
        if p.get("focus_areas"):
            areas = p["focus_areas"]
            if isinstance(areas, list) and areas:
                pref_lines.append(
                    f"- 重点关注领域: {', '.join(str(a) for a in areas)}"
                )
        if p.get("style"):
            pref_lines.append(f"- 评判风格: {p['style']}")
        if pref.custom_prompt and pref.custom_prompt.strip():
            pref_lines.append(f"- 自定义补充: {pref.custom_prompt.strip()}")
        if pref_lines:
            parts.append(
                "## 用户偏好(请在生成 checklist 和评估时遵循)\n"
                + _truncate("\n".join(pref_lines), MAX_PREF_CHARS)
            )

    # 全局长期记忆(内容已含 ## 类别 + - 条目结构,用引导语作外层避免层级冲突)
    mem = db.query(UserMemory).filter(UserMemory.user_id == user_id).first()
    if mem and mem.content and mem.content.strip():
        parts.append(
            "以下是跨任务积累的长期记忆,按类别组织(在生成 checklist 和评估时遵循):\n"
            + _truncate(mem.content.strip(), MAX_GLOBAL_MEM_CHARS)
        )

    if not parts:
        return ""
    return "\n\n".join(parts)


def build_react_agent_memory_section(
    db: Session, user_id, repo_url: str | None,
) -> str:
    """构造 react_agent 的"分项目记忆"段。

    user_id 为 None / repo_url 为空 / 无对应 Project / memory_content 为空 → 返回空串。
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
    if not proj or not proj.memory_content or not proj.memory_content.strip():
        return ""

    header = (
        "以下是你对该项目的已知问题与历史记忆,按类别组织,"
        "优先检查 Hard Constraints 和 Known Issues 方向:"
    )
    if proj.alias:
        header += f"\n项目别名: {proj.alias}"
    return header + "\n" + _truncate(
        proj.memory_content.strip(), MAX_PROJECT_MEM_CHARS
    )
