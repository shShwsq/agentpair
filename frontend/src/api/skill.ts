/**
 * Skill API 模块
 *
 * 对应后端 app/routers/skills.py 的端点。
 * 创建任务时拉取所有可用 skill 供用户多选(allowed_skills)。
 */
import client from './client'

/** Skill 概要(后端 SkillSummaryResponse) */
export interface SkillSummary {
  /** skill 唯一标识(来自 SKILL.md frontmatter name) */
  name: string
  /** 简短说明(给用户选择时看) */
  description: string
  /** 所属场景 id(磁盘目录名,前端按全局展示,不按场景分组) */
  scenario_id: string
}

/**
 * 列出所有 skill(跨所有场景)
 *
 * 用于创建任务时的 skill 多选 UI。后端 GET /skills 返回所有已注册 skill。
 */
export function getSkills(): Promise<SkillSummary[]> {
  return client.get('/skills').then((r) => r.data)
}
