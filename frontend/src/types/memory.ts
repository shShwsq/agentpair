/**
 * 长期记忆管理相关类型定义
 *
 * 对应后端 /memory 系列 API(app/routers/memory.py):
 * - User Profile (1:1,结构化字段 + 自由文本):影响 user_agent 评判标准与 checklist 生成
 * - 全局长期记忆(1:1,自由文本):跨项目通用经验,注入 user_agent
 * - 分项目记忆(1:N,按 repo_url 聚合):注入 react_agent,影响审计方向
 *
 * 三类记忆均可由用户手动编辑;分项目记忆还会在任务完成时由 agent 自动归纳写入。
 */

/**
 * User Profile (1:1)
 *
 * user_profile 为自由文本 Markdown(用户在记忆管理页编辑),注入 user_agent,
 * 影响评判标准与 checklist 生成(≤ 2000 字符)。
 */
export interface UserPreferenceOut {
  /** 自由文本 Markdown(≤ 2000 字符) */
  user_profile: string
  /** 最后更新时间(ISO 字符串,未配置时为 null) */
  updated_at: string | null
}

/** 保存 User Profile 请求(PUT /memory/preferences body) */
export interface SaveUserPreferenceRequest {
  user_profile: string
}

/**
 * 全局长期记忆(1:1)
 *
 * 跨项目通用经验,注入 user_agent。自由文本,≤ 20000 字符。
 * 后端写入时还有合并截断(10000)+ 注入截断(2000)两道防线。
 */
export interface UserMemoryOut {
  content: string
  /** 最后更新时间(ISO 字符串,未配置时为 null) */
  updated_at: string | null
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
