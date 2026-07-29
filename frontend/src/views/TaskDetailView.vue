<script setup lang="ts">
/**
 * 任务详情页
 *
 * 三大区域:
 * 1. 任务概览:状态徽章、场景、创建时间、当前阶段、错误信息
 * 2. 结果清单:按 severity 分组(安全场景),展示 title/content/位置信息
 * 3. 协作对话流:按 round_idx 分组,展示 user_agent 与 react_agent 的来回
 *
 * 实时更新:SSE 接收每条对话/状态变更 + thinking_delta(流式 token 增量)。
 * 初始加载 GET /tasks/{id} 拿快照(补历史),然后 SSE 接收增量。
 *
 * 流式思考显示(thinking_delta):
 * - 一次 LLM 调用对应一个 conv_id,前端按 conv_id 累积 reasoning + content
 * - 流式期间以"流式思考卡片"显示打字机效果
 * - 流式结束后(phase='end')延迟 800ms 移除卡片,由后续 conversation 事件接管
 *   (reasoning 不入正式对话表,只在流式卡片临时显示)
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import ConversationMessage from '@/components/ConversationMessage.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import { getTask } from '@/api/task'
import { subscribeTaskStream } from '@/api/stream'
import { extractErrorMessage } from '@/utils/error'
import type {
  Conversation,
  TaskDetail,
  TaskResult,
  TaskStatus,
  ThinkingDeltaEventData,
} from '@/types/task'

const route = useRoute()

const task = ref<TaskDetail | null>(null)
const loading = ref(true)
const error = ref('')
let eventSource: EventSource | null = null
/** 对话流容器引用,用于自动滚动到底部 */
const conversationRef = ref<HTMLElement | null>(null)

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
/** 全局序号计数器:流式项到达顺序 */
let streamingSeqCounter = 0
/** 每 round 已收到的正式对话数(用于给 streamingItem 计算插入位置 seq) */
const convCountPerRound = reactive<Map<number, number>>(new Map())

// ---- 加载 + SSE 订阅 ----

