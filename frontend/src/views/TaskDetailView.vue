<script setup lang="ts">
/**
 * 任务详情页
 *
 * 三大区域:
 * 1. 任务概览:状态徽章、场景、创建时间、当前阶段、错误信息
 * 2. 结果清单:分组由 task.params._grouping 驱动(user_agent done 时声明),
 *    展示 title/content/meta(meta 字段从 results 的 metadata keys 动态推断)
 * 3. 协作对话流:按 round_idx 分组,展示 user_agent 与 react_agent 的来回
 *
 * 实时更新:SSE 接收每条对话/状态变更 + thinking_delta(流式 token 增量)。
 * 初始加载 GET /tasks/{id} 拿快照(补历史),然后 SSE 接收增量。
 *
 * 覆盖度看板:从 task.checklist 读取维度(user_agent 第 0 轮动态生成,
 * 用户可通过 ChecklistReviewDialog 编辑确认),不再从场景声明。
 *
 * 流式思考显示(thinking_delta):
 * - 一次 LLM 调用对应一个 conv_id,前端按 conv_id 累积 reasoning + content
 * - 流式期间以"流式思考卡片"显示打字机效果
 * - 流式结束后(phase='end')延迟 800ms 移除卡片,由后续 conversation 事件接管
 *   (reasoning 不入正式对话表,只在流式卡片临时显示)
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import ChecklistReviewDialog from '@/components/ChecklistReviewDialog.vue'
import ConversationMessage from '@/components/ConversationMessage.vue'
import QuestionDialog from '@/components/QuestionDialog.vue'
import UserMessageInput from '@/components/UserMessageInput.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import {
  downloadTaskReportMarkdown,
  getPendingChecklist,
  getPendingQuestion,
  getTask,
  getTaskCoverage,
  getTaskReportHtml,
  pauseTask,
  resumeTask,
  submitTaskAnswer,
  submitTaskChecklist,
} from '@/api/task'
import { subscribeTaskStream } from '@/api/stream'
import { extractErrorMessage } from '@/utils/error'
import type {
  AnswerItem,
  ChecklistDimension,
  ClarificationQuestion,
  Conversation,
  PlanStep,
  QuestionEventData,
  ChecklistReviewEventData,
  SendMessageResponse,
  TaskCoverage,
  TaskDetail,
  TaskResult,
  TaskStatus,
  ThinkingDeltaEventData,
} from '@/types/task'

const route = useRoute()
const router = useRouter()

const task = ref<TaskDetail | null>(null)
const loading = ref(true)
const error = ref('')
let eventSource: EventSource | null = null
/** 对话流容器引用,用于自动滚动到底部 */
const conversationRef = ref<HTMLElement | null>(null)

/** 工作区侧栏是否折叠(默认折叠,完全隐藏) */
const workspaceCollapsed = ref(true)

/** 任务详情侧栏是否折叠(右侧栏,默认展开;折叠时完全隐藏,主区聚焦对话) */
const detailCollapsed = ref(false)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

function toggleDetail(): void {
  detailCollapsed.value = !detailCollapsed.value
}

// ---- 流式思考项(thinking_delta 累积)----
// key: conv_id, value: 流式思考项状态
interface StreamingItem {
  conv_id: string
  round_idx: number
  role: 'react_agent' | 'user_agent'
  iteration?: number
  reasoning: string
  content: string
  status: 'streaming' | 'done' | 'error'
  started_at: string
  finished_at?: string
  /** reasoning 是否展开(默认折叠,流式期间自动展开,完成后折叠) */
  reasoning_expanded: boolean
  /** 全局递增序号(流式项到达顺序,用于调试) */
  seq: number
  /** 该流式 thinking 开始时,其所在 round 已收到的正式对话数(用于计算插入位置) */
  insertSeq: number
}

const streamingItems = reactive<Map<string, StreamingItem>>(new Map())
/**
 * 历史回放思考项的展开状态
 * key: conv_id(形如 history:${c.id});value: 是否展开
 *
 * 历史思考项在 roundGroups computed 里每次重算都会新建 streamingItem 对象,
 * 状态无法持久,且其 conv_id 未注册进 streamingItems,故 toggleReasoning 找不到。
 * 这里用独立 Map 持久化展开状态,computed 读取它,toggle 时修改它触发重算。
 */
const historyReasoningExpanded = reactive<Map<string, boolean>>(new Map())
/** 全局序号计数器:流式项到达顺序 */
let streamingSeqCounter = 0
/** 每 round 已收到的正式对话数(用于给 streamingItem 计算插入位置 seq) */
const convCountPerRound = reactive<Map<number, number>>(new Map())

// ---- 计划清单(plan 事件 + 历史回放)----
// key: round_idx,value: 该 round 最新一次的 plan 步骤列表(覆盖式更新)
const planPerRound = reactive<Map<number, PlanStep[]>>(new Map())

// ---- 用户澄清提问弹窗(阶段 8)----
// user_agent 在第 0 轮评估时若 ask_user=true,后端推送 question 事件,
// 前端弹出 QuestionDialog 让用户填答。刷新页面后通过 getPendingQuestion 恢复。
const questionOpen = ref(false)
const questionData = reactive<{
  questions: ClarificationQuestion[]
  reasoning: string
  askRound: number
}>({
  questions: [],
  reasoning: '',
  askRound: 0,
})
const submittingAnswer = ref(false)

/** 从 PendingQuestion / QuestionEventData 填充弹窗数据并打开 */
function openQuestionDialog(payload: {
  questions: ClarificationQuestion[]
  reasoning?: string
  ask_round?: number
}): void {
  questionData.questions = payload.questions ?? []
  questionData.reasoning = payload.reasoning ?? ''
  questionData.askRound = payload.ask_round ?? 0
  questionOpen.value = true
}

/** 用户提交答案:调 API,成功后关闭弹窗 */
async function handleSubmitAnswer(answers: AnswerItem[]): Promise<void> {
  if (!task.value?.id || submittingAnswer.value) return
  submittingAnswer.value = true
  try {
    const resp = await submitTaskAnswer(String(task.value.id), { answers })
    if (resp.accepted) {
      questionOpen.value = false
    } else {
      error.value = resp.message || '答案提交失败,任务可能已结束'
    }
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    submittingAnswer.value = false
  }
}

/** 用户取消提问弹窗(直接关闭,不提交) */
function handleCancelQuestion(): void {
  questionOpen.value = false
}

/** 刷新页面后恢复待回答问题弹窗(若后端有 pending question) */
async function restorePendingQuestion(taskId: string): Promise<void> {
  try {
    const pending = await getPendingQuestion(taskId)
    if (pending && pending.questions?.length) {
      openQuestionDialog({
        questions: pending.questions,
        reasoning: pending.reasoning,
        ask_round: pending.ask_round,
      })
    }
  } catch {
    // 无 pending question 或任务已结束,静默忽略
  }
}

// ---- 覆盖度清单确认弹窗(checklist_review 事件)----
// user_agent 在第 0 轮动态生成覆盖度清单后推送 checklist_review 事件,
// 前端弹出 ChecklistReviewDialog 让用户编辑确认。刷新页面后通过
// getPendingChecklist 恢复弹窗。
const checklistOpen = ref(false)
const checklistData = reactive<{
  checklist: ChecklistDimension[]
  reasoning: string
}>({
  checklist: [],
  reasoning: '',
})
const submittingChecklist = ref(false)

/** 从 ChecklistReviewEventData / 待确认清单 填充弹窗数据并打开 */
function openChecklistDialog(payload: {
  checklist: ChecklistDimension[]
  reasoning?: string
}): void {
  checklistData.checklist = payload.checklist ?? []
  checklistData.reasoning = payload.reasoning ?? ''
  checklistOpen.value = true
}

/** 用户提交清单:null=直接采用,数组=用户编辑后的清单 */
async function handleSubmitChecklist(
  checklist: ChecklistDimension[] | null,
): Promise<void> {
  if (!task.value?.id || submittingChecklist.value) return
  submittingChecklist.value = true
  try {
    const resp = await submitTaskChecklist(String(task.value.id), checklist)
    if (resp.accepted) {
      checklistOpen.value = false
    } else {
      error.value = resp.message || '清单提交失败,任务可能已结束'
    }
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    submittingChecklist.value = false
  }
}

/** 用户取消清单弹窗(直接关闭,不提交) */
function handleCancelChecklist(): void {
  checklistOpen.value = false
}

/** 刷新页面后恢复待确认清单弹窗(若后端有 pending checklist) */
async function restorePendingChecklist(taskId: string): Promise<void> {
  try {
    const pending = await getPendingChecklist(taskId)
    if (pending && pending.length > 0) {
      openChecklistDialog({ checklist: pending })
    }
  } catch {
    // 无 pending checklist 或任务已结束,静默忽略
  }
}

// ---- 加载 + SSE 订阅 ----

async function initTask(): Promise<void> {
  const taskId = route.params.id as string
  try {
    // 拉任务快照(场景降级后不再需要单独拉 /scenarios:结果分组/meta
    // 从 task.params._grouping 和 results 的 metadata keys 推断)
    const taskData = await getTask(taskId)
    task.value = taskData
    error.value = ''
    loading.value = false

    // 从历史对话提取 plan(刷新页面/迟到订阅者回放)
    // react_agent 的 type=thinking content 里可能含 <plan>...</plan>,
    // 每个 round 取最后一次出现的 plan(可能被后续思考更新过状态)
    extractPlanFromHistory(taskData.conversations)

    // 恢复 convCountPerRound(按 round 统计历史对话数,含 thinking,跳过 user question)
    // 必须和 roundGroups 里 localIdx 的基准一致:localIdx 跳过 user question,
    // 这里也跳过,否则刷新后新 thinking 的 insertSeq 偏大,seq 排到 tool_call 之前
    convCountPerRound.clear()
    for (const c of taskData.conversations) {
      if (c.role === 'user' && c.type === 'question') continue
      convCountPerRound.set(
        c.round_idx,
        (convCountPerRound.get(c.round_idx) ?? 0) + 1,
      )
    }

    // 2. 若任务仍在进行(含暂停态),连接 SSE 接收实时事件
    if (task.value && (task.value.status === 'pending' || task.value.status === 'running' || task.value.status === 'paused')) {
      connectSSE(taskId)
      // 恢复可能存在的待回答问题弹窗(刷新页面 / 迟到订阅者场景)
      // 后端 user_agent 可能已发出 ask_user,但 SSE 事件在连接前已错过,
      // 通过 GET /pending_question 拉取当前待回答问题
      void restorePendingQuestion(taskId)
      // 恢复可能存在的待确认清单弹窗(同理,SSE 事件可能已错过)
      void restorePendingChecklist(taskId)
    }

    // 3. 加载覆盖度看板(task.checklist 存在才拉取)
    void loadCoverage()
  } catch (err) {
    error.value = extractErrorMessage(err)
    loading.value = false
  }
}

