/**
 * 任务相关类型定义
 *
 * 与后端 app/schemas/task.py 一一对应
 */

/** 任务状态(后端 TaskStatus 枚举的字符串值) */
export type TaskStatus = 'pending' | 'running' | 'completed' | 'failed'

/** 场景信息(后端 ScenarioInfo) */
export interface Scenario {
  id: string
  name: string
}

/** 提交任务请求(后端 TaskCreateRequest) */
export interface TaskCreateRequest {
  scenario: string
  user_input?: string
  params?: Record<string, unknown>
  /** 兼容字段:传 repo_url 时自动生成 user_input */
  repo_url?: string
  branch?: string
  scope?: string
}

/** 提交任务响应(后端 TaskCreateResponse) */
export interface TaskCreateResponse {
  id: string
  status: TaskStatus
}

/** 任务结果项(后端 ResultResponse) */
export interface TaskResult {
  id: string
  round_idx: number
  title: string
  content: string
  /** 场景专用信息(安全场景:cwe/severity/file_path/line_range) */
  metadata_?: Record<string, unknown> | null
}

/** 对话记录(后端 ConversationResponse) */
export interface Conversation {
  id: string
  round_idx: number
  /** 角色:user / user_agent / react_agent */
  role: string
  /** 消息类型:evaluation/followup/thinking/tool_call/tool_result/submit/summary/error */
  type: string
  content: string
  created_at: string
}

/** 任务详情(后端 TaskResponse) */
export interface TaskDetail {
  id: string
  scenario: string
  user_input: string
  params?: Record<string, unknown> | null
  status: TaskStatus
  current_stage: string | null
  error_message: string | null
  created_at: string
  completed_at: string | null
  results: TaskResult[]
  conversations: Conversation[]
}
