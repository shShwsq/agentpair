<script setup lang="ts">
/**
 * 练习题生成预览对话框(TaskDetailView「生成练习题」入口)
 *
 * 打开时调 POST /practice/generate 逐条 finding 出题(秒级~分钟级);
 * 生成的候选题(draft)逐题预览(题干/代码片段/选项/答案/解析),
 * 用户勾选保留后确认入库(转 active),未勾选的一并丢弃。
 */
import { computed, reactive, ref, watch } from 'vue'

import { confirmQuestions, generateQuestions } from '@/api/practice'
import { extractErrorMessage } from '@/utils/error'
import type { DraftQuestion } from '@/types/practice'

const props = defineProps<{
  open: boolean
  /** 来源任务 id(后端校验归属) */
  taskId: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
  /** 确认入库完成(confirmed=入库题数) */
  (e: 'confirmed', confirmed: number): void
}>()

type Phase = 'generating' | 'preview' | 'error' | 'confirming' | 'done'
const phase = ref<Phase>('generating')
const errorMsg = ref('')
const drafts = ref<DraftQuestion[]>([])
const skippedFindings = ref(0)
/** 勾选保留的题 id */
const selected = reactive<Set<string>>(new Set())
/** 确认结果(done 阶段展示) */
const confirmedCount = ref(0)

function resetState(): void {
  phase.value = 'generating'
  errorMsg.value = ''
  drafts.value = []
  selected.clear()
  skippedFindings.value = 0
  confirmedCount.value = 0
}

async function generate(): Promise<void> {
  phase.value = 'generating'
  errorMsg.value = ''
  try {
    const res = await generateQuestions({ task_id: props.taskId })
    drafts.value = res.questions
    skippedFindings.value = res.skipped_findings
    // 默认全部保留
    selected.clear()
    for (const q of res.questions) selected.add(q.id)
    phase.value = 'preview'
  } catch (err) {
    errorMsg.value = extractErrorMessage(err)
    phase.value = 'error'
  }
}

// open 变 true 时重新生成;关闭不做请求(未确认的 draft 保留,下次打开重新生成会因去重而减少)
watch(
  () => props.open,
  (isOpen) => {
    if (!isOpen) return
    resetState()
    generate()
  },
  { immediate: true },
)

function toggleQuestion(id: string): void {
  if (selected.has(id)) selected.delete(id)
  else selected.add(id)
}

function toggleAll(): void {
  if (selected.size === drafts.value.length) {
    selected.clear()
  } else {
    for (const q of drafts.value) selected.add(q.id)
  }
}

const allSelected = computed(
  () => drafts.value.length > 0 && selected.size === drafts.value.length,
)

/** 确认入库:只传勾选的 id,同一任务的其余 draft 由后端删除 */
async function handleConfirm(): Promise<void> {
  if (phase.value === 'confirming') return
  phase.value = 'confirming'
  errorMsg.value = ''
  try {
    const res = await confirmQuestions({
      task_id: props.taskId,
      question_ids: Array.from(selected),
    })
    confirmedCount.value = res.confirmed
    phase.value = 'done'
  } catch (err) {
    errorMsg.value = extractErrorMessage(err)
    phase.value = 'preview'
  }
}

function handleClose(): void {
  if (phase.value === 'confirming') return
  emit('close')
}