// ---- 覆盖度看板(仅当 task.checklist 存在时启用) ----

const coverageData = ref<TaskCoverage | null>(null)

/**
 * 拉取覆盖度数据;task.checklist 不存在或任务无 evaluation 时静默置空。
 *
 * 场景降级后,覆盖度维度从 task.checklist 读取(由 user_agent 第 0 轮动态生成),
 * 不再从场景声明 coverage 读取。
 */
async function loadCoverage(): Promise<void> {
  if (!task.value?.id || !task.value.checklist?.length) return
  try {
    coverageData.value = await getTaskCoverage(String(task.value.id))
  } catch {
    coverageData.value = null
  }
}

// ---- 报告导出(Markdown 下载 / PDF 打印) ----

const exporting = ref(false)

/** 导出 Markdown:下载 .md 文件 */
async function exportMarkdown(): Promise<void> {
  if (!task.value?.id || exporting.value) return
  exporting.value = true
  try {
    const blob = await downloadTaskReportMarkdown(String(task.value.id))
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `task-${task.value.id}.md`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    exporting.value = false
  }
}

/** 导出 PDF:获取 HTML 报告,新窗口渲染并调起打印 */
async function exportPdf(): Promise<void> {
  if (!task.value?.id || exporting.value) return
  exporting.value = true
  try {
    const html = await getTaskReportHtml(String(task.value.id))
    const w = window.open('', '_blank')
    if (!w) {
      error.value = '无法打开新窗口,请检查浏览器弹窗拦截设置'
      return
    }
    w.document.write(html)
    w.document.close()
    w.focus()
    // 等待渲染后调起打印对话框(用户可选"另存为 PDF")
    setTimeout(() => w.print(), 400)
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    exporting.value = false
  }
}

function connectSSE(taskId: string): void {
  // 关闭旧连接
  if (eventSource) eventSource.close()

  eventSource = subscribeTaskStream(taskId, {
    onConnected: (data) => {
      // 更新状态(可能任务已结束)
      if (task.value) {
        task.value.status = data.status
        task.value.current_stage = data.current_stage
      }
    },
    onConversation: (data) => {
      if (!task.value) return
      // 追加到对话列表
      // 注意:react_agent 的 type=thinking 不走 SSE 推送(已在流式卡片展示),
      // 这里收到的都是其他类型(工具调用/结果/提交/用户指令/user_agent 评估等)
      const conv: Conversation = {
        id: data.id,
        round_idx: data.round_idx,
        role: data.role,
        type: data.type,
        content: data.content,
        reasoning: data.reasoning ?? null,
        created_at: data.created_at || new Date().toISOString(),
      }
      task.value.conversations.push(conv)
      // 维护该 round 的正式对话计数(供 streamingItem 计算插入位置 seq)
      // 必须与 roundGroups 里 localIdx 的基准一致:localIdx 跳过 user question
      // (user question 单独提到顶部 userDirective 渲染),这里也跳过,
      // 否则每 round 多算 1,流式 thinking 的 insertSeq 偏大,seq 排到 tool_call 之后,
      // 导致 thinking 不再是迭代起点,首个 tool_call 被甩进 plains(界面最底部)。
      if (!(data.role === 'user' && data.type === 'question')) {
        convCountPerRound.set(
          data.round_idx,
          (convCountPerRound.get(data.round_idx) ?? 0) + 1,
        )
      }
      // 自动滚动到底部
      nextTick(scrollToBottom)

      // user_agent 评估产出 → 刷新覆盖度看板
      if (data.role === 'user_agent' && data.type === 'evaluation') {
        void loadCoverage()
      }
    },
    onConversationUpdate: (data) => {
      if (!task.value) return
      // 更新已有对话项的 content(如 Kimi 增量参数补全后刷新 tool_call 显示)
      const conv = task.value.conversations.find((c) => c.id === data.id)
      if (conv) {
        conv.content = data.content
      }
    },
    onStatus: (data) => {
      if (task.value) {
        task.value.status = data.status
        task.value.current_stage = data.current_stage
      }
    },
    onThinkingDelta: (data) => {
      handleThinkingDelta(data)
      nextTick(scrollToBottom)
    },
    onPlan: (data) => {
      // 覆盖式更新:每个 round 只保留最新一次 plan
      planPerRound.set(data.round_idx, data.steps)
      nextTick(scrollToBottom)
    },
    onQuestion: (data: QuestionEventData) => {
      // user_agent 请求用户澄清:弹出 QuestionDialog
      openQuestionDialog({
        questions: data.questions,
        reasoning: data.reasoning,
        ask_round: data.ask_round,
      })
    },
    onChecklistReview: (data: ChecklistReviewEventData) => {
      // user_agent 动态生成覆盖度清单:弹出 ChecklistReviewDialog 让用户编辑确认
      openChecklistDialog({
        checklist: data.checklist,
        reasoning: data.reasoning,
      })
    },
    onDone: async () => {
      // 任务完成:拉取最终结果(含 results)
      try {
        task.value = await getTask(taskId)
        if (task.value) {
          // 重新提取 plan(最终快照可能含最后一轮的 plan 更新)
          extractPlanFromHistory(task.value.conversations)
          // 恢复 convCountPerRound(最终快照含所有 thinking,跳过 user question,与 localIdx 对齐)
          convCountPerRound.clear()
          for (const c of task.value.conversations) {
            if (c.role === 'user' && c.type === 'question') continue
            convCountPerRound.set(
              c.round_idx,
              (convCountPerRound.get(c.round_idx) ?? 0) + 1,
            )
          }
          // 清空流式卡片(已完成,由历史对话接管显示)
          streamingItems.clear()
        }
      } catch (err) {
        console.error('拉取最终结果失败:', err)
      }
    },
    onError: async (data) => {
      if (task.value) {
        task.value.status = 'failed'
        task.value.error_message = data.error_message || '执行失败'
      }
    },
  })
}

// ---- 流式增量处理 ----

function handleThinkingDelta(data: ThinkingDeltaEventData): void {
  const { conv_id, round_idx, role, phase, delta, iteration } = data

  if (phase === 'start') {
    // 创建新的流式项:reasoning 默认折叠(用户可手动展开查看思考链)
    // 记录该 round 当前已收到的正式对话数,用于后续 seq 计算(让 thinking 排在
    // 它之后的 tool_call 之前,而非所有 thinking 都挤在最前面)
    const insertSeq = convCountPerRound.get(round_idx) ?? 0
    streamingItems.set(conv_id, {
      conv_id,
      round_idx,
      role,
      iteration,
      reasoning: '',
      content: '',
      status: 'streaming',
      started_at: new Date().toISOString(),
      reasoning_expanded: false,
      seq: streamingSeqCounter++,
      insertSeq,
    })
    return
  }

  const item = streamingItems.get(conv_id)
  if (!item) {
    // 没收到 start 事件就来了 delta,创建一个
    const insertSeq = convCountPerRound.get(round_idx) ?? 0
    streamingItems.set(conv_id, {
      conv_id,
      round_idx,
      role,
      iteration,
      reasoning: '',
      content: '',
      status: 'streaming',
      started_at: new Date().toISOString(),
      reasoning_expanded: false,
      seq: streamingSeqCounter++,
      insertSeq,
    })
  }

  const cur = streamingItems.get(conv_id)!
  if (phase === 'reasoning') {
    cur.reasoning += delta
  } else if (phase === 'content') {
    cur.content += delta
  } else if (phase === 'error') {
    cur.status = 'error'
    cur.reasoning += `\n[错误] ${delta}`
  } else if (phase === 'end') {
    // 流式结束:标记完成,reasoning 自动折叠(只显示标题和字数提示)
    // 不移除卡片!reasoning 不入正式 conversation 表,移除后用户再也看不到了
    cur.status = 'done'
    cur.finished_at = new Date().toISOString()
    cur.reasoning_expanded = false
  }
}

/** 切换流式卡片 reasoning 的展开/折叠 */
function toggleReasoning(convId: string): void {
  // 实时流式项:状态存在 streamingItems 里
  const item = streamingItems.get(convId)
  if (item) {
    item.reasoning_expanded = !item.reasoning_expanded
    return
  }
  // 历史回放项:conv_id 形如 history:xxx,未注册进 streamingItems,
  // 用独立 Map 持久化展开状态(修改后触发 roundGroups computed 重算)
  const cur = historyReasoningExpanded.get(convId) ?? false
  historyReasoningExpanded.set(convId, !cur)
}

// ---- plan 提取工具(与后端 _extract_plan 正则一致)----

const PLAN_BLOCK_RE = /<plan>\s*([\s\S]*?)\s*<\/plan>/
const PLAN_LINE_RE = /^\s*(?:\d+[.、)]\s*)?(?:\[([\w_]+)\]\s*)?(.+)$/

/** 从单段 content 提取 plan 步骤列表,无 plan 块返回 null */
function parsePlanFromContent(content: string): PlanStep[] | null {
  const m = content.match(PLAN_BLOCK_RE)
  if (!m) return null
  const block = m[1]
  const steps: PlanStep[] = []
  let id = 0
  for (const line of block.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const lm = trimmed.match(PLAN_LINE_RE)
    if (!lm) continue
    id += 1
    let status: PlanStep['status'] = (lm[1] as PlanStep['status']) || 'pending'
    if (status !== 'pending' && status !== 'in_progress' && status !== 'done') {
      status = 'pending'
    }
    steps.push({ id, text: lm[2].trim(), status })
  }
  return steps.length > 0 ? steps : null
}

