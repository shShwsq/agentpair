/**
 * Skill API 模块
 *
 * 对应后端 app/routers/skills.py 的端点。
 * - 创建任务时拉取所有可见 skill 供用户多选(allowed_skills)
 * - 技能管理页:上传 zip / 删除 / 详情
 */
import client from './client'

/** Skill 概要(后端 SkillSummaryResponse) */
export interface SkillSummary {
  /** skill 唯一标识(来自 SKILL.md frontmatter name) */
  name: string
  /** 简短说明(给用户选择时看) */
  description: string
  /** 所属场景 id(内置=目录名,用户上传=user_<uuid>) */
  scenario_id: string
  /** 当前用户是否可管理(仅自己上传的 skill 为 true) */
  owned: boolean
}

/** Skill 详情(后端 SkillResponse) */
export interface SkillDetail extends SkillSummary {
  /** SKILL.md 正文(frontmatter 之后的指令) */
  body: string
  /** SKILL.md 磁盘路径 */
  source_path: string
}

/** zip 上传结果(后端 SkillUploadResponse) */
export interface SkillUploadResult {
  skill: SkillDetail
  /** 是否覆盖了已存在的同名 skill */
  replaced: boolean
}

/**
 * 列出当前用户可见的 skill(内置全局共享 + 自己上传的)
 *
 * 用于创建任务时的 skill 多选 UI 与技能管理页列表。
 * 后端 GET /skills 返回已注册且当前用户可见的 skill。
 */
export function getSkills(): Promise<SkillSummary[]> {
  return client.get('/skills').then((r) => r.data)
}

/** 查看 skill 详情(含 body) */
export function getSkillDetail(
  scenarioId: string,
  skillName: string,
): Promise<SkillDetail> {
  return client
    .get(`/skills/${encodeURIComponent(scenarioId)}/${encodeURIComponent(skillName)}`)
    .then((r) => r.data)
}

/**
 * 上传 zip 格式的 skill
 *
 * @param file .zip 文件(内含 SKILL.md,支持 <skill_name>/SKILL.md 或根目录 SKILL.md)
 * @param force 与自己的同名 skill 冲突时,true=覆盖
 */
export function uploadSkill(file: File, force: boolean): Promise<SkillUploadResult> {
  const form = new FormData()
  form.append('file', file)
  form.append('force', String(force))
  // 覆盖实例默认的 application/json(否则 axios 不会自动切 multipart),
  // 置 undefined 让 axios 为 FormData 自动生成 boundary
  return client.post('/skills/upload', form, {
    headers: { 'Content-Type': undefined },
  }).then((r) => r.data)
}

/** 删除自己上传的 skill(内置/他人 skill 后端会拒绝) */
export function deleteSkill(
  scenarioId: string,
  skillName: string,
): Promise<void> {
  return client.delete(
    `/skills/${encodeURIComponent(scenarioId)}/${encodeURIComponent(skillName)}`,
  )
}
