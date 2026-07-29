<script setup lang="ts">
/**
 * 协作对话流中的单条消息卡片
 *
 * 从 TaskDetailView 抽出,用于复用:平铺消息(user_agent 评估/追问/总结、user 指令)、
 * 迭代内的 thinking 项、工具调用/结果项、submit 项等。
 *
 * 渲染规则:
 * - 流式项(is_streaming=true):显示 reasoning(可折叠) + content,流式期间有打字机提示
 * - 正式对话项(is_streaming=false):仅显示 content
 *
 * reasoning 折叠状态由父组件管理(实时流式项的状态在父的 streamingItems Map 里),
 * 通过 toggle-reasoning 事件通知父组件切换。
 */
import { computed, ref } from 'vue'

interface StreamingItem {
  conv_id: string
  reasoning: string
  content: string
  status: 'streaming' | 'done' | 'error'
  reasoning_expanded: boolean
  role?: 'react_agent' | 'user_agent'
}

interface DisplayItem {
  id: string
  created_at: string
  is_streaming: boolean
  role?: string
  type?: string
  content?: string
  /** 完整评估(可折叠回看,如 user_agent evaluation 的覆盖情况+判断) */
  reasoning?: string | null
  streaming?: StreamingItem
}

const props = defineProps<{
  item: DisplayItem
}>()

const emit = defineEmits<{
  'toggle-reasoning': [convId: string]
}>()

interface MessageMeta {
  label: string
  variant: 'user-agent' | 'react-agent' | 'tool' | 'error' | 'summary' | 'streaming'
}

function getMessageMeta(item: DisplayItem): MessageMeta {
  if (item.is_streaming && item.streaming) {
    const role = item.streaming.role
    const roleLabel = role === 'user_agent' ? 'user_agent' : 'react_agent'
    if (item.streaming.status === 'streaming') {
      return { label: `${roleLabel} 思考中`, variant: 'streaming' }
    }
    return { label: `${roleLabel} 思考`, variant: 'streaming' }
  }

  if (item.role === 'user_agent') {
    if (item.type === 'evaluation')
      return { label: 'user_agent 评估', variant: 'user-agent' }
    if (item.type === 'followup')
      return { label: 'user_agent 追问', variant: 'user-agent' }
    if (item.type === 'summary')
      return { label: '最终总结', variant: 'summary' }
    return { label: 'user_agent', variant: 'user-agent' }
  }
  if (item.role === 'react_agent') {
    if (item.type === 'thinking')
      return { label: 'react_agent 思考', variant: 'react-agent' }
    if (item.type === 'tool_call')
      return { label: '工具调用', variant: 'tool' }
    if (item.type === 'tool_result')
      return { label: '工具结果', variant: 'tool' }
    if (item.type === 'submit')
      return { label: '提交结果', variant: 'react-agent' }
    if (item.type === 'error')
      return { label: '错误', variant: 'error' }
    return { label: 'react_agent', variant: 'react-agent' }
  }
  if (item.role === 'user') {
    return { label: '用户指令', variant: 'tool' }
  }
  return { label: item.role || 'unknown', variant: 'react-agent' }
}

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

const meta = computed(() => getMessageMeta(props.item))
const isActive = computed(
  () => !!(props.item.is_streaming && props.item.streaming?.status === 'streaming'),
)
const time = computed(() => formatTime(props.item.created_at))

/** 正式对话项的 reasoning(完整评估)折叠状态:默认折叠,点击展开 */
const evalExpanded = ref(false)
</script>

