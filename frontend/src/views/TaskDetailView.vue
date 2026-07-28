<script setup lang="ts">
/**
 * 任务详情页
 *
 * 三大区域:
 * 1. 任务概览:状态徽章、场景、创建时间、当前阶段、错误信息
 * 2. 结果清单:按 severity 分组(安全场景),展示 title/content/位置信息
 * 3. 协作对话流:按 round_idx 分组,展示 user_agent 与 react_agent 的来回
 *
 * 实时更新:SSE 接收每条对话/状态变更,实时追加到界面。
 * 初始加载 GET /tasks/{id} 拿快照(补历史),然后 SSE 接收增量。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import { getTask } from '@/api/task'
import { subscribeTaskStream } from '@/api/stream'
import { extractErrorMessage } from '@/utils/error'
import type {
  Conversation,
  TaskDetail,
  TaskResult,
  TaskStatus,
} from '@/types/task'

const route = useRoute()

const task = ref<TaskDetail | null>(null)
const loading = ref(true)
const error = ref('')
let eventSource: EventSource | null = null
/** 对话流容器引用,用于自动滚动到底部 */
const conversationRef = ref<HTMLElement | null>(null)

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
      const conv: Conversation = {
        id: data.id,
        round_idx: data.round_idx,
        role: data.role,
        type: data.type,
        content: data.content,
        created_at: data.created_at || new Date().toISOString(),
      }
      task.value.conversations.push(conv)
      // 自动滚动到底部
      nextTick(scrollToBottom)
    },
    onStatus: (data) => {
      if (task.value) {
        task.value.status = data.status
        task.value.current_stage = data.current_stage
      }
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

function scrollToBottom(): void {
  if (conversationRef.value) {
    conversationRef.value.scrollTop = conversationRef.value.scrollHeight
  }
}

onMounted(initTask)
onUnmounted(() => {
  if (eventSource) eventSource.close()
})

// ---- 对话流分组:按 round_idx ----

interface RoundGroup {
  roundIdx: number
  label: string
  conversations: Conversation[]
}

const roundGroups = computed<RoundGroup[]>(() => {
  if (!task.value?.conversations) return []
  const groups = new Map<number, Conversation[]>()
  for (const c of task.value.conversations) {
    if (!groups.has(c.round_idx)) groups.set(c.round_idx, [])
    groups.get(c.round_idx)!.push(c)
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a - b)
    .map(([roundIdx, convs]) => ({
      roundIdx,
      label: roundIdx === 0 ? '初始评估' : `第 ${roundIdx} 轮`,
      conversations: convs.sort((a, b) => a.created_at.localeCompare(b.created_at)),
    }))
})

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

// ---- 对话消息元信息(角色/类型 → 展示) ----

interface MessageMeta {
  label: string
  variant: 'user-agent' | 'react-agent' | 'tool' | 'error' | 'summary'
}

function getMessageMeta(c: Conversation): MessageMeta {
  if (c.role === 'user_agent') {
    if (c.type === 'evaluation')
      return { label: 'user_agent 评估', variant: 'user-agent' }
    if (c.type === 'followup')
      return { label: 'user_agent 追问', variant: 'user-agent' }
    if (c.type === 'summary')
      return { label: '最终总结', variant: 'summary' }
    return { label: 'user_agent', variant: 'user-agent' }
  }
  if (c.role === 'react_agent') {
    if (c.type === 'thinking')
      return { label: 'react_agent 思考', variant: 'react-agent' }
    if (c.type === 'tool_call')
      return { label: '工具调用', variant: 'tool' }
    if (c.type === 'tool_result')
      return { label: '工具结果', variant: 'tool' }
    if (c.type === 'submit')
      return { label: '提交结果', variant: 'react-agent' }
    if (c.type === 'error')
      return { label: '错误', variant: 'error' }
    return { label: 'react_agent', variant: 'react-agent' }
  }
  return { label: c.role, variant: 'react-agent' }
}

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
      </template>
    </AppHeader>

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
          <div v-for="group in roundGroups" :key="group.roundIdx" class="round-group">
            <div class="round-label">{{ group.label }}</div>
            <div class="messages">
              <div
                v-for="msg in group.conversations"
                :key="msg.id"
                :class="['message', `msg-${getMessageMeta(msg).variant}`]"
              >
                <div class="msg-header">
                  <span class="msg-label">{{ getMessageMeta(msg).label }}</span>
                  <span class="msg-time">{{ formatTime(msg.created_at) }}</span>
                </div>
                <div class="msg-content">{{ msg.content }}</div>
              </div>
            </div>
          </div>
          <!-- 运行中等待提示 -->
          <div v-if="isRunning" class="waiting-hint">
            <span class="typing-dots">
              <span></span><span></span><span></span>
            </span>
            智能体思考中...
          </div>
        </section>
      </template>
    </main>
  </div>
</template>

<style scoped>
.page {
  min-height: 100vh;
  background: var(--color-bg);
}

.main {
  max-width: var(--content-width);
  margin: 0 auto;
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

.message {
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-lg);
  border-left: 3px solid transparent;
}

.msg-user-agent {
  background: var(--color-info-light);
  border-left-color: var(--color-info);
}

.msg-react-agent {
  background: #faf5ff;
  border-left-color: #a855f7;
}

.msg-tool {
  background: var(--color-surface-alt);
  border-left-color: var(--color-text-muted);
}

.msg-error {
  background: var(--color-danger-light);
  border-left-color: var(--color-danger);
}

.msg-summary {
  background: linear-gradient(135deg, #faf5ff 0%, #f0f4ff 100%);
  border-left-color: var(--color-primary);
}

.msg-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-2);
}

.msg-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}

.msg-summary .msg-label {
  color: var(--color-primary);
}

.msg-time {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.msg-content {
  font-size: var(--fs-sm);
  white-space: pre-wrap;
  word-break: break-word;
  line-height: var(--lh-relaxed);
}

.msg-tool .msg-content {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  max-height: 200px;
  overflow-y: auto;
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