/** 从历史对话提取 plan,每个 round 取最后一次出现的 plan(可能被更新过状态) */
function extractPlanFromHistory(conversations: Conversation[]): void {
  // 按 round 收集所有含 plan 的 thinking content,保留每个 round 最后一次
  const lastPlanPerRound = new Map<number, PlanStep[]>()
  for (const c of conversations) {
    if (c.role !== 'react_agent' || c.type !== 'thinking') continue
    if (!c.content) continue
    const steps = parsePlanFromContent(c.content)
    if (steps) {
      lastPlanPerRound.set(c.round_idx, steps)
    }
  }
  for (const [roundIdx, steps] of lastPlanPerRound) {
    planPerRound.set(roundIdx, steps)
  }
}

function scrollToBottom(): void {
  if (conversationRef.value) {
    conversationRef.value.scrollTop = conversationRef.value.scrollHeight
  }
}

onMounted(initTask)
onUnmounted(() => {
  if (eventSource) eventSource.close()
})

/**
 * 切换任务时清理旧任务状态(组件复用,route.params.id 变化)
 */
function resetTaskState(): void {
  // 断开旧 SSE,避免向旧任务写数据
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
  // 清空流式/计划/对话计数等运行态
  streamingItems.clear()
  planPerRound.clear()
  convCountPerRound.clear()
  historyReasoningExpanded.clear()
  // 关闭提问弹窗
  questionOpen.value = false
  // 关闭清单确认弹窗
  checklistOpen.value = false
  // 重置任务视图态
  task.value = null
  coverageData.value = null
  loading.value = true
  error.value = ''
}

// 同一组件复用下,route.params.id 变化时重新加载任务
watch(
  () => route.params.id,
  (newId, oldId) => {
    if (!newId || newId === oldId) return
    resetTaskState()
    void initTask()
  },
)

// ---- 对话流分组:按 round_idx → 按 plan step 分组迭代 → 再按迭代分段 ----
//
// 层级结构:
//   round
//     ├─ plain segment     (user_agent 评估/追问/总结、user 指令等关键节点,平铺)
//     └─ step group        (plan step,文字=step.text,内含多个迭代;无 plan 时回退为单个平铺组)
//          └─ iteration segment (react_agent 一次 ReAct 循环:thinking + N 个工具调用/结果)
//
// 迭代识别:遇到 react_agent 的 thinking 项(实时流式或历史 type=thinking)就开新迭代,
// 后续 react_agent 的 tool_call/tool_result/submit 归入当前迭代,
// 直到遇到下一个 thinking(开新迭代)或非 react_agent 消息(关闭迭代,平铺该消息)。
//
// step 归属推断:用迭代内首个工具调用的工具名匹配 plan step 关键词
// (复用后端 _TOOL_STEP_KEYWORDS 映射,与 plan 状态推进逻辑一致)
//
// 折叠策略:
// - step 组:默认折叠(完成后)或展开(含流式中)。文字=step.text。
// - 迭代块:默认折叠。包含正在流式中的 thinking 时自动展开。
// - 工具调用组:默认折叠(一个迭代内的所有 tool_call/tool_result 合并为一个折叠块)。

interface DisplayItem {
  /** 正式对话用 UUID,流式项用 `stream:${conv_id}` */
  id: string
  round_idx: number
  created_at: string
  /** 是否流式思考项 */
  is_streaming: boolean
  /** 稳定排序序号:正式对话用数组下标,流式项用全局计数器(避免跨来源 created_at 时钟漂移) */
  seq: number
  /** 正式对话字段 */
  role?: string
  type?: string
  content?: string
  /** 完整评估/思考链(如 user_agent evaluation),可折叠回看 */
  reasoning?: string | null
  /** 流式项字段 */
  streaming?: StreamingItem
}

/** 平铺段:user_agent/user 等关键消息,直接渲染为单张卡片 */
interface PlainSegment {
  kind: 'plain'
  item: DisplayItem
}

/** 迭代段:react_agent 一次 ReAct 循环的所有产物 */
interface IterationSegment {
  kind: 'iteration'
  /** 迭代在 round 内的序号(从 1 开始) */
  iterationIdx: number
  /** 唯一标识:`${roundIdx}-${iterationIdx}` */
  id: string
  /** 该迭代的 thinking 项(流式或历史,通常 1 条) */
  thinkingItems: DisplayItem[]
  /** 该迭代内的工具调用项(tool_call + tool_result),按时间顺序 */
  toolItems: DisplayItem[]
  /** 该迭代内的其他 react_agent 项(submit 等) */
  otherItems: DisplayItem[]
  /** 是否包含正在流式中的项(自动展开用) */
  hasStreaming: boolean
}

/** plan step 分组:把归属同一 step 的迭代合并 */
interface StepGroup {
  kind: 'step'
  /** step 唯一标识:`${roundIdx}-step-${stepId}` 或 `${roundIdx}-nostep` */
  id: string
  /** step 文字(无 plan 时为"审计过程") */
  text: string
  /** step 状态(无 plan 时为 in_progress) */
  status: PlanStep['status'] | 'none'
  /** 该 step 下的迭代列表 */
  iterations: IterationSegment[]
  /** 是否含流式中(任一迭代流式则为 true) */
  hasStreaming: boolean
}

type RoundSegment = PlainSegment | StepGroup

interface RoundGroup {
  roundIdx: number
  label: string
  segments: RoundSegment[]
  /** 该 round 的计划清单(复杂任务时 react_agent 输出,空数组表示无 plan) */
  planSteps: PlanStep[]
}

/** 用户手动展开过的 step 组 id */
const expandedSteps = reactive<Set<string>>(new Set())
/** 用户手动展开过的迭代 id(流式结束后保留展开状态,不被自动折叠) */
const expandedIterations = reactive<Set<string>>(new Set())
/** 用户手动展开过的工具组 id(格式 `${iterId}-tools`) */
const expandedToolGroups = reactive<Set<string>>(new Set())

/** 判断 DisplayItem 是否为 react_agent 的 thinking(迭代起点) */
function isReactThinkingItem(item: DisplayItem): boolean {
  if (item.is_streaming) {
    return item.streaming?.role === 'react_agent'
  }
  return item.role === 'react_agent' && item.type === 'thinking'
}

/** 判断 DisplayItem 是否属于 react_agent(用于归入当前迭代) */
function isReactAgentItem(item: DisplayItem): boolean {
  if (item.is_streaming) return item.streaming?.role === 'react_agent'
  return item.role === 'react_agent'
}

/** 判断 DisplayItem 是否正在流式 */
function isStreamingActive(item: DisplayItem): boolean {
  return !!(item.is_streaming && item.streaming?.status === 'streaming')
}

/** 工具名 → plan step 关键词映射(与后端 _TOOL_STEP_KEYWORDS 保持一致) */
const TOOL_STEP_KEYWORDS: Record<string, string[]> = {
  clone_repo:     ['克隆', 'clone', '仓库'],
  list_files:     ['结构', '目录', '查看', 'list'],
  read_file:      ['读取', '依赖', '清单', 'read'],
  query_cve:      ['依赖', 'cve', '漏洞'],
  search_code:    ['注入', '密钥', '反序列化', 'ssrf', '路径', '认证', '授权',
                    '审计', '代码审计', 'search'],
  run_semgrep:    ['semgrep', 'sast', '静态分析'],
  list_skills:    ['skill', '技能'],
  skill:          ['skill', '技能'],
  submit_results: ['提交', '汇总', 'submit'],
}

/** 从工具调用 content 提取工具名 */
function extractToolNameFromContent(content: string): string {
  // 新格式(向 qoder CLI 看齐):"人类可读意图 [tool_name]\n{参数JSON}"
  // 匹配首行末尾的 [tool_name] 标签(首行是 intent,后续行是参数 JSON)
  const firstLine = content.split('\n', 1)[0]
  const m = firstLine.match(/\[(\w+)\]$/)
  return m ? m[1] : ''
}

/** 推断迭代归属哪个 plan step,返回 step id(无匹配返回 null) */
function inferStepFromIteration(
  iter: IterationSegment,
  planSteps: PlanStep[],
): number | null {
  if (!planSteps.length) return null
  // 取迭代内首个 tool_call 的工具名
  const firstToolCall = iter.toolItems.find(
    (i) => !i.is_streaming && i.type === 'tool_call',
  )
  if (!firstToolCall) return null
  const toolName = extractToolNameFromContent(firstToolCall.content || '')
  if (!toolName) return null
  const keywords = TOOL_STEP_KEYWORDS[toolName]
  if (!keywords) return null

  const kwLower = keywords.map((k) => k.toLowerCase())
  // 先找 pending(进入新步骤),再找 in_progress(同步骤内)
  for (const s of planSteps) {
    if (s.status !== 'pending') continue
    if (kwLower.some((k) => s.text.toLowerCase().includes(k))) return s.id
  }
  for (const s of planSteps) {
    if (s.status !== 'in_progress') continue
    if (kwLower.some((k) => s.text.toLowerCase().includes(k))) return s.id
  }
  return null
}