<template>
  <div :class="['message', `msg-${meta.variant}`, { 'msg-streaming-active': isActive }]">
    <div class="msg-header">
      <span class="msg-label">{{ meta.label }}</span>
      <span v-if="isActive" class="msg-streaming-tag">
        <span class="typing-dots">
          <span></span><span></span><span></span>
        </span>
      </span>
      <span class="msg-time">{{ time }}</span>
    </div>

    <!-- 流式思考项:显示 reasoning(可折叠) + content -->
    <template v-if="item.is_streaming && item.streaming">
      <div v-if="item.streaming.reasoning" class="msg-reasoning">
        <div
          class="msg-reasoning-header"
          @click="emit('toggle-reasoning', item.streaming.conv_id)"
        >
          <span class="msg-reasoning-toggle">
            {{ item.streaming.reasoning_expanded ? '▼' : '▶' }}
          </span>
          <span class="msg-reasoning-label">思考链</span>
          <span class="msg-reasoning-meta">
            {{ item.streaming.reasoning.length }} 字符
          </span>
        </div>
        <div
          v-if="item.streaming.reasoning_expanded"
          class="msg-reasoning-content"
        >{{ item.streaming.reasoning }}</div>
      </div>
      <div v-if="item.streaming.content" class="msg-content">{{ item.streaming.content }}</div>
      <div
        v-if="isActive && !item.streaming.reasoning && !item.streaming.content"
        class="msg-content msg-content-muted"
      >
        等待模型响应...
      </div>
    </template>

    <!-- 正式对话项:若有 reasoning(完整评估)则可折叠展示,默认只显示精简 content -->
    <template v-else>
      <div v-if="item.reasoning" class="msg-reasoning">
        <div
          class="msg-reasoning-header"
          @click="evalExpanded = !evalExpanded"
        >
          <span class="msg-reasoning-toggle">
            {{ evalExpanded ? '▼' : '▶' }}
          </span>
          <span class="msg-reasoning-label">完整评估</span>
          <span class="msg-reasoning-meta">
            {{ item.reasoning.length }} 字符
          </span>
        </div>
        <div v-if="evalExpanded" class="msg-reasoning-content">{{ item.reasoning }}</div>
      </div>
      <div v-if="item.content" class="msg-content">{{ item.content }}</div>
    </template>
  </div>
</template>

<style scoped>
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

.msg-streaming {
  background: linear-gradient(135deg, #fefce8 0%, #fef3c7 100%);
  border-left-color: #f59e0b;
  position: relative;
}

.msg-streaming-active {
  box-shadow: 0 0 0 2px rgba(245, 158, 11, 0.2);
  animation: streaming-pulse 2s ease-in-out infinite;
}

@keyframes streaming-pulse {
  0%, 100% { box-shadow: 0 0 0 2px rgba(245, 158, 11, 0.2); }
  50% { box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.35); }
}

.msg-streaming-tag {
  display: inline-flex;
  align-items: center;
  margin-left: var(--space-2);
}

.msg-streaming-tag .typing-dots span {
  background: #f59e0b;
}

.msg-reasoning {
  margin-bottom: var(--space-2);
  padding: var(--space-2) var(--space-3);
  background: rgba(255, 255, 255, 0.5);
  border-radius: var(--radius-md);
  border-left: 2px solid #d97706;
}

.msg-reasoning-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  cursor: pointer;
  user-select: none;
  padding: var(--space-1) 0;
  transition: background 0.15s ease;
}

.msg-reasoning-header:hover {
  background: rgba(245, 158, 11, 0.08);
  border-radius: var(--radius-sm);
}

.msg-reasoning-toggle {
  display: inline-block;
  width: 14px;
  font-size: var(--fs-xs);
  color: #92400e;
  text-align: center;
}

.msg-reasoning-label {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: #92400e;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.msg-reasoning-meta {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  margin-left: auto;
}

.msg-reasoning-content {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  font-style: italic;
  line-height: var(--lh-relaxed);
  max-height: 300px;
  overflow-y: auto;
  margin-top: var(--space-1);
  padding-top: var(--space-2);
  border-top: 1px dashed rgba(146, 64, 14, 0.2);
}

.msg-content-muted {
  color: var(--color-text-muted);
  font-style: italic;
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
