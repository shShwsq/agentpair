/**
 * 长期记忆管理相关类型定义
 *
 * 对应后端 /memory 系列 API(app/routers/memory.py):
 * - 用户偏好(1:1,结构化字段 + 自由文本):影响 user_agent 评判标准与 checklist 生成
 * - 全局长期记忆(1:1,自由文本):跨项目通用经验,注入 user_agent
 * - 分项目记忆(1:N,按 repo_url 聚合):注入 react_agent,影响审计方向
 *
 * 三类记忆均可由用户手动编辑;分项目记忆还会在任务完成时由 agent 自动归纳写入。
 */

/**
 * 用户偏好(1:1)
 *
 * preferences 为结构化偏好,后端以 JSONB 存储,约定字段:
 * - output_language?: 'zh' | 'en' | ...        输出语言
 * - focus_areas?: string[]                       重点关注领域,如 ['security','performance']
 * - style?: string                                评判风格,如 'concise' | 'strict'
 *
 * custom_prompt 为自由文本兜底,用户大段自定义偏好/评判标准补充(≤ 8000 字符)。
 */
export interface UserPreferenceOut {
  /** 结构化偏好(JSONB,字段约定见上) */
  preferences: Record<string, unknown>
  /** 自由文本兜底(≤ 8000 字符) */
  custom_prompt: string
}

/** 保存用户偏好请求(PUT /memory/preferences body) */
export interface SaveUserPreferenceRequest {
  preferences: Record<string, unknown>
  custom_prompt: string
}

/**
 * 全局长期记忆(1:1)
 *
 * 跨项目通用经验,注入 user_agent。自由文本,≤ 20000 字符。
 * 后端写入时还有合并截断(10000)+ 注入截断(2000)两道防线。
 */
export interface UserMemoryOut {
  content: string
}

/** 保存全局长期记忆请求(PUT /memory/global body) */
export interface SaveUserMemoryRequest {
  content: string
}

/**
 * 分项目记忆(1:N,按 repo_url 聚合)
 *
 * 项目由 orchestrator 在任务完成时自动归纳创建(_get_or_create_project),
 * 前端不提供"新建项目"入口,只能编辑/删除已由 agent 自动归纳产生的项目记录。
 */
export interface ProjectOut {
  /** 项目记忆 UUID(字符串) */
  id: string
  /** 归一化后的 repo_url,作为项目身份(不可改) */
  repo_url_normalized: string
  /** 原始 repo_url(展示用) */
  repo_url_raw: string
  /** 项目别名(用户可改,如 'AgentPair 主仓库') */
  alias: string | null
  /** 项目备注(用户可改,自由文本) */
  note: string | null
  /** 分项目记忆正文(注入 react_agent,≤ 20000 字符) */
  memory_content: string
  /** 上次自动归纳时间(ISO 字符串,可空) */
  last_summary_at: string | null
  created_at: string | null
  updated_at: string | null
}

/**
 * 保存分项目记忆请求(PUT /memory/projects/{id} body)
 *
 * 仅允许编辑 alias/note/memory_content(不修改 repo_url 与 last_summary_at)。
 */
export interface SaveProjectRequest {
  alias: string | null
  note: string | null
  memory_content: string
}

/** 分项目记忆列表响应(GET /memory/projects、DELETE 后响应) */
export interface ProjectListResponse {
  projects: ProjectOut[]
}