/** 把单个 round 内的 DisplayItem 列表先按迭代分段,再按 plan step 分组 */
function segmentRoundItems(
  roundIdx: number,
  items: DisplayItem[],
  planSteps: PlanStep[],
): RoundSegment[] {
  // 第一阶段:按 thinking 起点切迭代(原逻辑)
  const iterations: IterationSegment[] = []
  const plains: { idx: number; seg: PlainSegment }[] = []
  let current: IterationSegment | null = null
  let iterCounter = 0

  const closeCurrent = () => {
    if (current) {
      iterations.push(current)
      current = null
    }
  }

  for (const item of items) {
    if (isReactThinkingItem(item)) {
      closeCurrent()
      iterCounter++
      current = {
        kind: 'iteration',
        iterationIdx: iterCounter,
        id: `${roundIdx}-${iterCounter}`,
        thinkingItems: [item],
        toolItems: [],
        otherItems: [],
        hasStreaming: isStreamingActive(item),
      }
    } else if (current && isReactAgentItem(item)) {
      if (item.is_streaming) {
        current.thinkingItems.push(item)
      } else if (item.type === 'tool_call' || item.type === 'tool_result') {
        current.toolItems.push(item)
      } else {
        current.otherItems.push(item)
      }
      if (isStreamingActive(item)) current.hasStreaming = true
    } else {
      closeCurrent()
      plains.push({ idx: iterations.length + plains.length, seg: { kind: 'plain', item } })
    }
  }
  closeCurrent()

  // 第二阶段:按 plan step 分组迭代
  // 无 plan 时,所有迭代归入单个"审计过程"组(保持折叠体验一致)
  const segments: RoundSegment[] = []
  const stepGroupsMap = new Map<number, StepGroup>()
  const noStepGroup: StepGroup = {
    kind: 'step',
    id: `${roundIdx}-nostep`,
    text: '审计过程',
    status: 'none',
    iterations: [],
    hasStreaming: false,
  }

  for (const iter of iterations) {
    const stepId = inferStepFromIteration(iter, planSteps)
    if (stepId !== null) {
      // 归入 plan step 组
      let group = stepGroupsMap.get(stepId)
      if (!group) {
        const step = planSteps.find((s) => s.id === stepId)
        group = {
          kind: 'step',
          id: `${roundIdx}-step-${stepId}`,
          text: step?.text || '(未知步骤)',
          status: step?.status || 'pending',
          iterations: [],
          hasStreaming: false,
        }
        stepGroupsMap.set(stepId, group)
      }
      group.iterations.push(iter)
      if (iter.hasStreaming) group.hasStreaming = true
    } else {
      // 无法归属(无 plan 或工具名无匹配)→ 归入无 step 组
      noStepGroup.iterations.push(iter)
      if (iter.hasStreaming) noStepGroup.hasStreaming = true
    }
  }

  // 按 plan step 顺序输出 step 组(无 plan 时只有 noStepGroup)
  for (const step of planSteps) {
    const group = stepGroupsMap.get(step.id)
    if (group) {
      // 同步最新状态(plan 可能已被 LLM 更新)
      group.status = step.status
      segments.push(group)
    }
  }
  // 追加无法归属的迭代组(如果有)
  if (noStepGroup.iterations.length > 0) {
    segments.push(noStepGroup)
  }
  // 追加 plain 段(保持原顺序——plains 在迭代之后,简化处理)
  for (const { seg } of plains) {
    segments.push(seg)
  }

  return segments
}

const roundGroups = computed<RoundGroup[]>(() => {
  if (!task.value?.conversations && streamingItems.size === 0) return []

  const groups = new Map<number, DisplayItem[]>()

  // 加入正式对话:
  // - type=thinking 且有 reasoning → 转成流式卡片样式展示(只读,状态 done,reasoning 折叠)
  //   这样刷新页面后历史的思考过程仍以流式卡片的形式展示,和实时流式视觉一致
  // - role=user type=question(用户指令) → 跳过,单独提取到顶部 userDirective 显示
  // - 其他类型 → 正常对话项
  //
  // seq 计算:用"该 round 内的下标 * 1000"(每条间隔 1000,留出空间给实时流式 thinking 插入)
  // 历史回放场景:thinking 和 tool_call 都在 convs,下标交替,顺序天然正确。
  const convs = task.value?.conversations ?? []
  const roundCounter = new Map<number, number>() // 每 round 内的下标计数
  convs.forEach((c) => {
    // 用户指令不进 round 分组,提到最顶部单独渲染
    if (c.role === 'user' && c.type === 'question') return

    const localIdx = roundCounter.get(c.round_idx) ?? 0
    roundCounter.set(c.round_idx, localIdx + 1)
    const seq = localIdx * 1000

    if (c.type === 'thinking' && c.reasoning) {
      // 还原为流式卡片(只读模式)
      const historyConvId = `history:${c.id}`
      const streamingItem: StreamingItem = {
        conv_id: historyConvId,
        round_idx: c.round_idx,
        role: c.role as 'react_agent' | 'user_agent',
        reasoning: c.reasoning,
        content: c.content,
        status: 'done',
        started_at: c.created_at,
        finished_at: c.created_at,
        // 从独立 Map 读取持久化的展开状态(实时流式项不在此处读取)
        reasoning_expanded: historyReasoningExpanded.get(historyConvId) ?? false,
        seq: 0,
        insertSeq: localIdx,
      }
      if (!groups.has(c.round_idx)) groups.set(c.round_idx, [])
      groups.get(c.round_idx)!.push({
        id: `stream:history:${c.id}`,
        round_idx: c.round_idx,
        created_at: c.created_at,
        is_streaming: true,
        seq,
        streaming: streamingItem,
      })
    } else {
      // 正常对话项
      if (!groups.has(c.round_idx)) groups.set(c.round_idx, [])
      groups.get(c.round_idx)!.push({
        id: c.id,
        round_idx: c.round_idx,
        created_at: c.created_at,
        is_streaming: false,
        seq,
        role: c.role,
        type: c.type,
        content: c.content,
        reasoning: c.reasoning,
      })
    }
  })

  // 加入实时流式思考项(SSE 期间)
  // 实时流式 thinking 不在 convs(后端 publish_event=False),只在 streamingItems 里。
  // 用 insertSeq(该 thinking 开始时该 round 已收到的正式对话数)定位插入位置:
  //   seq = insertSeq * 1000 - 500
  // 排在 convs[insertSeq-1](seq=(insertSeq-1)*1000)之后、convs[insertSeq](seq=insertSeq*1000)之前。
  // 例:thinking1 在 convCount=0 时开始(insertSeq=0),seq=-500,排在 tool_call1(seq=0)之前;
  //     thinking2 在 convCount=2 时开始(insertSeq=2),seq=1500,排在 tool_result1(seq=1000)
  //     之后、tool_call2(seq=2000)之前。这样每个 thinking 紧跟它之后的 tool_call/tool_result,
  //     正确归入各自迭代,不会出现"所有 thinking 挤前面、所有 tool_call 堆最后"的错乱。
  for (const item of streamingItems.values()) {
    if (!groups.has(item.round_idx)) groups.set(item.round_idx, [])
    groups.get(item.round_idx)!.push({
      id: `stream:${item.conv_id}`,
      round_idx: item.round_idx,
      created_at: item.started_at,
      is_streaming: true,
      seq: item.insertSeq * 1000 - 500,
      streaming: item,
    })
  }

  return [...groups.entries()]
    .sort(([a], [b]) => a - b)
    .map(([roundIdx, items]) => {
      // 用 seq 排序(稳定,不依赖跨来源的 created_at)
      const sorted = items.sort((a, b) => a.seq - b.seq)
      const steps = planPerRound.get(roundIdx) ?? []
      return {
        roundIdx,
        label: roundIdx === 0 ? '初始评估' : `第 ${roundIdx} 轮`,
        segments: segmentRoundItems(roundIdx, sorted, steps),
        planSteps: steps,
      }
    })
})

/** 用户指令(从对话中提取,单独显示在最顶部) */
const userDirective = computed<DisplayItem | null>(() => {
  const c = task.value?.conversations?.find(
    (x) => x.role === 'user' && x.type === 'question',
  )
  if (!c) return null
  return {
    id: c.id,
    round_idx: c.round_idx,
    created_at: c.created_at,
    is_streaming: false,
    seq: 0,
    role: c.role,
    type: c.type,
    content: c.content,
  }
})

// ---- 折叠状态查询/切换 ----

/** step 组是否展开:用户手动展开 OR 含流式中(自动展开) OR 状态为 in_progress */
function isStepExpanded(group: StepGroup): boolean {
  return expandedSteps.has(group.id) || group.hasStreaming
}

function toggleStep(group: StepGroup): void {
  if (expandedSteps.has(group.id)) {
    expandedSteps.delete(group.id)
  } else {
    expandedSteps.add(group.id)
  }
}

/** step 组的状态图标:done=✓,in_progress=◌,pending=○,none=· */
function stepStatusIcon(status: PlanStep['status'] | 'none'): string {
  switch (status) {
    case 'done': return '✓'
    case 'in_progress': return '◌'
    case 'pending': return '○'
    default: return '·'
  }
}

/** 迭代是否展开:用户手动展开 OR 当前正在流式(自动展开) */
function isIterationExpanded(seg: IterationSegment): boolean {
  return expandedIterations.has(seg.id) || seg.hasStreaming
}

function toggleIteration(seg: IterationSegment): void {
  if (expandedIterations.has(seg.id)) {
    expandedIterations.delete(seg.id)
  } else {
    expandedIterations.add(seg.id)
  }
}

/** 工具组是否展开(默认折叠,仅由用户控制) */
function isToolGroupExpanded(iterId: string): boolean {
  return expandedToolGroups.has(`${iterId}-tools`)
}

function toggleToolGroup(iterId: string): void {
  const key = `${iterId}-tools`
  if (expandedToolGroups.has(key)) {
    expandedToolGroups.delete(key)
  } else {
    expandedToolGroups.add(key)
  }
}

/** 从工具调用 content 中提取工具名(用于折叠摘要预览) */
function extractToolName(content: string): string {
  // 新格式:"人类可读意图 [tool_name]\n{参数JSON}"
  // 匹配首行末尾的 [tool_name] 标签
  const firstLine = content.split('\n', 1)[0]
  const m = firstLine.match(/\[(\w+)\]$/)
  return m ? m[1] : firstLine.slice(0, 30)
}

/** 迭代内的工具调用数(只数 tool_call,不数 tool_result) */
function toolCallCount(seg: IterationSegment): number {
  return seg.toolItems.filter(
    (i) => !i.is_streaming && i.type === 'tool_call',
  ).length
}

/**
 * 判断 tool item 是否属于子智能体(Agent)调用,用于加缩进+左边线。
 * - tool_call:原始 content 末尾含 [Agent] 标签(后端 _build_tool_intent_detail 注入)
 * - tool_result:向前找最近的 tool_call,若它是 Agent 调用,则此 result 是其输出
 */
