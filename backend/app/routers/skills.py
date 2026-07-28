"""SKILL 管理后台 API(阶段 5)

阶段 5 暂不鉴权,阶段 6 加 JWT 后限制为管理员。
路径设计:
    GET    /skills                                   列出所有场景的 skill
    GET    /skills/{scenario_id}                     列出某场景的 skill
    GET    /skills/{scenario_id}/{skill_name}         查看 skill 详情(含 body)
    POST   /skills/{scenario_id}/{skill_name}        创建/更新 skill(body 传 SKILL.md 全文)
    DELETE /skills/{scenario_id}/{skill_name}        删除 skill(删磁盘文件)
    POST   /skills/reload                             重新扫描磁盘(刷新注册表)
"""
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.skills.loader import (
    DEFAULT_SKILLS_ROOT,
    reload_registry,
)
from app.skills import loader as skill_loader
from app.skills.schema import ParsedSkill

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])


# ============================================================
# 请求 / 响应模型
# ============================================================


class SkillCreateRequest(BaseModel):
    """创建/更新 skill 的请求

    body 直接传 SKILL.md 全文(frontmatter + 正文)。
    若 name/description 与 frontmatter 不一致以 frontmatter 为准。
    """

    content: str


class SkillResponse(BaseModel):
    """skill 详情响应"""

    name: str
    description: str
    scenario_id: str
    body: str
    source_path: str

    model_config = {"from_attributes": True}


class SkillSummaryResponse(BaseModel):
    """skill 概要响应(列表用)"""

    name: str
    description: str
    scenario_id: str


# ============================================================
# 辅助
# ============================================================


def _skill_path(scenario_id: str, skill_name: str) -> Path:
    """构造 SKILL.md 路径,校验合法性"""
    # 防止路径穿越:scenario_id 和 skill_name 不能含 .. 或路径分隔符
    if "/" in scenario_id or "\\" in scenario_id or ".." in scenario_id:
        raise HTTPException(status_code=400, detail="非法 scenario_id")
    if "/" in skill_name or "\\" in skill_name or ".." in skill_name:
        raise HTTPException(status_code=400, detail="非法 skill_name")

    return DEFAULT_SKILLS_ROOT / scenario_id / skill_name / "SKILL.md"


def _to_summary(skill: ParsedSkill) -> SkillSummaryResponse:
    return SkillSummaryResponse(
        name=skill.name,
        description=skill.description,
        scenario_id=skill.scenario_id,
    )


def _to_detail(skill: ParsedSkill) -> SkillResponse:
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        scenario_id=skill.scenario_id,
        body=skill.body,
        source_path=str(skill.source_path),
    )


# ============================================================
# 路由
# ============================================================


@router.get("", response_model=list[SkillSummaryResponse])
def list_all_skills() -> list[SkillSummaryResponse]:
    """列出所有场景的所有 skill"""
    result = []
    for scenario_id in skill_loader.REGISTRY.list_scenarios():
        for skill in skill_loader.REGISTRY.list_for_scenario(scenario_id):
            result.append(_to_summary(skill))
    return result


@router.get("/{scenario_id}", response_model=list[SkillSummaryResponse])
def list_scenario_skills(scenario_id: str) -> list[SkillSummaryResponse]:
    """列出某场景的所有 skill"""
    skills = skill_loader.REGISTRY.list_for_scenario(scenario_id)
    return [_to_summary(s) for s in skills]


@router.get("/{scenario_id}/{skill_name}", response_model=SkillResponse)
def get_skill_detail(scenario_id: str, skill_name: str) -> SkillResponse:
    """查看 skill 详情(含 body)"""
    skill = skill_loader.REGISTRY.get(scenario_id, skill_name)
    if not skill:
        raise HTTPException(
            status_code=404,
            detail=f"skill 不存在: {scenario_id}/{skill_name}",
        )
    return _to_detail(skill)


@router.post("/{scenario_id}/{skill_name}", response_model=SkillResponse)
def upsert_skill(
    scenario_id: str, skill_name: str, req: SkillCreateRequest
) -> SkillResponse:
    """创建或更新 skill(直接写 SKILL.md 全文)

    - 目录不存在会自动创建
    - 写完后调用 reload_registry 刷新注册表
    - 若 frontmatter 解析失败返回 400
    """
    skill_md = _skill_path(scenario_id, skill_name)
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(req.content, encoding="utf-8")

    # 立即重新扫描,校验 frontmatter 是否合法
    reload_registry()
    skill = skill_loader.REGISTRY.get(scenario_id, skill_name)
    if not skill:
        # 重载后找不到,说明 frontmatter 解析失败
        # 删掉刚写的文件,避免污染磁盘
        try:
            skill_md.unlink()
        except OSError:
            pass
        raise HTTPException(
            status_code=400,
            detail="SKILL.md frontmatter 解析失败,请检查 name/description 字段",
        )

    logger.info(f"upsert skill: {scenario_id}/{skill_name} ({skill_md})")
    return _to_detail(skill)


@router.delete("/{scenario_id}/{skill_name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_skill(scenario_id: str, skill_name: str) -> None:
    """删除 skill(删整个 skill 目录)"""
    skill = skill_loader.REGISTRY.get(scenario_id, skill_name)
    if not skill:
        raise HTTPException(
            status_code=404,
            detail=f"skill 不存在: {scenario_id}/{skill_name}",
        )

    # 删整个 skill 目录(SKILL.md + 可能的附加资源)
    skill_dir = skill.skill_dir
    import shutil

    shutil.rmtree(skill_dir, ignore_errors=True)
    reload_registry()
    logger.info(f"delete skill: {scenario_id}/{skill_name} ({skill_dir})")


@router.post("/reload", response_model=list[SkillSummaryResponse])
def reload_skills() -> list[SkillSummaryResponse]:
    """重新扫描磁盘,刷新注册表"""
    registry = reload_registry()
    result = []
    for scenario_id in registry.list_scenarios():
        for skill in registry.list_for_scenario(scenario_id):
            result.append(_to_summary(skill))
    return result
