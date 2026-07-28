/**
 * 任务 API 模块
 *
 * 对应后端 app/routers/tasks.py 的端点。
 *
 * 注意:后端 POST /tasks 当前是同步阻塞执行(agent 跑完才返回),
 * createTask 设了 10 分钟超时。阶段 9 异步化后可改回正常超时 + 轮询。
 */
import client from './client'
import type {
  Scenario,
  TaskCreateRequest,
  TaskCreateResponse,
  TaskDetail,
} from '@/types/task'

/** 列出可用场景 */
export function getScenarios(): Promise<Scenario[]> {
  return client.get('/scenarios').then((r) => r.data)
}

/**
 * 提交任务
 *
 * 后端同步阻塞,agent 跑完才返回。超时设 10 分钟。
 * 阶段 9 改异步后:超时改回 30s,提交后立即拿 task_id 轮询。
 */
export function createTask(req: TaskCreateRequest): Promise<TaskCreateResponse> {
  return client
    .post('/tasks', req, { timeout: 600_000 })
    .then((r) => r.data)
}

/** 查询任务详情(含对话记录与结果) */
export function getTask(taskId: string): Promise<TaskDetail> {
  return client.get(`/tasks/${taskId}`).then((r) => r.data)
}
