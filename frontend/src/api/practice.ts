/**
 * 练习模块 API(对应后端 app/routers/practice.py)
 *
 * 题目来源为审计任务的真实发现,经 LLM 改编为客观题;
 * 组卷策略:到期复习(SM-2)+ 薄弱点强化 + 难度匹配。
 */
import client from './client'
import type {
  ConfirmQuestionsRequest,
  ConfirmQuestionsResponse,
  GenerateRequest,
  GenerateResponse,
  PracticeStats,
  QuestionListItem,
  StartSessionRequest,
  StartSessionResponse,
  SubmitAnswerRequest,
  SubmitAnswerResponse,
} from '@/types/practice'

/**
 * 从审计任务生成候选题(draft)
 *
 * 逐条 finding 调 LLM 出题,耗时较长(秒级~分钟级),放宽超时。
 */
export function generateQuestions(req: GenerateRequest): Promise<GenerateResponse> {
  return client
    .post('/practice/generate', req, { timeout: 300_000 })
    .then((r) => r.data)
}

/** 确认勾选的候选题入库,丢弃其余 draft */
export function confirmQuestions(
  req: ConfirmQuestionsRequest,
): Promise<ConfirmQuestionsResponse> {
  return client.post('/practice/questions/confirm', req).then((r) => r.data)
}

/** 按需即时组卷(答案不下发) */
export function startSession(req: StartSessionRequest): Promise<StartSessionResponse> {
  return client.post('/practice/sessions', req).then((r) => r.data)
}

/** 提交单题答案,返回判分结果与知识点记忆状态更新 */
export function submitAnswer(
  sessionId: string,
  req: SubmitAnswerRequest,
): Promise<SubmitAnswerResponse> {
  return client.post(`/practice/sessions/${sessionId}/answers`, req).then((r) => r.data)
}

/** 练习统计:能力值 / 到期复习数 / 薄弱点分布 */
export function getPracticeStats(): Promise<PracticeStats> {
  return client.get('/practice/stats').then((r) => r.data)
}

/** 题库列表(可按状态 / 知识点筛选) */
export function listQuestions(params?: {
  status?: string
  knowledge_point?: string
}): Promise<QuestionListItem[]> {
  return client.get('/practice/questions', { params }).then((r) => r.data)
}

/** 归档题目(不再参与组卷) */
export function archiveQuestion(questionId: string): Promise<{ archived: boolean }> {
  return client.post(`/practice/questions/${questionId}/archive`).then((r) => r.data)
}
