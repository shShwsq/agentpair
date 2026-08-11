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

/**
 * 场景信息(后端 ScenarioInfo,精简模板)
 *
 * 场景降级为模板后,仅保留 id/名称/描述/预设 prompt/推荐 skill,
 * 不再声明 checklist/prompt/工具白名单/结果 schema。
 */
export interface Scenario {
  id: string
  name: string
  description?: string
  /** 预设 prompt:选择场景时预填到用户输入框,用户可自由编辑 */
  preset_prompt?: string
  /** 推荐使用的 skill 列表(展示给用户参考,不强制) */
  recommended_skills?: string[]
}

/**
 * 覆盖度清单维度(user_agent 第 0 轮动态生成,用户可编辑确认)
 *
 * 维度对应一类检查目标,checklist 是该维度下的具体检查项。
 */
export interface ChecklistDimension {
  id: string
  name: string
  description: string
  /** 该维度下的检查项列表 */
  checklist: string[]
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
export interface TaskCoverageDimension {
  /** 是否已覆盖 */
  covered: boolean
  id: string
  name: string
  description: string
}

/** 提交任务请求(后端 TaskCreateRequest) */
export interface TaskCreateRequest {
  scenario: string
  /** 任务标题:可选,用户自定义便于在历史列表识别;为空时前端用 user_input 截断展示 */
  title?: string
  user_input?: string
  params?: Record<string, unknown>
  /** user_agent 评估使用的 LLM 配置 id(对应 user_llm_configs.llm_configs[].id) */
  llm_config_id?: string
  /**
   * 内置 react_agent 使用的 LLM 配置 id(仅 executor=builtin 时生效)。
   * 为空时回退到 llm_config_id(react_agent 与 user_agent 共用同一模型)。
   * 外部 CLI 执行器忽略此字段(模型由 CLI 账号配额管理)。
   */
  react_llm_config_id?: string
  /**
   * 执行器选择:"builtin"(默认,内置 react_agent)或某个 agent_type(如 "qoder_cli")
   *
   * builtin 模式下,react_agent 用 react_llm_config_id(回退到 llm_config_id),
   * user_agent 用 llm_config_id。
   * CLI 模式下,react 角色模型由该 CLI 自管,llm_config_id 仅用于 user_agent 评估。
   * 候选 agent 列表由后端 GET /agents/configs 动态返回(is_active=true 的)。
   */
  executor?: string
  /** 兼容字段:传 repo_url 时自动生成 user_input */
  repo_url?: string
  branch?: string
  scope?: string
  /**
   * 用户选择的 skill 列表(可选)
   *
   * 不传(undefined)= 使用全部可用 skill;传空数组 = 禁用所有 skill;
   * 传非空数组 = 仅允许使用列表中的 skill。
   */
  allowed_skills?: string[]
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
  /**
   * 消息类型:
   * - evaluation: user_agent 评估
   * - question: user_agent 向用户提问 / react_agent 接收的 user 指令(原始意图)
   * - answer: 用户对澄清提问的回答
   * - message: 用户在对话界面下方输入框主动发送的补充消息
   * - thinking: react_agent 思考过程
   * - tool_call / tool_result: 工具调用 / 结果
   * - submit: react_agent 提交结果
   * - summary: user_agent 最终总结
   * - error: 错误信息
   */
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
  /** 任务标题:可空,前端按 null 回退到 user_input 截断展示 */
  title: string | null
  user_input: string
  params?: Record<string, unknown> | null
  llm_config_id?: string | null
  /** 内置 react_agent 模型 id(空时回退到 llm_config_id) */
  react_llm_config_id?: string | null
  /** 执行器:"builtin"(内置 react_agent)或某个 agent_type(如 "qoder_cli") */
  executor?: string
  status: TaskStatus
  current_stage: string | null
  error_message: string | null
  created_at: string
  completed_at: string | null
  results: TaskResult[]
  conversations: Conversation[]
  /**
   * 覆盖度清单(user_agent 第 0 轮动态生成,用户确认后落库)
   *
   * null/未定义 = 未生成清单(任务尚未进入清单生成阶段,或场景不使用清单)。
   * 覆盖度看板据此判断是否展示。
   */
  checklist?: ChecklistDimension[] | null
  /** 用户选择的 skill 列表(null/未定义 = 全部可用) */
  allowed_skills?: string[] | null
}

/** 任务列表项(后端 TaskListItem,精简版用于侧栏) */
export interface TaskListItem {
  id: string
  scenario: string
  /** 任务标题:可空,前端按 null 回退到 user_input 截断展示 */
  title: string | null
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
  | 'conversation_update'
  | 'status'
  | 'thinking_delta'
  | 'plan'
  | 'question'
  | 'checklist_review'
  | 'done'
  | 'error'
  | 'agent_checkpoint'

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

/** conversation_update 事件 data(更新已有对话项的 content,如 Kimi 增量参数补全) */
export interface ConversationUpdateEventData {
  id: string
  content: string
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

/**
 * checklist_review 事件 data(覆盖度清单动态生成)
 *
 * user_agent 在第 0 轮动态生成覆盖度清单后推送此事件,前端弹出 ChecklistReviewDialog
 * 让用户编辑确认。用户提交后通过 POST /tasks/{id}/checklist 唤醒后台线程继续评估。
 */
export interface ChecklistReviewEventData {
  /** user_agent 生成的覆盖度维度列表 */
  checklist: ChecklistDimension[]
  /** user_agent 生成清单的依据(展示给用户参考,可选) */
  reasoning?: string
}

/**
 * agent_checkpoint 事件 data(user_agent 检查点评估结果)
 *
 * react_agent(含内置 react_agent 和外部 CLI agent)执行过程中,每 K 个迭代边界
 * user_agent 做轻量评估,判断方向是否跑偏。评估结果通过此事件推送前端展示。
 * interrupt=true 时表示 user_agent 决定打断并注入追问指令(软中断)。
 */
export interface AgentCheckpointEventData {
  /** 协作轮次 */
  round_idx: number
  /** 触发评估时的迭代序号 */
  iteration: number
  /** 是否打断(true=user_agent 认为方向跑偏,注入追问指令) */
  interrupt: boolean
  /** 评估理由(展示给用户看) */
  reason: string
  /** 打断时的追问指令(interrupt=true 时非空,注入 react_agent 作为 user 消息) */
  query: string | null
}

/**
 * agent 策略配置(检查点评估频率、打断权限、验证权限)
 *
 * 用户级默认存储在 UserPreference.agent_policy,任务级覆盖存储在
 * task.params["_agent_policy"]。resolve_agent_policy 合并两者后生效。
 */
export interface AgentPolicy {
  /** 统一 K 值,每 K 个迭代评估一次 */
  checkpoint_interval: number
  /** 高级:内置 react_agent 专用 K 值(null=用统一值) */
  checkpoint_interval_builtin: number | null
  /** 高级:CLI agent 专用 K 值(null=用统一值) */
  checkpoint_interval_cli: number | null
  /** user_agent 是否能打断 react_agent */
  allow_interrupt: boolean
  /** 每轮最多打断次数(防死锁) */
  max_interrupts_per_round: number
  /** user_agent 是否能自己在测试环境验证(实验性,先留开关) */
  allow_verify: boolean
}

// ============================================================
// 用户补充消息(对话界面下方输入框)
// ============================================================

/** 发送用户补充消息请求(POST /tasks/{id}/messages) */
export interface SendMessageRequest {
  /** 消息内容(1-8000 字符) */
  content: string
}

/** 发送用户补充消息响应 */
export interface SendMessageResponse {
  /** 是否被接受(false 表示任务状态不允许或内容无效) */
  accepted: boolean
  /** 提示信息(展示给用户) */
  message?: string
}
