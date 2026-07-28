"""SKILL 工具入口(阶段 5)

暴露给 react_agent 的两个工具:
- list_skills:列出当前场景的所有 skill(name + description)
- skill:获取某个 skill 的 SKILL.md body 内容,让 LLM 按指令自行执行后续工具

设计要点:
- skill 工具不直接执行任何审计操作,只返回 SKILL.md 文本
- LLM 看完 SKILL.md 后,自己决定调用 search_code / read_file / run_in_sandbox 等底层工具
- 这符合 Trae / Claude Code 的 skill 规范:Markdown 指令驱动,而非程序化 steps
- scenario 上下文从 react_agent 注入(set_current_scenario)
"""
import logging

from app.skills import loader as skill_loader

logger = logging.getLogger(__name__)


# 当前 scenario(由 react_agent 在每轮开始时注入)
_CURRENT_SCENARIO: str = "code_security_audit"


def set_current_scenario(scenario_id: str) -> None:
    """react_agent 调用工具前,注入当前任务的场景"""
    global _CURRENT_SCENARIO
    _CURRENT_SCENARIO = scenario_id


# ============================================================
# 工具 1:list_skills
# ============================================================


def list_available_skills(task_id: str = "") -> dict:
    """列出当前场景的所有可用 skill

    返回:{
        "scenario": "code_security_audit",
        "skills": [
            {"name": "check_sql_injection", "description": "..."},
            ...
        ],
        "total": int
    }
    """
    skills = skill_loader.REGISTRY.list_for_scenario(_CURRENT_SCENARIO)
    return {
        "scenario": _CURRENT_SCENARIO,
        "skills": [
            {"name": s.name, "description": s.description}
            for s in skills
        ],
        "total": len(skills),
    }


# ============================================================
# 工具 2:skill(获取 SKILL.md 内容)
# ============================================================


def run_skill(skill_name: str, task_id: str = "") -> dict:
    """获取指定 skill 的 SKILL.md 指令内容

    参数:
        skill_name: skill 名称(先调 list_skills 查可用名称)

    返回:{
        "skill_name": "check_sql_injection",
        "description": "...",
        "instructions": "<SKILL.md body 全文>"
    }

    LLM 拿到 instructions 后,按其指引自行调用底层工具执行
    """
    skill = skill_loader.REGISTRY.get(_CURRENT_SCENARIO, skill_name)
    if not skill:
        available = [s.name for s in skill_loader.REGISTRY.list_for_scenario(_CURRENT_SCENARIO)]
        return {
            "error": f"未知 skill: {skill_name}",
            "scenario": _CURRENT_SCENARIO,
            "available_skills": available,
            "hint": "先调用 list_skills 查可用 skill 名称",
        }

    return {
        "skill_name": skill.name,
        "description": skill.description,
        "instructions": skill.body,
    }
