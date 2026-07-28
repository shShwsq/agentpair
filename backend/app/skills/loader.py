"""SKILL 加载器

扫描磁盘上的 SKILL.md 文件,解析 frontmatter,注册到 SkillRegistry。

目录结构(与 scenarios 模块对齐):
    <skills_root>/
      <scenario_id>/
        <skill_name>/
          SKILL.md          # 必需
          <可选附加文件>      # 规则文件、示例等,skill body 可引用

进程级缓存:启动时扫描一次,管理员后台改完调 reload() 重新扫描。
"""
import logging
import re
from pathlib import Path

import yaml

from app.skills.schema import ParsedSkill, SkillRegistry

logger = logging.getLogger(__name__)


# SKILL 根目录(backend/skills/)
DEFAULT_SKILLS_ROOT = Path(__file__).parent.parent.parent / "skills"


# 进程级注册表(模块单例)
REGISTRY = SkillRegistry()


# ============================================================
# frontmatter 解析
# ============================================================

# SKILL.md 必须以 --- 开头的 frontmatter 块开始
_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z",
    re.DOTALL,
)


def parse_skill_md(path: Path, scenario_id: str) -> ParsedSkill:
    """解析一个 SKILL.md 文件

    参数:
        path: SKILL.md 绝对路径
        scenario_id: 所属场景(从目录路径推断)

    抛出:
        ValueError: frontmatter 缺失或必填字段不完整
    """
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(
            f"SKILL.md 缺少 frontmatter 或格式错误(应以 --- 开头): {path}"
        )

    frontmatter_text, body = m.group(1), m.group(2)
    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"SKILL.md frontmatter YAML 解析失败: {path}: {e}") from e

    name = frontmatter.get("name")
    description = frontmatter.get("description")

    if not name or not description:
        raise ValueError(
            f"SKILL.md frontmatter 缺少必填字段 name/description: {path}"
        )

    if not isinstance(name, str) or not isinstance(description, str):
        raise ValueError(
            f"SKILL.md frontmatter name/description 必须是字符串: {path}"
        )

    return ParsedSkill(
        name=name.strip(),
        description=description.strip(),
        scenario_id=scenario_id,
        skill_dir=path.parent,
        body=body.rstrip() + "\n",
        source_path=path,
    )


# ============================================================
# 扫描与注册
# ============================================================


def discover_skills(skills_root: Path | None = None) -> SkillRegistry:
    """扫描 skills_root 下所有 SKILL.md,返回新的 SkillRegistry

    扫描规则:
        <skills_root>/<scenario_id>/<skill_name>/SKILL.md

    - scenario_id 取第二层目录名
    - skill_name 取 frontmatter.name(以文件内容为准,不依赖目录名)
    - 单个 SKILL.md 解析失败不阻断其他 skill,只记录 warning
    """
    root = skills_root or DEFAULT_SKILLS_ROOT
    registry = SkillRegistry()

    if not root.is_dir():
        logger.warning(f"skills 目录不存在: {root},返回空注册表")
        return registry

    # 遍历 <root>/<scenario_id>/<skill_name>/SKILL.md
    for scenario_dir in sorted(root.iterdir()):
        if not scenario_dir.is_dir() or scenario_dir.name.startswith("."):
            continue
        scenario_id = scenario_dir.name

        for skill_dir in sorted(scenario_dir.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue

            try:
                skill = parse_skill_md(skill_md, scenario_id)
            except Exception as e:
                logger.warning(f"解析 SKILL.md 失败,跳过: {skill_md}: {e}")
                continue

            # 同一 scenario 下重名,后注册的覆盖(以磁盘扫描顺序为准)
            if registry.get(scenario_id, skill.name):
                logger.warning(
                    f"skill 重名覆盖: scenario={scenario_id} name={skill.name} "
                    f"(新文件: {skill_md})"
                )
            registry.register(skill)
            logger.info(f"已加载 skill: {scenario_id}/{skill.name} ({skill_md})")

    return registry


def reload_registry(skills_root: Path | None = None) -> SkillRegistry:
    """重新扫描磁盘,刷新进程级注册表

    管理员后台改完 SKILL.md 后调用此函数
    """
    global REGISTRY
    REGISTRY = discover_skills(skills_root)
    return REGISTRY


# ============================================================
# 查询接口(供 skill_tool / routers/skills 使用)
# ============================================================


def get_skill(scenario_id: str, skill_name: str) -> ParsedSkill | None:
    """按 (scenario, name) 查找 skill"""
    return REGISTRY.get(scenario_id, skill_name)


def list_skills(scenario_id: str) -> list[ParsedSkill]:
    """列出某场景的所有 skill"""
    return REGISTRY.list_for_scenario(scenario_id)
