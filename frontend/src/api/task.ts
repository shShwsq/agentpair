/**
 * 任务 API 模块
 *
 * 对应后端 app/routers/tasks.py 的端点。
 * 后端 POST /tasks 异步执行(立即返回 task_id),进度通过 SSE 端点观看。
 */
import client from './client'
import type {
  AnswerRequest,
  AnswerResponse,
  PendingQuestion,
  Scenario,
  TaskCoverage,
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
 *
 * 可选 q:全文搜索关键词(后端 ILIKE 匹配 title / user_input /
 * conversation.content / conversation.reasoning / result.title / result.content)。
 */
export function listTasks(params?: {
  limit?: number
  offset?: number
  q?: string
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

/**
 * 查询任务覆盖度看板数据
 *
 * 仅当任务场景声明了 coverage 时可用(404 表示无看板)。
 * 返回各维度覆盖状态,基于最新一条 user_agent evaluation。
 */
export function getTaskCoverage(taskId: string): Promise<TaskCoverage> {
  return client.get(`/tasks/${taskId}/coverage`).then((r) => r.data)
}

/**
 * 下载任务报告(Markdown 格式,触发浏览器下载)
 *
 * 后端返回 text/markdown 附件。
 */
export function downloadTaskReportMarkdown(taskId: string): Promise<Blob> {
  return client
    .get(`/tasks/${taskId}/export`, {
      params: { format: 'markdown' },
      responseType: 'blob',
    })
    .then((r) => r.data)
}

/**
 * 获取任务报告 HTML(打印友好,前端用于新窗口打印为 PDF)
 *
 * 后端返回完整 HTML 文档(含内联样式)。
 */
export function getTaskReportHtml(taskId: string): Promise<string> {
  return client
    .get(`/tasks/${taskId}/export`, {
      params: { format: 'html' },
      responseType: 'text',
      transformResponse: [(x) => x],
    })
    .then((r) => r.data)
}

// ============================================================
// 阶段 8:用户澄清(user_agent 向用户提问)
// ============================================================

/**
 * 查询任务当前待回答的问题
 *
 * 用于刷新页面后恢复提问弹窗。无待回答问题时返回 null。
 */
export function getPendingQuestion(taskId: string): Promise<PendingQuestion | null> {
  return client.get(`/tasks/${taskId}/pending_question`).then((r) => r.data)
}

/**
 * 提交用户对澄清问题的答案
 *
 * 后端唤醒阻塞的后台线程,把答案拼回 user_intent 重新评估。
 * 返回 accepted=false 表示任务已结束 / 重复提交 / 状态异常。
 */
export function submitTaskAnswer(
  taskId: string,
  req: AnswerRequest,
): Promise<AnswerResponse> {
  return client.post(`/tasks/${taskId}/answer`, req).then((r) => r.data)
}

// ============================================================
// 任务暂停/恢复
// ============================================================

/**
 * 暂停正在运行的任务
 *
 * 后台线程会在下一个检查点(迭代边界/工具调用前)阻塞。
 * task.status 变为 paused,前端把"暂停"按钮切换为"恢复"按钮。
 */
export function pauseTask(taskId: string): Promise<{ status: string; message: string }> {
  return client.post(`/tasks/${taskId}/pause`).then((r) => r.data)
}

/**
 * 恢复已暂停的任务
 *
 * 唤醒在检查点阻塞的后台线程,task.status 变回 running。
 */
export function resumeTask(taskId: string): Promise<{ status: string; message: string }> {
  return client.post(`/tasks/${taskId}/resume`).then((r) => r.data)
}

// ============================================================
// 任务标题修改 / 任务删除
// ============================================================

/**
 * 修改任务标题
 *
 * 传空字符串等价于清除自定义标题(后端存 null,前端回退到 user_input 截断展示)。
 * 返回最新的任务详情,父组件可据此刷新本地状态。
 */
export function updateTaskTitle(taskId: string, title: string): Promise<TaskDetail> {
  return client.patch(`/tasks/${taskId}/title`, { title }).then((r) => r.data)
}

/**
 * 删除任务
 *
 * 后端级联删除对话/结果,并清理沙箱 session。
 * 删除当前正在浏览的任务后,前端需自行跳转离开详情页。
 */
export function deleteTask(taskId: string): Promise<void> {
  return client.delete(`/tasks/${taskId}`).then(() => undefined)
}
