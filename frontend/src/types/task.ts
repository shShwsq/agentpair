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
  /**
   * 思考链(仅 type=thinking 有,模型 reasoning_content 输出)
   *
   * 流式期间通过 thinking_delta 实时推送,完成后落库到此字段。
   * 刷新页面后从 GET /tasks/{id} 拿到,用于还原流式卡片(只读模式,不再实时打字)。
   */
  reasoning?: string | null
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

// ============================================================
// SSE 事件类型(后端 event_bus.py publish 的事件格式)
// ============================================================

/** SSE 事件类型 */
export type SSEEventType =
  | 'connected'
  | 'conversation'
  | 'status'
  | 'thinking_delta'
  | 'done'
  | 'error'

/** SSE 事件通用结构 */
export interface SSEEvent {
  type: SSEEventType
  task_id: string
  data: Record<string, unknown>
  timestamp: string
}

/** connected 事件 data */
export interface ConnectedData {
  status: TaskStatus
  current_stage: string | null
}

/** conversation 事件 data(与 Conversation 字段一致) */
export interface ConversationEventData {
  id: string
  round_idx: number
  role: string
  type: string
  content: string
  /** 完整评估/思考链(如 user_agent evaluation 的覆盖情况+判断),可折叠回看 */
  reasoning?: string | null
  created_at: string | null
}

/** status 事件 data */
export interface StatusEventData {
  status: TaskStatus
  current_stage: string | null
}

/**
 * thinking_delta 事件 data
 *
 * LLM 流式输出的 token 增量。前端按 conv_id 累积,实现打字机效果。
 * 一个 conv_id 对应一次 LLM 调用:
 *   1. phase='start' → 创建占位项,显示"正在生成..."
 *   2. phase='reasoning' → 累积到思考链区域
 *   3. phase='content' → 累积到正式回答区域
 *   4. phase='end' → 标记完成
 *
 * 流结束后,完整内容会通过 conversation 事件再推一次(用于落库 + 迟到订阅者补播)
 */
export interface ThinkingDeltaEventData {
  /** 该次 LLM 调用的临时 ID(前端按此 key 累积) */
  conv_id: string
  /** 协作轮次 */
  round_idx: number
  /** 角色:react_agent / user_agent */
  role: 'react_agent' | 'user_agent'
  /** 阶段:start / reasoning / content / end / error */
  phase: 'start' | 'reasoning' | 'content' | 'end' | 'error'
  /** 增量文本(start/end 时为空字符串) */
  delta: string
  /** 迭代序号(仅 react_agent 有,标识 ReAct 循环的第几次 LLM 调用) */
  iteration?: number
}

/** done/error 事件 data */
export interface DoneEventData {
  status: TaskStatus
  error_message?: string
}