async function initTask(): Promise<void> {
  const taskId = route.params.id as string
  try {
    // 1. 先 GET 拿任务快照(含历史对话)
    task.value = await getTask(taskId)
    error.value = ''
    loading.value = false

    // 2. 若任务仍在进行,连接 SSE 接收实时事件
    if (task.value && (task.value.status === 'pending' || task.value.status === 'running')) {
      connectSSE(taskId)
    }
  } catch (err) {
    error.value = extractErrorMessage(err)
    loading.value = false
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
      convCountPerRound.set(
        data.round_idx,
        (convCountPerRound.get(data.round_idx) ?? 0) + 1,
      )
      // 自动滚动到底部
      nextTick(scrollToBottom)
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
    onDone: async () => {
      // 任务完成:拉取最终结果(含 results)
      try {
        task.value = await getTask(taskId)
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
    // 创建新的流式项:流式期间 reasoning 默认展开(用户能看到模型在怎么想)
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
      reasoning_expanded: true,
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
      reasoning_expanded: true,
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
  const item = streamingItems.get(convId)
  if (item) {
    item.reasoning_expanded = !item.reasoning_expanded
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

// ---- 对话流分组:按 round_idx → 再按迭代分段 ----
//
// 层级结构:
//   round
//     ├─ plain segment     (user_agent 评估/追问/总结、user 指令等关键节点,平铺)
//     └─ iteration segment (react_agent 一次 ReAct 循环:thinking + N 个工具调用/结果 + 可选 submit)
//
// 迭代识别:遇到 react_agent 的 thinking 项(实时流式或历史 type=thinking)就开新迭代,
// 后续 react_agent 的 tool_call/tool_result/submit 归入当前迭代,
// 直到遇到下一个 thinking(开新迭代)或非 react_agent 消息(关闭迭代,平铺该消息)。
//
// 折叠策略:
// - 迭代块:默认折叠。包含正在流式中的 thinking 时自动展开;用户点击切换后以用户选择为准。
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

type RoundSegment = PlainSegment | IterationSegment

interface RoundGroup {
  roundIdx: number
  label: string
  segments: RoundSegment[]
}

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

/** 把单个 round 内的 DisplayItem 列表按迭代分段 */
function segmentRoundItems(roundIdx: number, items: DisplayItem[]): RoundSegment[] {
  const segments: RoundSegment[] = []
  let current: IterationSegment | null = null
  let iterCounter = 0

  const closeCurrent = () => {
    if (current) {
      segments.push(current)
      current = null
    }
  }

  for (const item of items) {
    if (isReactThinkingItem(item)) {
      // thinking = 新迭代起点,先关闭上一个
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
      // 归入当前迭代
      if (item.is_streaming) {
        current.thinkingItems.push(item)
      } else if (item.type === 'tool_call' || item.type === 'tool_result') {
        current.toolItems.push(item)
      } else {
        current.otherItems.push(item)
      }
      if (isStreamingActive(item)) current.hasStreaming = true
    } else {
      // user_agent / user / 其他 → 平铺
      closeCurrent()
      segments.push({ kind: 'plain', item })
    }
  }
  closeCurrent()
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
      const streamingItem: StreamingItem = {
        conv_id: `history:${c.id}`,
        round_idx: c.round_idx,
        role: c.role as 'react_agent' | 'user_agent',
        reasoning: c.reasoning,
        content: c.content,
        status: 'done',
        started_at: c.created_at,
        finished_at: c.created_at,
        reasoning_expanded: false,
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
      return {
        roundIdx,
        label: roundIdx === 0 ? '初始评估' : `第 ${roundIdx} 轮`,
        segments: segmentRoundItems(roundIdx, sorted),
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
  // content 形如 "调用 semgrep_scan({...})" 或 "调用 read_file({...})"
  const m = content.match(/^调用\s+(\w+)\s*\(/)
  return m ? m[1] : content.slice(0, 30)
}

/** 迭代内的工具调用数(只数 tool_call,不数 tool_result) */
function toolCallCount(seg: IterationSegment): number {
  return seg.toolItems.filter(
    (i) => !i.is_streaming && i.type === 'tool_call',
  ).length
}

/** 迭代摘要:工具数量 + 工具名预览(最多 3 个) */
function iterationSummary(seg: IterationSegment): string {
  const count = toolCallCount(seg)
  if (count === 0) {
    // 没有工具调用,可能只有 thinking(纯回答)
    return '思考中无工具调用'
  }
  const names = seg.toolItems
    .filter((i) => !i.is_streaming && i.type === 'tool_call')
    .map((i) => extractToolName(i.content || ''))
    .slice(0, 3)
  const preview = names.join(', ')
  const extra = count > 3 ? ` 等 ${count} 个` : ''
  return `${count} 个工具调用: ${preview}${extra}`
}

// ---- 结果分组:按 severity ----

type Severity = 'critical' | 'high' | 'medium' | 'low' | 'info' | 'unknown'

const severityOrder: Record<Severity, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
  info: 4,
  unknown: 5,
}

const severityLabels: Record<Severity, string> = {
  critical: '严重',
  high: '高危',
  medium: '中危',
  low: '低危',
  info: '提示',
  unknown: '未分级',
}

function getSeverity(result: TaskResult): Severity {
  const s = result.metadata_?.['severity'] as string | undefined
  if (s && s in severityOrder) return s as Severity
  return 'unknown'
}

interface SeverityGroup {
  severity: Severity
  label: string
  results: TaskResult[]
}

const severityGroups = computed<SeverityGroup[]>(() => {
  if (!task.value?.results) return []
  const groups = new Map<Severity, TaskResult[]>()
  for (const r of task.value.results) {
    const sev = getSeverity(r)
    if (!groups.has(sev)) groups.set(sev, [])
    groups.get(sev)!.push(r)
  }
  return [...groups.entries()]
    .sort(([a], [b]) => severityOrder[a] - severityOrder[b])
    .map(([severity, results]) => ({
      severity,
      label: severityLabels[severity],
      results,
    }))
})

// ---- 状态徽章 ----

const statusConfig: Record<TaskStatus, { label: string; class: string }> = {
  pending: { label: '等待中', class: 'badge-pending' },
  running: { label: '进行中', class: 'badge-running' },
  completed: { label: '已完成', class: 'badge-completed' },
  failed: { label: '已失败', class: 'badge-failed' },
}

// ---- 是否运行中(控制滚动区域提示) ----

const isRunning = computed(
  () => task.value?.status === 'pending' || task.value?.status === 'running',
)

// ---- 结果卡片 metadata 提取 ----

function getResultMeta(r: TaskResult): { cwe?: string; file?: string; line?: string } {
  const m = r.metadata_ || {}
  return {
    cwe: m['cwe'] as string | undefined,
    file: m['file_path'] as string | undefined,
    line: m['line_range'] as string | undefined,
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
</script>

<template>
  <div class="page">
    <AppHeader>
      <template #nav>
        <RouterLink to="/">首页</RouterLink>
        <RouterLink to="/tasks/new">提交任务</RouterLink>
        <RouterLink to="/settings">模型设置</RouterLink>
      </template>
    </AppHeader>

    <div class="page-body">
    <!-- 左侧:工作区文件树侧栏(可折叠) -->
    <WorkspaceSidebar
      v-if="task"
      :task-id="route.params.id as string"
      :is-running="isRunning"
    />

    <main class="main">
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

      <!-- 任务详情 -->
      <template v-else-if="task">
        <!-- 概览卡片 -->
        <section class="overview-card">
          <div class="overview-header">
            <h1>任务详情</h1>
            <span :class="['badge', statusConfig[task.status].class]">
              {{ statusConfig[task.status].label }}
            </span>
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

        <!-- 结果清单 -->
        <section v-if="task.results.length > 0" class="results-section">
          <h2>结果清单 <span class="count">({{ task.results.length }})</span></h2>
          <div v-for="group in severityGroups" :key="group.severity" class="severity-group">
            <h3>
              <span :class="['severity-tag', `sev-${group.severity}`]">{{ group.label }}</span>
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
                <div class="result-meta">
                  <span v-if="getResultMeta(r).cwe" class="meta-tag">{{ getResultMeta(r).cwe }}</span>
                  <span v-if="getResultMeta(r).file" class="meta-tag meta-file">
                    {{ getResultMeta(r).file }}<template v-if="getResultMeta(r).line">:{{ getResultMeta(r).line }}</template>
                  </span>
                </div>
              </article>
            </div>
          </div>
        </section>

        <!-- 协作对话流 -->
        <section v-if="roundGroups.length > 0 || isRunning" ref="conversationRef" class="conversation-section">
          <div class="conv-header">
            <h2>协作对话流</h2>
            <span v-if="isRunning" class="live-indicator">
              <span class="live-dot" />实时
            </span>
          </div>
          <!-- 用户指令(整个对话流最顶部,独立显示) -->
          <div v-if="userDirective" class="round-group">
            <div class="round-label">用户指令</div>
            <div class="messages">
              <ConversationMessage
                :item="userDirective"
                @toggle-reasoning="toggleReasoning"
              />
            </div>
          </div>

          <div v-for="group in roundGroups" :key="group.roundIdx" class="round-group">
            <div class="round-label">{{ group.label }}</div>
            <div class="messages">
              <template
                v-for="seg in group.segments"
                :key="seg.kind === 'iteration' ? `iter-${seg.id}` : `plain-${seg.item.id}`"
              >
                <!-- 平铺段:user_agent 评估/追问/总结、user 指令等关键消息 -->
                <ConversationMessage
                  v-if="seg.kind === 'plain'"
                  :item="seg.item"
                  @toggle-reasoning="toggleReasoning"
                />

                <!-- 迭代段:react_agent 一次 ReAct 循环(thinking + 工具调用 + 可选 submit) -->
                <div
                  v-else
                  class="iteration-block"
                  :class="{
                    'iteration-streaming': seg.hasStreaming,
                    'iteration-expanded': isIterationExpanded(seg),
                  }"
                >
                  <div class="iteration-header" @click="toggleIteration(seg)">
                    <span class="iteration-toggle">{{ isIterationExpanded(seg) ? '▼' : '▶' }}</span>
                    <span class="iteration-label">迭代 {{ seg.iterationIdx }}</span>
                    <span class="iteration-summary">{{ iterationSummary(seg) }}</span>
                    <span v-if="seg.hasStreaming" class="iteration-streaming-tag">
                      <span class="typing-dots"><span></span><span></span><span></span></span>
                    </span>
                  </div>
                  <div v-if="isIterationExpanded(seg)" class="iteration-body">
                    <!-- thinking 项(流式或历史) -->
                    <ConversationMessage
                      v-for="t in seg.thinkingItems"
                      :key="t.id"
                      :item="t"
                      @toggle-reasoning="toggleReasoning"
                    />
                    <!-- 工具调用折叠组(一个迭代内的所有 tool_call/tool_result 合并) -->
                    <div
                      v-if="seg.toolItems.length > 0"
                      class="tool-group"
                      :class="{ 'tool-group-expanded': isToolGroupExpanded(seg.id) }"
                    >
                      <div class="tool-group-header" @click="toggleToolGroup(seg.id)">
                        <span class="tool-group-toggle">{{ isToolGroupExpanded(seg.id) ? '▼' : '▶' }}</span>
                        <span class="tool-group-label">🔧 工具调用 ({{ toolCallCount(seg) }})</span>
                      </div>
                      <div v-if="isToolGroupExpanded(seg.id)" class="tool-group-body">
                        <ConversationMessage
                          v-for="ti in seg.toolItems"
                          :key="ti.id"
                          :item="ti"
                          @toggle-reasoning="toggleReasoning"
                        />
                      </div>
                    </div>
                    <!-- 其他项(submit 等) -->
                    <ConversationMessage
                      v-for="o in seg.otherItems"
                      :key="o.id"
                      :item="o"
                      @toggle-reasoning="toggleReasoning"
                    />
                  </div>
                </div>
              </template>
            </div>
          </div>
          <!-- 运行中等待提示(没有流式项时才显示) -->
          <div v-if="isRunning && streamingItems.size === 0" class="waiting-hint">
            <span class="typing-dots">
              <span></span><span></span><span></span>
            </span>
            智能体思考中...
          </div>
        </section>
      </template>
    </main>
    </div>
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
}

.main {
  flex: 1;
  min-width: 0;
  max-width: var(--content-width);
  margin: 0 auto;
  overflow-y: auto;
  padding: var(--space-6) var(--space-6) var(--space-12);
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

/* ---- 概览卡片 ---- */
.overview-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  padding: var(--space-6);
  margin-bottom: var(--space-6);
}

.overview-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-4);
}

.overview-header h1 {
  font-size: var(--fs-xl);
}

.overview-meta {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: var(--space-4);
  margin-bottom: var(--space-4);
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
  margin-bottom: var(--space-3);
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
.results-section,
.conversation-section {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  padding: var(--space-6);
  margin-bottom: var(--space-6);
}

.results-section h2,
.conversation-section h2 {
  font-size: var(--fs-lg);
  margin-bottom: var(--space-5);
}

.count {
  color: var(--color-text-muted);
  font-weight: var(--fw-normal);
  font-size: var(--fs-sm);
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
}

/* ---- 对话流 ---- */
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

.messages {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding-left: var(--space-4);
  border-left: 2px solid var(--color-border);
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

.iteration-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: #a855f7;
  text-transform: uppercase;
  letter-spacing: 0.03em;
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

/* ---- 对话区头部 + 实时指示器 ---- */
.conv-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-5);
}

.conv-header h2 {
  font-size: var(--fs-lg);
  margin: 0;
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
