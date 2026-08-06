<script setup lang="ts">
/**
 * 分项目记忆编辑弹窗
 *
 * 用户手动编辑单个项目的 alias/note/memory_content。
 * 项目本身(repo_url)由 orchestrator 在任务完成时自动归纳创建,
 * 此弹窗只做"编辑已有项目记忆",不新建。
 *
 * 草稿在 open=true 时从 props.project 重置;确定时 emit save,由父组件调 API 持久化。
 * 删除按钮 emit delete,由父组件调 DELETE 后关闭弹窗。
 *
 * 复用 PasswordDialog / ModelConfigDialog 的视觉语言(mask + card + header/body/footer)。
 */
import { reactive, watch } from 'vue'

import type { ProjectOut, SaveProjectRequest } from '@/types/memory'

const props = defineProps<{
  /** 是否显示 */
  open: boolean
  /** 当前编辑的项目(为 null 时弹窗不渲染内容) */
  project: ProjectOut | null
  /** 保存中状态(禁用按钮 + spinner) */
  loading: boolean
  /** 删除中状态(禁用删除按钮 + spinner) */
  deleting?: boolean
  /** 错误信息(父组件 API 失败时传入) */
  error?: string
}>()

const emit = defineEmits<{
  (e: 'save', payload: SaveProjectRequest): void
  (e: 'delete'): void
  (e: 'cancel'): void
}>()

/** 草稿(alias/note/memory_content 三字段可编辑) */
const draft = reactive({
  alias: '',
  note: '',
  memory_content: '',
})

/** memory_content 上限(与后端 schema 一致) */
const MAX_MEMORY = 20000
/** alias 上限(与后端 schema 一致) */
const MAX_ALIAS = 255

/** open 变 true 且 project 存在时,从 project 重置草稿 */
watch(
  () => [props.open, props.project] as const,
  ([isOpen, project]) => {
    if (!isOpen || !project) return
    draft.alias = project.alias ?? ''
    draft.note = project.note ?? ''
    draft.memory_content = project.memory_content ?? ''
  },
  { immediate: true },
)

const memoryOverLimit = () => draft.memory_content.length > MAX_MEMORY
const aliasOverLimit = () => draft.alias.length > MAX_ALIAS

const canSubmit = () =>
  !props.loading &&
  !props.deleting &&
  !memoryOverLimit() &&
  !aliasOverLimit()

function handleSubmit(): void {
  if (!canSubmit()) return
  emit('save', {
    alias: draft.alias.trim() || null,
    note: draft.note.trim() || null,
    memory_content: draft.memory_content,
  })
}

function handleDelete(): void {
  if (props.deleting) return
  emit('delete')
}

function handleCancel(): void {
  if (props.loading || props.deleting) return // 提交/删除中不允许取消
  emit('cancel')
}

