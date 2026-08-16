/**
 * 练习模块类型定义(对应后端 schemas/practice.py)
 */

// ---- 题目生成 / 确认 ----

export interface GenerateRequest {
  task_id: string
  max_findings?: number
}

/** 生成的候选题(draft,预览阶段含答案与解析供校对) */
export interface DraftQuestion {
  id: string
  qtype: 'single_choice' | 'true_false'
  stem: string
  code_snippet: string | null
  options: string[]
  answer_idx: number
  explanation: string
  difficulty: number
  knowledge_key: string | null
  knowledge_name: string | null
}

export interface GenerateResponse {
  questions: DraftQuestion[]
  skipped_findings: number
}

/** 异步出题句柄(POST /practice/generate 立即返回) */
export interface GenerateJobResponse {
  job_id: string
}

/** 出题进度与结果(GET /practice/generate/{job_id}) */
export interface GenerateJobStatus {
  status: 'pending' | 'running' | 'done' | 'error'
  done: number
  total: number
  error: string
  questions: DraftQuestion[]
  skipped_findings: number
}

export interface ConfirmQuestionsRequest {
  task_id: string
  question_ids: string[]
}

export interface ConfirmQuestionsResponse {
  confirmed: number
  discarded: number
}

/** 只转正指定 draft,不影响其余 draft(题库管理逐条操作用) */
export interface ActivateQuestionsRequest {
  question_ids: string[]
}

export interface ActivateQuestionsResponse {
  activated: number
}

// ---- 练习会话 ----

export interface StartSessionRequest {
  count?: number
  topic_filter?: string | null
  /** 题目白名单(错题重练):非空时只从这些 active 题中组卷 */
  question_ids?: string[]
}

/** 组卷下发的题面(不含答案) */
export interface SessionQuestion {
  id: string
  qtype: 'single_choice' | 'true_false'
  stem: string
  code_snippet: string | null
  options: string[]
  difficulty: number
  knowledge_name: string | null
}

export interface StartSessionResponse {
  session_id: string
  questions: SessionQuestion[]
  message: string
}

export interface SubmitAnswerRequest {
  question_id: string
  chosen_idx: number
}

/** 知识点记忆状态(SM-2) */
export interface KnowledgeState {
  knowledge_key: string
  knowledge_name: string
  ease_factor: number
  interval_days: number
  repetitions: number
  due_at: string | null
  attempts: number
  correct_count: number
  accuracy: number | null
}

export interface SubmitAnswerResponse {
  is_correct: boolean
  correct_idx: number
  explanation: string
  state: KnowledgeState | null
  answered_count: number
  total_count: number
}

// ---- 统计 / 题库 ----

export interface WeakPointItem {
  knowledge_key: string
  knowledge_name: string
  attempts: number
  correct_count: number
  /** 错误率 0-1 */
  accuracy: number
  ease_factor: number
  due_at: string | null
}

export interface PracticeStats {
  ability: number
  due_count: number
  total_attempts: number
  total_correct: number
  accuracy: number | null
  weak_points: WeakPointItem[]
  active_question_count: number
  draft_question_count: number
}

export interface QuestionListItem {
  id: string
  qtype: string
  stem: string
  difficulty: number
  status: 'draft' | 'active' | 'archived'
  knowledge_name: string | null
  attempts: number
  accuracy: number | null
  created_at: string
}

// ---- 导航徽章 / 历史会话 / 趋势 ----

/** 轻量汇总(导航徽章用) */
export interface PracticeSummary {
  due_count: number
  draft_count: number
}

export interface SessionListItem {
  id: string
  started_at: string
  finished_at: string | null
  question_count: number
  answered_count: number
  correct_count: number
  accuracy: number | null
}

export interface SessionAttemptItem {
  question_id: string
  stem: string
  qtype: string
  knowledge_name: string | null
  chosen_idx: number
  correct_idx: number
  is_correct: boolean
  answered_at: string
}

export interface SessionDetail {
  id: string
  started_at: string
  finished_at: string | null
  question_count: number
  attempts: SessionAttemptItem[]
}

/** 按周聚合的学习趋势点 */
export interface TrendPoint {
  week_start: string
  attempts: number
  correct: number
}

export interface TrendResponse {
  weeks: TrendPoint[]
}