function isSubAgentToolItem(
  item: DisplayItem,
  items: DisplayItem[],
  index: number,
): boolean {
  if (item.type === 'tool_call') {
    return /\[Agent\]/.test(item.content || '')
  }
  if (item.type === 'tool_result') {
    for (let i = index - 1; i >= 0; i--) {
      if (items[i].type === 'tool_call') {
        return /\[Agent\]/.test(items[i].content || '')
      }
    }
  }
  return false
}

/** 迭代摘要:工具数量 + 工具名预览(最多 3 个) */
function iterationSummary(seg: IterationSegment): string {
  // 流式中:显示正在思考
  if (seg.hasStreaming) {
    return '正在思考...'
  }
  const count = toolCallCount(seg)
  if (count === 0) {
    // 没有工具调用,可能只有 thinking(纯回答)
    return '思考完成'
  }
  const names = seg.toolItems
    .filter((i) => !i.is_streaming && i.type === 'tool_call')
    .map((i) => extractToolName(i.content || ''))
    .slice(0, 3)
  const preview = names.join(', ')
  const extra = count > 3 ? ` 等 ${count} 个` : ''
  return `${count} 个工具调用: ${preview}${extra}`
}

/** plan 进度文本:已完成 / 总数 */
function planProgress(steps: PlanStep[]): string {
  const done = steps.filter((s) => s.status === 'done').length
  return `${done}/${steps.length}`
}

// ---- 结果分组:由 task.params._grouping 驱动 ----
//
// 场景降级后,分组声明由 user_agent 在 done 时写入 task.params._grouping,
// 不再从场景声明读取。grouping 结构与原 ScenarioResultGrouping 一致:
// - 无 _grouping:不分组,所有结果放入单个"结果"组平铺
// - type=ordered:按声明 values 的 order 排序,metadata 缺失该字段用 default 组
// - type=dynamic:按 metadata 实际值动态分组,缺失用 default 组
//
// color 与前端 sev-<color> CSS class 对齐(安全场景保留原视觉)

/** 分组枚举值(对应原 ScenarioResultGroupValue) */
interface ResultGroupValue {
  value: string
  label: string
  /** 颜色 key,对应前端 CSS class 后缀(如 critical/high/medium) */
  color: string
  /** 排序序号 */
  order: number
}

/** 分组声明(对应原 ScenarioResultGrouping,从 task.params._grouping 读取) */
interface ResultGrouping {
  /** 从 result.metadata 取该字段分组 */
  field: string
  /** ordered(固定枚举+顺序) | dynamic(按值动态分组) */
  type: 'ordered' | 'dynamic'
  /** ordered 时的固定枚举值 */
  values: ResultGroupValue[]
  /** 元数据缺失该字段时的分组名 */
  default_label: string
  /** 默认分组颜色 key(对应前端 sev-<color> CSS class) */
  default_color: string
}

interface ResultGroup {
  /** 分组 key(severity 值或 '__default__' / 'all') */
  key: string
  label: string
  /** 颜色 key,对应 sev-<color> CSS class */
  color: string
  results: TaskResult[]
}

/** 从 task.params._grouping 读取分组声明(可能不存在) */
const resultGrouping = computed<ResultGrouping | null>(() => {
  const g = task.value?.params?.['_grouping'] as Partial<ResultGrouping> | undefined
  if (!g || !g.field) return null
  // 形态校验通过即可,字段完整性由后端保证
  return g as ResultGrouping
})

const resultGroups = computed<ResultGroup[]>(() => {
  if (!task.value?.results) return []
  const results = task.value.results
  const grouping = resultGrouping.value

  // 不分组:单个平铺组
  if (!grouping) {
    return [{ key: 'all', label: '结果', color: 'unknown', results }]
  }

  // 按 grouping.field 从 metadata 取值分组
  const buckets = new Map<string, TaskResult[]>()
  for (const r of results) {
    const raw = (r.metadata_?.[grouping.field] as string | undefined) ?? ''
    const key = raw || '__default__'
    if (!buckets.has(key)) buckets.set(key, [])
    buckets.get(key)!.push(r)
  }

  if (grouping.type === 'ordered') {
    // 按声明 values 的 order 排序,仅渲染有结果的组
    const ordered: ResultGroup[] = []
    for (const v of [...grouping.values].sort((a, b) => a.order - b.order)) {
      const rs = buckets.get(v.value) || []
      if (rs.length > 0) {
        ordered.push({ key: v.value, label: v.label, color: v.color, results: rs })
      }
    }
    // default 组(metadata 缺失字段的结果)
    const defaultRs = buckets.get('__default__') || []
    if (defaultRs.length > 0) {
      ordered.push({
        key: '__default__',
        label: grouping.default_label,
        color: grouping.default_color,
        results: defaultRs,
      })
    }
    return ordered
  }

  // dynamic:按实际值动态分组,缺失用 default
  const dyn: ResultGroup[] = []
  for (const [key, rs] of buckets) {
    if (key === '__default__') {
      dyn.push({ key, label: grouping.default_label, color: grouping.default_color, results: rs })
    } else {
      dyn.push({ key, label: key, color: grouping.default_color, results: rs })
    }
  }
  return dyn
})

// ---- 状态徽章 ----

const statusConfig: Record<TaskStatus, { label: string; class: string }> = {
  pending: { label: '等待中', class: 'badge-pending' },
  running: { label: '进行中', class: 'badge-running' },
  paused: { label: '已暂停', class: 'badge-paused' },
  completed: { label: '已完成', class: 'badge-completed' },
  failed: { label: '已失败', class: 'badge-failed' },
}

// ---- 是否运行中(控制滚动区域提示) ----
// paused 也算"活跃"状态:仍在 SSE 订阅,UI 显示暂停徽标 + 恢复按钮
const isRunning = computed(
  () =>
    task.value?.status === 'pending' ||
    task.value?.status === 'running' ||
    task.value?.status === 'paused',
)

/** 是否处于暂停态(控制按钮文案:暂停 ↔ 恢复) */
const isPaused = computed(() => task.value?.status === 'paused')

/** 暂停/恢复按钮 loading 态(防止重复点击) */
const pausing = ref(false)

/** 点击暂停/恢复按钮:根据当前状态调对应 API */
async function handleTogglePause(): Promise<void> {
  if (!task.value?.id || pausing.value) return
  pausing.value = true
  try {
    if (isPaused.value) {
      await resumeTask(String(task.value.id))
    } else if (task.value.status === 'running' || task.value.status === 'pending') {
      await pauseTask(String(task.value.id))
    }
    // 状态变更由 SSE status 事件驱动更新,这里不本地改写
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    pausing.value = false
  }
}

// ---- 结果卡片 metadata 渲染:动态推断 ----
//
// 场景降级后,展示字段不再由场景声明 result_meta_fields 决定,
// 而是从所有 results 的 metadata keys 动态推断:
// - 收集所有 results 的 metadata keys 的并集(保持首次出现顺序)
// - file_path 视为 file 类型(可点击跳转源码),其余视为 text
// - 单条 result 渲染时,只展示该 result 实际有值(非空)的字段

/** 推断的 metadata 展示字段 */
interface InferredMetaField {
  name: string
  type: 'text' | 'file'
}

interface ResultMetaItem {
  field: InferredMetaField
  value: string
}

/**
 * 推断结果 metadata 展示字段:从所有 results 的 metadata keys 动态收集
 *
 * 取并集(保持首次出现顺序),file_path 视为 file 类型,其余为 text。
 */
const inferredMetaFields = computed<InferredMetaField[]>(() => {
  const results = task.value?.results ?? []
  const seen = new Set<string>()
  const fields: InferredMetaField[] = []
  for (const r of results) {
    if (!r.metadata_) continue
    for (const key of Object.keys(r.metadata_)) {
      if (seen.has(key)) continue
      seen.add(key)
      fields.push({
        name: key,
        type: key === 'file_path' ? 'file' : 'text',
      })
    }
  }
  return fields
})

function getResultMetaItems(r: TaskResult): ResultMetaItem[] {
  const items: ResultMetaItem[] = []
  for (const f of inferredMetaFields.value) {
    const v = r.metadata_?.[f.name]
    if (v != null && v !== '') {
      items.push({ field: f, value: String(v) })
    }
  }
  return items
}

// ---- 结果项点击跳转源码位置(B1) ----

/** 工作区侧栏组件引用,用于调用 openTaskFile 跳转 */
const sidebarRef = ref<InstanceType<typeof WorkspaceSidebar> | null>(null)

/** 从 line_range 字符串(如 "10-20" / "10")解析起始行号 */
function parseStartLine(lineRange?: string): number | undefined {
  if (!lineRange) return undefined
  const m = lineRange.match(/(\d+)/)
  return m ? parseInt(m[1], 10) : undefined
}

/** 点击结果项的文件标签:展开侧栏并打开文件定位行号 */
async function onResultFileClick(r: TaskResult): Promise<void> {
  const path = r.metadata_?.['file_path'] as string | undefined
  if (!path || !task.value?.id) return
  const startLine = parseStartLine(r.metadata_?.['line_range'] as string | undefined)
  // 展开工作区侧栏(若折叠)
  workspaceCollapsed.value = false
  await nextTick()
  await sidebarRef.value?.openTaskFile(String(task.value.id), path, startLine)
}

// ---- 侧栏事件:任务被删除 / 标题被修改 ----

/** 侧栏删除任务后:若删除的是当前详情页任务,跳转离开(避免停在 404 页) */
function onSidebarTaskDeleted(taskId: string): void {
  if (task.value && task.value.id === taskId) {
    // 关闭 SSE 等资源
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
    router.replace('/tasks/new')
  }
}

/** 侧栏修改标题后:若改的是当前详情页任务,同步本地 task.title */
function onSidebarTaskTitleUpdated(taskId: string, title: string | null): void {
  if (task.value && task.value.id === taskId) {
    task.value = { ...task.value, title }
  }
}

// ---- 格式化时间 ----

