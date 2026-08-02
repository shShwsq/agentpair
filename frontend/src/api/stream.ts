/**
 * SSE 客户端封装
 *
 * 浏览器原生 EventSource 不能自定义 header,所以通过 ?token=XXX 传递鉴权。
 *
 * 用法:
 *   const es = subscribeTaskStream(taskId, {
 *     onConversation: (data) => { ... },
 *     onStatus: (data) => { ... },
 *     onThinkingDelta: (data) => { ... },  // 流式 token 增量
 *     onDone: () => { ... },
 *     onError: (msg) => { ... },
 *   })
 *   // 组件卸载时:es.close()
 */
import { getAccessToken } from './client'
import type {
  ChecklistReviewEventData,
  ConnectedData,
  ConversationEventData,
  DoneEventData,
  PlanEventData,
  QuestionEventData,
  SSEEvent,
  SSEEventType,
  StatusEventData,
  ThinkingDeltaEventData,
} from '@/types/task'

/** 事件回调接口 */
export interface StreamCallbacks {
  onConnected?: (data: ConnectedData) => void
  onConversation?: (data: ConversationEventData) => void
  onStatus?: (data: StatusEventData) => void
  /** 流式 token 增量(打字机效果)。每个 LLM 调用按 conv_id 累积 */
  onThinkingDelta?: (data: ThinkingDeltaEventData) => void
  /** 计划清单更新(复杂任务时 react_agent 输出 <plan>,后端提取推送) */
  onPlan?: (data: PlanEventData) => void
  /** 用户澄清提问(阶段 8:user_agent 输出 ask_user=true 时触发) */
  onQuestion?: (data: QuestionEventData) => void
  /** 覆盖度清单确认(user_agent 第 0 轮动态生成 checklist 后触发) */
  onChecklistReview?: (data: ChecklistReviewEventData) => void
  onDone?: (data: DoneEventData) => void
  onError?: (data: DoneEventData) => void
}

/**
 * 订阅任务事件流
 *
 * 返回 EventSource 实例,调用 .close() 取消订阅。
 */
export function subscribeTaskStream(
  taskId: string,
  callbacks: StreamCallbacks,
): EventSource {
  const token = getAccessToken()
  const params = new URLSearchParams()
  if (token) params.set('token', token)

  const url = `/api/tasks/${taskId}/stream?${params.toString()}`
  const es = new EventSource(url)

  // 为每种事件类型注册监听器
  const eventTypes: SSEEventType[] = [
    'connected',
    'conversation',
    'status',
    'thinking_delta',
    'plan',
    'question',
    'checklist_review',
    'done',
    'error',
  ]

  for (const type of eventTypes) {
    es.addEventListener(type, (e: MessageEvent) => {
      try {
        const event = JSON.parse(e.data) as SSEEvent
        const data = event.data as Record<string, unknown>

        switch (type) {
          case 'connected':
            callbacks.onConnected?.(data as unknown as ConnectedData)
            break
          case 'conversation':
            callbacks.onConversation?.(data as unknown as ConversationEventData)
            break
          case 'status':
            callbacks.onStatus?.(data as unknown as StatusEventData)
            break
          case 'thinking_delta':
            callbacks.onThinkingDelta?.(data as unknown as ThinkingDeltaEventData)
            break
          case 'plan':
            callbacks.onPlan?.(data as unknown as PlanEventData)
            break
          case 'question':
            callbacks.onQuestion?.(data as unknown as QuestionEventData)
            break
          case 'checklist_review':
            callbacks.onChecklistReview?.(data as unknown as ChecklistReviewEventData)
            break
          case 'done':
            callbacks.onDone?.(data as unknown as DoneEventData)
            es.close()
            break
          case 'error':
            callbacks.onError?.(data as unknown as DoneEventData)
            es.close()
            break
        }
      } catch (err) {
        console.error('SSE 事件解析失败:', err, e.data)
      }
    })
  }

  // EventSource 原生 error 事件(网络断开等)
  es.onerror = () => {
    // 浏览器会自动重连,但如果任务已结束就不需要重连
    // 这里不做特殊处理,由调用方通过 done/error 事件管理生命周期
    // 连接失败时 EventSource 会反复重试,调用方可在 onDone/onError 后 close
  }

  return es
}