function formatDifficulty(d: number): string {
  return Number.isInteger(d) ? String(d) : d.toFixed(1)
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="dialog-mask" @click.self="handleClose">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <h3>生成练习题</h3>
            <button
              class="dialog-close"
              :disabled="phase === 'confirming'"
              aria-label="关闭"
              @click="handleClose"
            >×</button>
          </header>

          <div class="dialog-body">
            <!-- 生成中 -->
            <div v-if="phase === 'generating'" class="phase-block">
              <span class="status-spinner" />
              <p>正在基于审计发现出题,通常需要 1 分钟以内,请稍候...</p>
            </div>

            <!-- 失败 -->
            <div v-else-if="phase === 'error'" class="phase-block">
              <p class="error-text">生成失败:{{ errorMsg }}</p>
              <div class="phase-actions">
                <button class="btn-secondary" @click="handleClose">关闭</button>
                <button class="btn-primary" @click="generate">重试</button>
              </div>
            </div>

            <!-- 预览勾选 -->
            <template v-else-if="phase === 'preview' || phase === 'confirming'">
              <div class="preview-toolbar">
                <label class="select-all">
                  <input
                    type="checkbox"
                    :checked="allSelected"
                    :disabled="phase === 'confirming'"
                    @change="toggleAll"
                  />
                  全选
                </label>
                <span class="preview-count">
                  共 {{ drafts.length }} 题<template v-if="skippedFindings > 0"> · {{ skippedFindings }} 条发现未能出题</template>
                </span>
                <p v-if="errorMsg" class="error-text">{{ errorMsg }}</p>
              </div>
              <div class="draft-list">
                <div
                  v-for="(q, idx) in drafts"
                  :key="q.id"
                  :class="['draft-card', { 'draft-unselected': !selected.has(q.id) }]"
                >
                  <div class="draft-head">
                    <input
                      type="checkbox"
                      :checked="selected.has(q.id)"
                      :disabled="phase === 'confirming'"
                      title="保留该题"
                      @change="toggleQuestion(q.id)"
                    />
                    <span class="draft-no">题 {{ idx + 1 }}</span>
                    <span v-if="q.knowledge_name" class="tag tag-kp">{{ q.knowledge_name }}</span>
                    <span class="tag">{{ q.qtype === 'true_false' ? '判断' : '单选' }}</span>
                    <span class="tag">难度 {{ formatDifficulty(q.difficulty) }}</span>
                  </div>
                  <p class="draft-stem">{{ q.stem }}</p>
                  <pre v-if="q.code_snippet" class="code-snippet"><code>{{ q.code_snippet }}</code></pre>
                  <ol class="draft-options">
                    <li
                      v-for="(opt, oi) in q.options"
                      :key="oi"
                      :class="{ 'option-correct': oi === q.answer_idx }"
                    >
                      {{ opt }}<span v-if="oi === q.answer_idx" class="correct-mark">✓ 正确答案</span>
                    </li>
                  </ol>
                  <p v-if="q.explanation" class="draft-explanation">解析:{{ q.explanation }}</p>
                </div>
              </div>
            </template>

            <!-- 完成 -->
            <div v-else-if="phase === 'done'" class="phase-block">
              <p class="done-text">
                <template v-if="confirmedCount > 0">
                  已入库 {{ confirmedCount }} 题,可到「自适应练习」页开始练习
                </template>
                <template v-else>未保留任何题目,候选题已全部丢弃</template>
              </p>
              <div class="phase-actions">
                <button class="btn-primary" @click="handleClose">完成</button>
              </div>
            </div>
          </div>

          <footer
            v-if="phase === 'preview' || phase === 'confirming'"
            class="dialog-footer"
          >
            <span class="footer-hint">已勾选 {{ selected.size }} / {{ drafts.length }} 题</span>
            <div class="footer-actions">
              <button class="btn-secondary" :disabled="phase === 'confirming'" @click="handleClose">
                取消
              </button>
              <button class="btn-primary" :disabled="phase === 'confirming'" @click="handleConfirm">
                {{ phase === 'confirming' ? '入库中...' : (selected.size === 0 ? '全部丢弃' : `确认入库(${selected.size})`) }}
              </button>
            </div>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.dialog-mask {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-6);
  background: rgba(0, 0, 0, 0.45);
}

.dialog-card {
  display: flex;
  flex-direction: column;
  width: 100%;
  max-width: 720px;
  max-height: 86vh;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.dialog-header h3 {
  margin: 0;
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.dialog-close {
  width: 28px;
  height: 28px;
  font-size: var(--fs-lg);
  line-height: 1;
  color: var(--color-text-muted);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.dialog-close:hover:not(:disabled) {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.dialog-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--space-4) var(--space-5);
}

.phase-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-8) var(--space-4);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  text-align: center;
}

.phase-block p {
  margin: 0;
  line-height: var(--lh-relaxed);
}

.phase-actions {
  display: flex;
  gap: var(--space-3);
}

.error-text {
  color: var(--color-danger);
}

.done-text {
  font-size: var(--fs-base);
  color: var(--color-text);
}

.preview-toolbar {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  margin-bottom: var(--space-3);
  flex-wrap: wrap;
}

.select-all {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text);
  cursor: pointer;
}

.preview-count {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.preview-toolbar .error-text {
  width: 100%;
  margin: 0;
  font-size: var(--fs-xs);
}

.draft-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.draft-card {
  padding: var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg);
}

.draft-unselected {
  opacity: 0.55;
}

.draft-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}

.draft-no {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-muted);
}

.tag {
  display: inline-flex;
  align-items: center;
  padding: 2px var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
  white-space: nowrap;
}

.tag-kp {
  color: var(--color-primary);
  background: var(--color-primary-light);
}

.draft-stem {
  margin: 0 0 var(--space-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
  word-break: break-word;
}

.code-snippet {
  margin: 0 0 var(--space-2);
  padding: var(--space-3);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  overflow-x: auto;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
}

.draft-options {
  margin: 0 0 var(--space-2);
  padding-left: var(--space-5);
  font-size: var(--fs-sm);
  color: var(--color-text);
}

.draft-options li {
  margin: var(--space-1) 0;
  line-height: var(--lh-base);
}

.option-correct {
  color: var(--color-success);
  font-weight: var(--fw-medium);
}

.correct-mark {
  margin-left: var(--space-2);
  font-size: var(--fs-xs);
}

.draft-explanation {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
}

.footer-hint {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.footer-actions {
  display: flex;
  gap: var(--space-3);
}

.btn-primary,
.btn-secondary {
  display: inline-flex;
  align-items: center;
  height: 34px;
  padding: 0 var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
}

.btn-primary {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-secondary {
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-color: var(--color-border);
}

.btn-secondary:hover:not(:disabled) {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.btn-primary:disabled,
.btn-secondary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.status-spinner {
  display: inline-block;
  width: 18px;
  height: 18px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: pg-spin 0.8s linear infinite;
}

@keyframes pg-spin {
  to { transform: rotate(360deg); }
}

.dialog-fade-enter-active,
.dialog-fade-leave-active {
  transition: opacity var(--transition-base);
}

.dialog-fade-enter-from,
.dialog-fade-leave-to {
  opacity: 0;
}
</style>