/** 格式化时间戳展示(空则返回占位) */
function formatTime(iso: string | null): string {
  if (!iso) return '从未归纳'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open && project" class="dialog-mask" @click.self="handleCancel">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <div class="dialog-title">
              <h3>编辑项目记忆</h3>
              <span class="dialog-subtitle" :title="project.repo_url_raw">
                {{ project.alias || project.repo_url_raw }}
              </span>
            </div>
            <button
              class="dialog-close"
              :disabled="loading || deleting"
              aria-label="关闭"
              @click="handleCancel"
            >×</button>
          </header>

          <div class="dialog-body">
            <!-- 仓库信息(只读) -->
            <div class="readonly-info">
              <div class="info-row">
                <span class="info-label">仓库地址</span>
                <span class="info-value mono">{{ project.repo_url_raw }}</span>
              </div>
              <div class="info-row">
                <span class="info-label">上次自动归纳</span>
                <span class="info-value">{{ formatTime(project.last_summary_at) }}</span>
              </div>
            </div>

            <p class="dialog-tip">
              此记忆会在该仓库的任务执行时注入执行代理(react_agent),影响审计方向。
              可手动补充已知问题、重点关注路径、历史踩坑等;任务完成后也会自动归纳合并。
            </p>

            <!-- alias -->
            <div class="field">
              <label for="proj-alias">项目别名</label>
              <input
                id="proj-alias"
                v-model="draft.alias"
                type="text"
                placeholder="便于识别的名称,如「主仓库」"
                :class="{ invalid: aliasOverLimit() }"
                :disabled="loading || deleting"
                :maxlength="MAX_ALIAS"
              />
              <span v-if="aliasOverLimit()" class="field-error">别名不能超过 {{ MAX_ALIAS }} 字符</span>
            </div>

            <!-- note -->
            <div class="field">
              <label for="proj-note">备注</label>
              <textarea
                id="proj-note"
                v-model="draft.note"
                rows="2"
                placeholder="对该项目的补充说明(可选)"
                :disabled="loading || deleting"
              />
            </div>

            <!-- memory_content -->
            <div class="field">
              <label for="proj-memory">
                项目记忆正文
                <span class="char-count" :class="{ over: memoryOverLimit() }">
                  {{ draft.memory_content.length }} / {{ MAX_MEMORY }}
                </span>
              </label>
              <textarea
                id="proj-memory"
                v-model="draft.memory_content"
                rows="12"
                placeholder="记录该项目的已知问题、重点关注、历史踩坑等。任务完成时会自动归纳合并新发现。"
                :class="{ invalid: memoryOverLimit() }"
                :disabled="loading || deleting"
              />
              <span v-if="memoryOverLimit()" class="field-error">正文不能超过 {{ MAX_MEMORY }} 字符</span>
            </div>
          </div>

          <footer class="dialog-footer">
            <span v-if="error" class="validation-error">{{ error }}</span>
            <span v-else></span>
            <div class="footer-actions">
              <button
                class="btn btn-danger"
                :disabled="loading || deleting"
                @click="handleDelete"
              >
                <span v-if="deleting" class="btn-spinner danger" />
                {{ deleting ? '删除中...' : '删除' }}
              </button>
              <button
                class="btn btn-secondary"
                :disabled="loading || deleting"
                @click="handleCancel"
              >取消</button>
              <button
                class="btn btn-primary"
                :disabled="!canSubmit()"
                @click="handleSubmit"
              >
                <span v-if="loading" class="btn-spinner" />
                {{ loading ? '保存中...' : '保存' }}
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
  background: rgba(15, 23, 42, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: var(--space-4);
}

.dialog-card {
  background: var(--color-surface);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl);
  width: 100%;
  max-width: 640px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.dialog-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.dialog-title {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.dialog-title h3 {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  margin: 0;
  color: var(--color-text);
}

.dialog-subtitle {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  font-family: var(--font-mono);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 460px;
}

.dialog-close {
  background: none;
  border: none;
  font-size: 24px;
  line-height: 1;
  color: var(--color-text-muted);
  cursor: pointer;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
  flex-shrink: 0;
}

.dialog-close:hover:not(:disabled) {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.dialog-close:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.dialog-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-5);
}

/* 只读信息块 */
.readonly-info {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  margin-bottom: var(--space-4);
}

.info-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  font-size: var(--fs-sm);
}

.info-label {
  flex-shrink: 0;
  width: 84px;
  color: var(--color-text-secondary);
  font-size: var(--fs-xs);
}

.info-value {
  color: var(--color-text);
  word-break: break-all;
  min-width: 0;
}

.mono {
  font-family: var(--font-mono);
}

.dialog-tip {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  padding: var(--space-3);
  border-radius: var(--radius-md);
  border-left: 3px solid var(--color-primary);
  margin: 0 0 var(--space-4);
  line-height: var(--lh-relaxed);
}

/* ---- 表单字段 ---- */
.field {
  margin-bottom: var(--space-4);
}

.field:last-child {
  margin-bottom: 0;
}

.field label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  margin-bottom: var(--space-2);
  color: var(--color-text);
}

.char-count {
  font-size: var(--fs-xs);
  font-weight: var(--fw-regular);
  color: var(--color-text-muted);
}

.char-count.over {
  color: var(--color-danger);
  font-weight: var(--fw-medium);
}

.field input,
.field textarea {
  width: 100%;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-base);
  font-family: inherit;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.field textarea {
  resize: vertical;
  line-height: var(--lh-relaxed);
  min-height: 80px;
}

.field input::placeholder,
.field textarea::placeholder {
  color: var(--color-text-muted);
}

.field input:focus,
.field textarea:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.field input:disabled,
.field textarea:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.field input.invalid,
.field textarea.invalid {
  border-color: var(--color-danger);
}

.field-error {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

/* ---- footer ---- */
.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
}

.validation-error {
  font-size: var(--fs-sm);
  color: var(--color-danger);
  flex: 1;
  word-break: break-word;
}

.footer-actions {
  display: flex;
  gap: var(--space-2);
  flex-shrink: 0;
}

.btn {
  height: 38px;
  padding: 0 var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-primary {
  background: var(--color-primary);
  color: white;
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
  border-color: var(--color-border-strong);
  color: var(--color-text);
}

.btn-danger {
  background: var(--color-surface);
  color: var(--color-danger);
  border-color: var(--color-border);
}

.btn-danger:hover:not(:disabled) {
  background: var(--color-danger-light);
  border-color: var(--color-danger);
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: btn-spin 0.8s linear infinite;
}

.btn-spinner.danger {
  border-color: rgba(239, 68, 68, 0.3);
  border-top-color: var(--color-danger);
}

@keyframes btn-spin {
  to { transform: rotate(360deg); }
}

/* 弹窗淡入淡出 */
.dialog-fade-enter-active,
.dialog-fade-leave-active {
  transition: opacity 0.2s ease;
}

.dialog-fade-enter-from,
.dialog-fade-leave-to {
  opacity: 0;
}
</style>