function formatTime(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

/** 无标题时回退到 user_input,并截断到 max 字符(与历史任务侧栏一致) */
function truncateInput(s: string, max = 40): string {
  if (s.length <= max) return s
  return s.slice(0, max) + '...'
}

// ---- 用户补充消息输入框事件处理 ----

/**
 * 用户消息发送成功后的处理
 *
 * - completed 态:后端已启动 resume 线程(状态 → RUNNING),需重连 SSE
 *   接收新一轮的事件。用户消息的 conversation 事件已由后端在 reset_task_bus
 *   后 publish,会通过 SSE 历史补播送达。
 * - running / paused 态:SSE 已连接,conversation 事件由 onConversation 自动接收,
 *   无需额外处理。
 */
function handleMessageSent(_resp: SendMessageResponse): void {
  if (task.value?.status === 'completed') {
    // 后端 resume 线程已把状态改回 RUNNING,本地同步 + 重连 SSE
    task.value.status = 'running'
    task.value.current_stage = '用户追加消息,重启审计'
    connectSSE(String(task.value.id))
  }
  nextTick(scrollToBottom)
}

/** 用户消息发送失败:展示错误提示 */
function handleMessageError(message: string): void {
  error.value = message
}

/** 判断 DisplayItem 是否为用户补充消息(type=message,需右对齐展示) */
function isUserMessageItem(item: DisplayItem): boolean {
  return !item.is_streaming && item.role === 'user' && item.type === 'message'
}
</script>

<template>
  <div class="page">
    <AppHeader>
      <template #leading>
        <WorkspaceToggleButton
          v-if="task"
          :collapsed="workspaceCollapsed"
          expand-title="展开历史任务"
          collapse-title="折叠历史任务"
          @toggle="toggleWorkspace"
        />
      </template>
      <template #nav>
        <RouterLink to="/">首页</RouterLink>
        <RouterLink to="/tasks/new">提交任务</RouterLink>
        <RouterLink to="/models">模型设置</RouterLink>
        <RouterLink to="/cli">CLI 设置</RouterLink>
        <RouterLink to="/memory">记忆</RouterLink>
      </template>
    </AppHeader>

    <div class="page-body">
    <!-- 左侧:历史任务栏 + 按需切换工作区(折叠时完全隐藏) -->
    <WorkspaceSidebar
      v-if="!workspaceCollapsed"
      ref="sidebarRef"
      @task-deleted="onSidebarTaskDeleted"
      @task-title-updated="onSidebarTaskTitleUpdated"
    />

    <main class="main">
      <!-- 标题/状态行(固定在滚动容器外,不随对话滚动;有任务即显示) -->
      <div v-if="task" class="conv-header">
        <!-- 左侧:对话标题 + 创建时间 -->
        <div class="conv-header-info">
          <span class="conv-header-title" :title="task?.title || task?.user_input">
            {{ truncateInput(task?.title || task?.user_input || '') }}
          </span>
          <span v-if="task?.created_at" class="conv-header-time">{{ formatTime(task.created_at) }}</span>
        </div>
        <!-- 右侧:暂停态橙色徽标 + 恢复按钮;运行态红色实时徽标 + 暂停按钮 -->
        <template v-if="isRunning">
          <span v-if="isPaused" class="paused-indicator">
            <span class="paused-bars" /><span>已暂停</span>
          </span>
          <span v-else class="live-indicator">
            <span class="live-dot" />实时
          </span>
          <button
            class="btn-pause"
            :disabled="pausing"
            :title="isPaused ? '恢复执行' : '暂停执行'"
            @click="handleTogglePause"
          >
            {{ pausing ? '处理中...' : isPaused ? '恢复' : '暂停' }}
          </button>
        </template>
      </div>
      <div ref="conversationRef" class="main-scroll">
      <!-- 加载中 -->
      <div v-if="loading" class="loading-state">
        <div class="spinner-lg" />
        <p>加载任务详情...</p>
      </div>

      <!-- 错误 -->
      <div v-else-if="error" class="error-state">
        <p>{{ error }}</p>
        <RouterLink to="/tasks/new">提交新任务</RouterLink>
      </div>

      <!-- 任务详情(主区聚焦结果清单 + 协作对话流;任务详情/覆盖度看板在右侧栏) -->
      <template v-else-if="task">
        <!-- 结果清单(分组由 task.params._grouping 驱动) -->
        <section v-if="task.results.length > 0" class="results-section">
          <h2>结果清单 <span class="count">({{ task.results.length }})</span></h2>
          <div v-for="group in resultGroups" :key="group.key" class="severity-group">
            <h3>
              <span :class="['severity-tag', `sev-${group.color}`]">{{ group.label }}</span>
              <span class="count">{{ group.results.length }}</span>
            </h3>
            <div class="result-cards">
              <article
                v-for="r in group.results"
                :key="r.id"
                class="result-card"
              >
                <div class="result-header">
                  <h4>{{ r.title }}</h4>
                  <span class="round-tag">第 {{ r.round_idx }} 轮</span>
                </div>
                <p class="result-content">{{ r.content }}</p>
                <div v-if="getResultMetaItems(r).length > 0" class="result-meta">
                  <span
                    v-for="item in getResultMetaItems(r)"
                    :key="item.field.name"
                    :class="['meta-tag', { 'meta-file': item.field.type === 'file' }]"
                    @click="item.field.type === 'file' ? onResultFileClick(r) : undefined"
                  >
                    {{ item.value }}
                  </span>
                </div>
              </article>
            </div>
          </div>
        </section>

        <!-- 协作对话流(无外框,顶部仅在运行时显示实时徽标) -->
        <section v-if="roundGroups.length > 0 || isRunning" class="conversation-section">
          <!-- 用户指令(右对齐,像聊天界面的用户消息气泡) -->
          <div v-if="userDirective" class="user-directive">
            <ConversationMessage
              :item="userDirective"
              @toggle-reasoning="toggleReasoning"
            />
          </div>

          <div v-for="group in roundGroups" :key="group.roundIdx" class="round-group">
            <div class="round-label">{{ group.label }}</div>
            <!-- 计划清单(复杂任务时 react_agent 输出,展示接下来要做的步骤 + 进度) -->
            <div v-if="group.planSteps.length > 0" class="plan-card">
              <div class="plan-header">
                <span class="plan-title">计划清单</span>
                <span class="plan-progress">{{ planProgress(group.planSteps) }}</span>
              </div>
              <div class="plan-steps">
                <div
                  v-for="s in group.planSteps"
                  :key="s.id"
                  :class="['plan-step', `plan-step-${s.status}`]"
                >
                  <span class="plan-step-icon">{{
                    s.status === 'done' ? '✓' : s.status === 'in_progress' ? '◌' : '○'
                  }}</span>
                  <span class="plan-step-text">{{ s.text }}</span>
                </div>
              </div>
            </div>
            <div class="messages">
              <template
                v-for="seg in group.segments"
                :key="seg.kind === 'step' ? `step-${seg.id}` : `plain-${seg.item.id}`"
              >
                <!-- 平铺段:user_agent 评估/追问/总结、user 指令等关键消息 -->
                <!-- 用户补充消息(type=message)右对齐,与顶部 userDirective 视觉一致 -->
                <div
                  v-if="seg.kind === 'plain'"
                  :class="{ 'user-msg-row': isUserMessageItem(seg.item) }"
                >
                  <ConversationMessage
                    :item="seg.item"
                    @toggle-reasoning="toggleReasoning"
                  />
                </div>

                <!-- step 分组:plan step 下含多个迭代(无 plan 时为单个"审计过程"组) -->
                <div
                  v-else
                  class="step-block"
                  :class="{
                    'step-streaming': seg.hasStreaming,
                    'step-done': seg.status === 'done',
                    'step-expanded': isStepExpanded(seg),
                  }"
                >
                  <div class="step-header" @click="toggleStep(seg)">
                    <span class="step-toggle">{{ isStepExpanded(seg) ? '▼' : '▶' }}</span>
                    <span
                      :class="['step-status-icon', `step-status-${seg.status}`]"
                    >{{ stepStatusIcon(seg.status) }}</span>
                    <span class="step-text">{{ seg.text }}</span>
                    <span class="step-iter-count">{{ seg.iterations.length }} 次迭代</span>
                    <span v-if="seg.hasStreaming" class="step-streaming-tag">
                      <span class="typing-dots"><span></span><span></span><span></span></span>
                    </span>
                  </div>
                  <div v-if="isStepExpanded(seg)" class="step-body">
                    <!-- 该 step 下的所有迭代 -->
                    <div
                      v-for="iter in seg.iterations"
                      :key="iter.id"
                      class="iteration-block"
                      :class="{
                        'iteration-streaming': iter.hasStreaming,
                        'iteration-expanded': isIterationExpanded(iter),
                      }"
                    >
                      <div class="iteration-header" @click="toggleIteration(iter)">
                        <span class="iteration-toggle">{{ isIterationExpanded(iter) ? '▼' : '▶' }}</span>
                        <span class="iteration-summary">{{ iterationSummary(iter) }}</span>
                        <span v-if="iter.hasStreaming" class="iteration-streaming-tag">
                          <span class="typing-dots"><span></span><span></span><span></span></span>
                        </span>
                      </div>
                      <div v-if="isIterationExpanded(iter)" class="iteration-body">
                        <!-- thinking 项(流式或历史) -->
                        <ConversationMessage
                          v-for="t in iter.thinkingItems"
                          :key="t.id"
                          :item="t"
                          @toggle-reasoning="toggleReasoning"
                        />
                        <!-- 工具调用折叠组(一个迭代内的所有 tool_call/tool_result 合并) -->
                        <div
                          v-if="iter.toolItems.length > 0"
                          class="tool-group"
                          :class="{ 'tool-group-expanded': isToolGroupExpanded(iter.id) }"
                        >
                          <div class="tool-group-header" @click="toggleToolGroup(iter.id)">
                            <span class="tool-group-toggle">{{ isToolGroupExpanded(iter.id) ? '▼' : '▶' }}</span>
                            <span class="tool-group-label">🔧 工具调用 ({{ toolCallCount(iter) }})</span>
                          </div>
                          <div v-if="isToolGroupExpanded(iter.id)" class="tool-group-body">
                            <ConversationMessage
                              v-for="(ti, idx) in iter.toolItems"
                              :key="ti.id"
                              :item="ti"
                              :is-sub-agent="isSubAgentToolItem(ti, iter.toolItems, idx)"
                              @toggle-reasoning="toggleReasoning"
                            />
                          </div>
                        </div>
                        <!-- 其他项(submit 等) -->
                        <ConversationMessage
                          v-for="o in iter.otherItems"
                          :key="o.id"
                          :item="o"
                          @toggle-reasoning="toggleReasoning"
                        />
                      </div>
                    </div>
                  </div>
                </div>
              </template>
            </div>
          </div>
          <!-- 运行中等待提示(没有流式项时才显示) -->
          <!-- 优先用后端推送的 current_stage(如"正在克隆仓库..."),无则回退通用文案 -->
          <!-- 暂停态:不显示打字动画(已暂停,不再思考) -->
          <div
            v-if="isRunning && streamingItems.size === 0"
            class="waiting-hint"
            :class="{ 'waiting-hint-paused': isPaused }"
          >
            <span v-if="!isPaused" class="typing-dots">
              <span></span><span></span><span></span>
            </span>
            {{ isPaused ? '已暂停,点击恢复按钮继续执行' : (task?.current_stage || '智能体思考中...') }}
          </div>
        </section>
      </template>
      </div>

      <!-- 用户补充消息输入框(running/paused/completed 可见,pending/failed 隐藏) -->
      <UserMessageInput
        v-if="task && (task.status === 'running' || task.status === 'paused' || task.status === 'completed')"
        :task-id="String(task.id)"
        :task-status="task.status"
        @sent="handleMessageSent"
        @error="handleMessageError"
      />
    </main>

    <!-- 右侧:任务详情 + 覆盖度看板(抽屉把手式;展开时顶部条带标题+折叠按钮,折叠时悬浮把手在 page-body 右上角) -->
    <aside v-if="task && !detailCollapsed" class="detail-sidebar">
      <div class="detail-sidebar-header">
        <span class="detail-sidebar-title">任务详情</span>
        <WorkspaceToggleButton
          side="right"
          :collapsed="false"
          expand-title="展开任务详情"
          collapse-title="折叠任务详情"
          @toggle="toggleDetail"
        />
      </div>
      <div class="detail-sidebar-body">
        <!-- 覆盖度(task.checklist 存在时显示,置顶以便无需滚动即可查看) -->
        <section v-if="task.checklist?.length && coverageData" class="coverage-section">
          <h2>
            覆盖度
            <span class="count">{{ coverageData.covered_count }}/{{ coverageData.total_count }}</span>
            <span v-if="coverageData.last_round !== null" class="coverage-round">
              第 {{ coverageData.last_round }} 轮评估
            </span>
          </h2>
          <div class="coverage-grid">
            <div
              v-for="d in coverageData.dimensions"
              :key="d.id"
              :class="['coverage-card', d.covered ? 'coverage-covered' : 'coverage-missing']"
              :title="d.description"
            >
              <span class="coverage-dot" />
              <span class="coverage-name">{{ d.name }}</span>
            </div>
          </div>
        </section>

        <!-- 任务详情(扁平化,无卡片外框) -->
        <section class="overview-section">
          <div class="overview-header">
            <span :class="['badge', statusConfig[task.status].class]">
              {{ statusConfig[task.status].label }}
            </span>
            <div
              v-if="task.status === 'completed' || task.results.length > 0"
              class="overview-actions"
            >
              <button
                class="btn-export"
                :disabled="exporting"
                title="下载 Markdown 报告"
                @click="exportMarkdown"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <polyline points="7 10 12 15 17 10" />
                  <line x1="12" y1="15" x2="12" y2="3" />
                </svg>
              </button>
              <button
                class="btn-export"
                :disabled="exporting"
                title="打印或另存为 PDF"
                @click="exportPdf"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polyline points="6 9 6 2 18 2 18 9" />
                  <path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2" />
                  <rect x="6" y="14" width="12" height="8" />
                </svg>
              </button>
            </div>
          </div>
          <dl class="overview-meta">
            <div>
              <dt>场景</dt>
              <dd>{{ task.scenario }}</dd>
            </div>
            <div>
              <dt>创建时间</dt>
              <dd>{{ formatTime(task.created_at) }}</dd>
            </div>
            <div v-if="task.completed_at">
              <dt>完成时间</dt>
              <dd>{{ formatTime(task.completed_at) }}</dd>
            </div>
          </dl>
          <div class="overview-input">
            <span class="label">用户意图</span>
            <p>{{ task.user_input }}</p>
          </div>
          <div v-if="task.current_stage" class="overview-stage">
            <span class="label">当前阶段</span>
            <p>{{ task.current_stage }}</p>
          </div>
          <div v-if="task.error_message" class="alert alert-error">
            {{ task.error_message }}
          </div>
        </section>
      </div>
    </aside>

    <!-- 折叠态:page-body 右上角悬浮把手(header 下方右侧,点击重新展开) -->
    <div v-else-if="task" class="detail-handle">
      <WorkspaceToggleButton
        side="right"
        :collapsed="true"
        expand-title="展开任务详情"
        collapse-title="折叠任务详情"
        @toggle="toggleDetail"
      />
    </div>
    </div>

    <!-- 用户澄清提问弹窗(user_agent ask_user=true 时触发) -->
    <QuestionDialog
      :open="questionOpen"
      :questions="questionData.questions"
      :reasoning="questionData.reasoning"
      :ask-round="questionData.askRound"
      :submitting="submittingAnswer"
      @submit="handleSubmitAnswer"
      @cancel="handleCancelQuestion"
    />

    <!-- 覆盖度清单确认弹窗(user_agent 动态生成 checklist 后触发) -->
    <ChecklistReviewDialog
      :open="checklistOpen"
      :checklist="checklistData.checklist"
      :reasoning="checklistData.reasoning"
      :submitting="submittingChecklist"
      @submit="handleSubmitChecklist"
      @cancel="handleCancelChecklist"
    />
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
  background: var(--color-bg);
}

