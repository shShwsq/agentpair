/**
 * 任务 API 模块
 *
 * 对应后端 app/routers/tasks.py 的端点。
 * 后端 POST /tasks 异步执行(立即返回 task_id),进度通过 SSE 端点观看。
 */
import client from './client'
import type {
  Scenario,
  TaskCreateRequest,
  TaskCreateResponse,
  TaskDetail,
  TaskListItem,
} from '@/types/task'

/** 列出可用场景 */
export function getScenarios(): Promise<Scenario[]> {
  return client.get('/scenarios').then((r) => r.data)
}

/**
 * 列出当前用户可见的任务(自己的 + 匿名的)
 *
 * 用于侧栏历史任务列表。按创建时间倒序。
 */
export function listTasks(params?: {
  limit?: number
  offset?: number
}): Promise<TaskListItem[]> {
  return client.get('/tasks', { params }).then((r) => r.data)
}

/**
 * 提交任务
 *
 * 后端立即返回 task_id(后台线程异步执行)。
 * 前端跳转详情页后通过 SSE 接收实时进度。
 */
export function createTask(req: TaskCreateRequest): Promise<TaskCreateResponse> {
  return client.post('/tasks', req).then((r) => r.data)
}

/** 查询任务详情(含对话记录与结果) */
export function getTask(taskId: string): Promise<TaskDetail> {
  return client.get(`/tasks/${taskId}`).then((r) => r.data)
}
