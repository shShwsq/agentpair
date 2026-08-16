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

export interface ConfirmQuestionsRequest {
  task_id: string
  question_ids: string[]
}

export interface ConfirmQuestionsResponse {
  confirmed: number
  discarded: number
}

// ---- 练习会话 ----

export interface StartSessionRequest {
  count?: number
  topic_filter?: string | null
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