.page-body {
  flex: 1;
  display: flex;
  align-items: stretch;
  min-height: 0;
  overflow: hidden;
  position: relative; /* 给折叠态悬浮把手定位 */
}

.main {
  flex: 1;
  min-width: 0;
  max-width: var(--content-width);
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}

/* 可滚动内容区(结果清单 + 协作对话流) */
.main-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-6) var(--space-6) var(--space-12);
}

/* 用户补充消息(type=message):右对齐,与顶部 userDirective 一致 */
.user-msg-row {
  display: flex;
  justify-content: flex-end;
}

.user-msg-row :deep(.msg-group) {
  max-width: 80%;
}

/* ---- 右侧任务详情栏(抽屉把手式;展开时顶部条+滚动内容区,折叠时不渲染) ---- */
.detail-sidebar {
  flex-shrink: 0;
  width: clamp(300px, 26vw, 380px);
  min-width: 300px;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--color-bg);
}

/* 顶部条:标题 + 折叠按钮(header 下方,固定不滚) */
.detail-sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.detail-sidebar-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

/* 内容区:独立滚动,卡片间距用 gap 统一 */
.detail-sidebar-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}



/* ---- 折叠态悬浮把手(page-body 右上角,header 下方右侧) ---- */
.detail-handle {
  position: absolute;
  right: var(--space-3);
  top: var(--space-3);
  z-index: 5;
}

/* ---- 响应式:窄屏下右侧栏改为覆盖式抽屉 ---- */
@media (max-width: 1024px) {
  .detail-sidebar {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    z-index: 20;
    width: min(380px, 85vw);
    box-shadow: var(--shadow-xl);
  }
}

/* ---- 加载 / 错误状态 ---- */
.loading-state,
.error-state {
  text-align: center;
  padding: var(--space-16) var(--space-6);
  color: var(--color-text-secondary);
}

.spinner-lg {
  width: 40px;
  height: 40px;
  margin: 0 auto var(--space-4);
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

/* ---- 任务详情概览(扁平化) ---- */
.overview-section {
  padding: var(--space-2) 0;
}

.overview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
  padding-bottom: var(--space-3);
  border-bottom: 1px solid var(--color-border);
}

.overview-actions {
  display: flex;
  gap: var(--space-2);
}

.btn-export {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  padding: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-export:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.btn-export:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.overview-meta {
  display: grid;
  grid-template-columns: 1fr;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}

.overview-meta dt {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  margin-bottom: var(--space-1);
}

.overview-meta dd {
  font-size: var(--fs-sm);
  color: var(--color-text);
}

.overview-input,
.overview-stage {
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
}

.overview-input .label,
.overview-stage .label {
  display: block;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  margin-bottom: var(--space-1);
}

.overview-input p,
.overview-stage p {
  font-size: var(--fs-sm);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 120px;
  overflow-y: auto;
}

/* ---- 状态徽章 ---- */
.badge {
  display: inline-flex;
  align-items: center;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  border-radius: var(--radius-full);
}

.badge-pending { background: var(--color-surface-alt); color: var(--color-text-secondary); }
.badge-running { background: var(--color-info-light); color: var(--color-info); }
.badge-paused { background: var(--color-warning-light); color: var(--color-warning); }
.badge-completed { background: var(--color-success-light); color: var(--color-success); }
.badge-failed { background: var(--color-danger-light); color: var(--color-danger); }

/* ---- 提示 ---- */
.alert {
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  margin-top: var(--space-3);
}

.alert-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border: 1px solid #fecaca;
}

/* ---- 通用 section ---- */
.results-section {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  padding: var(--space-6);
  margin-bottom: var(--space-6);
}

/* 对话流:无外框,直接铺在主区背景上(聊天式) */
.conversation-section {
  margin-bottom: var(--space-6);
}

.results-section h2 {
  font-size: var(--fs-lg);
  margin-bottom: var(--space-5);
}

.count {
  color: var(--color-text-muted);
  font-weight: var(--fw-normal);
  font-size: var(--fs-sm);
}

/* ---- 覆盖度 ---- */
.coverage-section {
  margin-bottom: 0;
}

.coverage-section h2 {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
}

