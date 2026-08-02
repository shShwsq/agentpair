/**
 * 任务相关类型定义
 *
 * 与后端 app/schemas/task.py 一一对应
 */

/** 任务状态(后端 TaskStatus 枚举的字符串值) */
export type TaskStatus = 'pending' | 'running' | 'paused' | 'completed' | 'failed'

// ============================================================
// 阶段 8:user_agent 向用户提问澄清意图
// ============================================================

/** 选择题选项 */
export interface ClarificationQuestionOption {
  value: string
  label: string
}

/**
 * user_agent 向用户提出的问题
 *
 * 两种类型:
 * - choice: 选择题(用户从 options 中选,可单选或多选)
 * - text: 填空题(用户自由文本回答)
 */
export interface ClarificationQuestion {
  id: string
  type: 'choice' | 'text'
  question: string
  placeholder?: string
  required?: boolean
  options?: ClarificationQuestionOption[]
  multi?: boolean
}

/** 任务当前待回答的问题(GET /tasks/{id}/pending_question) */
export interface PendingQuestion {
  ask_round: number
  questions: ClarificationQuestion[]
  reasoning?: string
  conversation_id?: string | null
}

/** 单个问题的答案 */
export interface AnswerItem {
  question_id: string
  value: string | string[]
}

/** 提交答案请求 */
export interface AnswerRequest {
  answers: AnswerItem[]
}

/** 提交答案响应 */
export interface AnswerResponse {
  accepted: boolean
  message?: string
}

/** 场景信息(后端 ScenarioInfo) */
export interface Scenario {
  id: string
  name: string
  /** 提交表单字段定义,前端按此动态渲染 */
  form_fields: ScenarioFormField[]
  /** 结果分组维度声明,null 表示平铺不分组 */
  result_grouping: ScenarioResultGrouping | null
  /** 结果项 metadata 字段展示声明 */
  result_meta_fields: ScenarioResultMetaField[]
  /** 覆盖度看板声明,null 表示不显示看板 */
  coverage: ScenarioCoverage | null
}

/** 场景表单字段定义 */
export interface ScenarioFormField {
  /** 字段名(提交时作为 params 的 key) */
  name: string
  /** text / url / textarea / select / number */
  type: 'text' | 'url' | 'textarea' | 'select' | 'number'
  label: string
  required: boolean
  placeholder?: string
  default?: string
  description?: string
  /** type=select 时的选项 */
  options?: { value: string; label: string }[]
}

/** 结果分组维度声明 */
export interface ScenarioResultGrouping {
  /** 从 result.metadata 取该字段分组 */
  field: string
  /** ordered(固定枚举+顺序) | dynamic(按值动态分组) */
  type: 'ordered' | 'dynamic'
  /** ordered 时的固定枚举值 */
  values: ScenarioResultGroupValue[]
  /** 元数据缺失该字段时的分组名 */
  default_label: string
  /** 默认分组颜色 key(对应前端 sev-<color> CSS class) */
  default_color: string
}

/** 分组枚举值 */
export interface ScenarioResultGroupValue {
  value: string
  label: string
  /** 颜色 key,对应前端 CSS class 后缀(如 critical/high/medium) */
  color: string
  /** 排序序号 */
  order: number
}

/** 结果 meta 字段展示声明 */
export interface ScenarioResultMetaField {
  /** metadata 中的 key */
  name: string
  label: string
  /** text / file(file 类型可点击跳转源码位置) */
  type: 'text' | 'file'
}

/** 覆盖度看板声明 */
export interface ScenarioCoverage {
  /** 维度列表(通常派生自场景 checklist) */
  dimensions: ScenarioCoverageDimension[]
}

/** 覆盖度维度 */
export interface ScenarioCoverageDimension {
  id: string
  name: string
  description: string
}

/** 任务覆盖度看板数据(GET /tasks/{id}/coverage) */
export interface TaskCoverage {
  /** 各维度覆盖状态 */
  dimensions: TaskCoverageDimension[]
  /** 已覆盖维度数 */
  covered_count: number
  /** 维度总数 */
  total_count: number
  /** 最新评估所在轮次(null 表示尚无评估) */
  last_round: number | null
}

/** 任务覆盖度维度(含运行时覆盖状态) */
export interface TaskCoverageDimension extends ScenarioCoverageDimension {
  /** 是否已覆盖 */
  covered: boolean
}

/** 提交任务请求(后端 TaskCreateRequest) */
export interface TaskCreateRequest {
  scenario: string
  user_input?: string
  params?: Record<string, unknown>
  /** 用户选择的 LLM 配置 id(对应 user_llm_configs.llm_configs[].id) */
  llm_config_id?: string
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
  llm_config_id?: string | null
  status: TaskStatus
  current_stage: string | null
  error_message: string | null
  created_at: string
  completed_at: string | null
  results: TaskResult[]
  conversations: Conversation[]
}

/** 任务列表项(后端 TaskListItem,精简版用于侧栏) */
export interface TaskListItem {
  id: string
  scenario: string
  user_input: string
  status: TaskStatus
  current_stage: string | null
  error_message: string | null
  created_at: string
  completed_at: string | null
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
  | 'plan'
  | 'question'
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

/**
 * plan 事件 data
 *
 * react_agent 在复杂任务的思考 content 里输出 <plan>...</plan> 计划清单,
 * 后端提取后推送此事件(覆盖式更新,取最新一次)。
 * 历史回放时前端从 thinking conversation.content 重新提取。
 */
export interface PlanEventData {
  /** 协作轮次 */
  round_idx: number
  /** 计划步骤列表 */
  steps: PlanStep[]
}

/** 计划步骤 */
export interface PlanStep {
  /** 步骤序号(从 1 开始) */
  id: number
  /** 步骤描述 */
  text: string
  /** 状态:pending / in_progress / done */
  status: 'pending' | 'in_progress' | 'done'
}

/**
 * question 事件 data(阶段 8:用户澄清)
 *
 * user_agent 在第 0 轮初始评估时,若认为用户意图不清晰,输出 ask_user=true
 * + questions 列表。后端推送此事件,前端弹出 QuestionDialog 让用户填答。
 * 用户提交后通过 POST /tasks/{id}/answer 唤醒后台线程继续评估。
 */
export interface QuestionEventData {
  /** 提问轮次(0=首次,1=用户回答后再问) */
  ask_round: number
  /** 问题列表(最后一题固定为"是否有其他补充") */
  questions: ClarificationQuestion[]
  /** user_agent 的判断依据(展示给用户参考) */
  reasoning?: string
  /** 对应的 Conversation 记录 id(落库的提问记录) */
  conversation_id?: string | null
}