.coverage-round {
  margin-left: auto;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  font-weight: var(--fw-normal);
}

.coverage-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: var(--space-2);
}

.coverage-card {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  transition: all var(--transition-fast);
}

.coverage-covered {
  border-color: var(--color-success);
  background: var(--color-success-light);
}

.coverage-covered .coverage-dot {
  background: var(--color-success);
}

.coverage-missing {
  border-color: var(--color-border);
  background: var(--color-surface-alt);
}

.coverage-missing .coverage-dot {
  background: var(--color-text-muted);
}

.coverage-dot {
  flex-shrink: 0;
  width: 8px;
  height: 8px;
  border-radius: var(--radius-full);
}

.coverage-name {
  color: var(--color-text);
  font-weight: var(--fw-medium);
  word-break: break-word;
}

/* ---- 结果清单 ---- */
.severity-group {
  margin-bottom: var(--space-5);
}

.severity-group h3 {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-base);
  margin-bottom: var(--space-3);
}

.severity-tag {
  display: inline-flex;
  align-items: center;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  border-radius: var(--radius-full);
}

.sev-critical { background: #fef2f2; color: #991b1b; border: 1px solid #fecaca; }
.sev-high { background: var(--color-danger-light); color: var(--color-danger); border: 1px solid #fecaca; }
.sev-medium { background: var(--color-warning-light); color: var(--color-warning); border: 1px solid #fde68a; }
.sev-low { background: #fefce8; color: #a16207; border: 1px solid #fef08a; }
.sev-info { background: var(--color-info-light); color: var(--color-info); border: 1px solid #bfdbfe; }
.sev-unknown { background: var(--color-surface-alt); color: var(--color-text-secondary); border: 1px solid var(--color-border); }

.result-cards {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.result-card {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  transition: border-color var(--transition-fast);
}

.result-card:hover {
  border-color: var(--color-border-strong);
}

.result-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-2);
}

.result-header h4 {
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-tight);
}

.round-tag {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  padding: var(--space-1) var(--space-2);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
}

.result-content {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  margin-bottom: var(--space-3);
}

.result-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.meta-tag {
  display: inline-flex;
  align-items: center;
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-xs);
  font-family: var(--font-mono);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
}

.meta-file {
  color: var(--color-primary);
  background: var(--color-primary-light);
  cursor: pointer;
  transition: filter var(--transition-fast);
}

.meta-file:hover {
  filter: brightness(0.95);
}

/* ---- 对话流 ---- */
/* 用户指令:右对齐气泡,像聊天界面的用户消息(消息卡头部已含"用户指令"标签) */
.user-directive {
  display: flex;
  justify-content: flex-end;
  margin-bottom: var(--space-6);
}

.user-directive :deep(.message) {
  max-width: 80%;
}

.round-group {
  margin-bottom: var(--space-6);
}

.round-group:last-child {
  margin-bottom: 0;
}

.round-label {
  display: inline-block;
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-primary);
  background: var(--color-primary-light);
  border-radius: var(--radius-full);
  margin-bottom: var(--space-3);
}

/* ---- 计划清单卡片 ---- */
.plan-card {
  margin-bottom: var(--space-3);
  padding: var(--space-3) var(--space-4);
  background: linear-gradient(135deg, #f0fdf4 0%, #ecfeff 100%);
  border: 1px solid #a7f3d0;
  border-radius: var(--radius-lg);
}

.plan-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-2);
}

.plan-title {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: #047857;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.plan-progress {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: #059669;
  padding: var(--space-1) var(--space-2);
  background: rgba(255, 255, 255, 0.7);
  border-radius: var(--radius-full);
}

.plan-steps {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.plan-step {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-sm);
  border-radius: var(--radius-sm);
  transition: background 0.15s ease;
}

.plan-step-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  border-radius: 50%;
}

.plan-step-text {
  flex: 1;
  word-break: break-word;
}

.plan-step-pending {
  color: var(--color-text-muted);
}

.plan-step-pending .plan-step-icon {
  color: var(--color-text-muted);
  background: var(--color-surface-alt);
}

.plan-step-in_progress {
  color: var(--color-text);
  background: rgba(245, 158, 11, 0.08);
}

.plan-step-in_progress .plan-step-icon {
  color: #f59e0b;
  background: rgba(245, 158, 11, 0.15);
  animation: plan-step-pulse 1.5s ease-in-out infinite;
}

.plan-step-done {
  color: var(--color-text-secondary);
  text-decoration: line-through;
  text-decoration-color: var(--color-text-muted);
}

.plan-step-done .plan-step-icon {
  color: #fff;
  background: #10b981;
}

@keyframes plan-step-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.6; transform: scale(0.9); }
}

.messages {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding-left: var(--space-4);
}

/* ---- step 分组(plan step,可折叠,内含多个迭代) ---- */
/* 左竖线已移除:状态色由 step header 内的状态图标(step-status-*)承载 */
.step-block {
  margin-bottom: var(--space-2);
}

.step-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: background 0.15s ease;
  user-select: none;
}

.step-header:hover {
  background: var(--color-surface-alt);
}

.step-toggle {
  flex-shrink: 0;
  width: 14px;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  text-align: center;
}

.step-status-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  border-radius: 50%;
}

.step-status-pending {
  color: var(--color-text-muted);
  background: var(--color-surface-alt);
}

.step-status-in_progress {
  color: #f59e0b;
  background: rgba(245, 158, 11, 0.15);
  animation: step-pulse 1.5s ease-in-out infinite;
}

.step-status-done {
  color: #fff;
  background: #10b981;
}

.step-status-none {
  color: var(--color-text-muted);
  background: transparent;
}

@keyframes step-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.6; transform: scale(0.9); }
}

.step-text {
  flex: 1;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.step-done .step-text {
  color: var(--color-text-secondary);
  text-decoration: line-through;
  text-decoration-color: var(--color-text-muted);
}

.step-iter-count {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  padding: var(--space-1) var(--space-2);
  background: var(--color-surface-alt);
  border-radius: var(--radius-full);
}

.step-streaming-tag {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
}

.step-body {
  padding-left: var(--space-3);
  padding-top: var(--space-1);
  padding-bottom: var(--space-1);
}

/* ---- 迭代块(react_agent 一次 ReAct 循环,可折叠) ---- */
.iteration-block {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface-alt);
  overflow: hidden;
}

.iteration-streaming {
  /* 包含正在流式 thinking 的迭代:加橙色光晕提示 */
  border-color: #f59e0b;
  box-shadow: 0 0 0 2px rgba(245, 158, 11, 0.15);
}

.iteration-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  cursor: pointer;
  user-select: none;
  background: var(--color-surface);
  border-bottom: 1px solid transparent;
  transition: background 0.15s ease;
}

.iteration-header:hover {
  background: var(--color-surface-alt);
}

.iteration-expanded .iteration-header {
  border-bottom-color: var(--color-border);
}

.iteration-toggle {
  display: inline-block;
  width: 14px;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  text-align: center;
}

.iteration-summary {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.iteration-streaming-tag {
  display: inline-flex;
  align-items: center;
}

.iteration-streaming-tag .typing-dots span {
  background: #f59e0b;
}

.iteration-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
}

/* ---- 工具调用折叠组(一个迭代内的所有 tool_call/tool_result 合并) ---- */
.tool-group {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  overflow: hidden;
}

.tool-group-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  cursor: pointer;
  user-select: none;
  border-bottom: 1px solid transparent;
  transition: background 0.15s ease;
}

.tool-group-header:hover {
  background: var(--color-surface-alt);
}

.tool-group-expanded .tool-group-header {
  border-bottom-color: var(--color-border);
}

.tool-group-toggle {
  display: inline-block;
  width: 14px;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  text-align: center;
}

.tool-group-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-secondary);
}

.tool-group-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
}

/* ---- 对话区头部 + 实时指示器(标题已移除,仅在运行时右对齐显示实时徽标) ---- */
.conv-header {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-6);
}

.conv-header-info {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
}

.conv-header-title {
  font-size: var(--fs-base);
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conv-header-time {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  white-space: nowrap;
}

.live-indicator {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-danger);
  padding: var(--space-1) var(--space-3);
  background: var(--color-danger-light);
  border-radius: var(--radius-full);
}

/* 暂停徽标:橙色,带两条竖线图标(CSS 绘制,不用 emoji) */
.paused-indicator {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-warning);
  padding: var(--space-1) var(--space-3);
  background: var(--color-warning-light);
  border-radius: var(--radius-full);
}

.paused-bars {
  display: inline-block;
  width: 8px;
  height: 8px;
  /* 两条竖线 = 暂停符号,用线性渐变绘制 */
  background:
    linear-gradient(
      to right,
      var(--color-warning) 0,
      var(--color-warning) 2px,
      transparent 2px,
      transparent 3px,
      var(--color-warning) 3px,
      var(--color-warning) 5px,
      transparent 5px
    );
}

/* 暂停/恢复按钮:与实时徽标并排 */
.btn-pause {
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-pause:hover:not(:disabled) {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.btn-pause:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.live-dot {
  width: 8px;
  height: 8px;
  background: var(--color-danger);
  border-radius: 50%;
  animation: pulse 1.5s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.5; transform: scale(0.85); }
}

/* ---- 运行中等待提示 ---- */
.waiting-hint {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-4);
  padding: var(--space-3) var(--space-4);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
  background: var(--color-surface-alt);
  border-radius: var(--radius-lg);
}

/* 暂停态:橙色提示,不闪烁 */
.waiting-hint-paused {
  color: var(--color-warning);
  background: var(--color-warning-light);
}

.typing-dots {
  display: inline-flex;
  gap: 3px;
}

.typing-dots span {
  width: 6px;
  height: 6px;
  background: var(--color-text-muted);
  border-radius: 50%;
  animation: typing 1.4s infinite;
}

.typing-dots span:nth-child(2) { animation-delay: 0.2s; }
.typing-dots span:nth-child(3) { animation-delay: 0.4s; }

@keyframes typing {
  0%, 60%, 100% { opacity: 0.3; transform: translateY(0); }
  30% { opacity: 1; transform: translateY(-4px); }
}
</style>
