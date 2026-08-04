<script setup lang="ts">
/**
 * TRAE CLI PAT 管理 弹窗
 *
 * 复用 PasswordDialog / GitHubDialog 的视觉语言(mask + card + header/body/footer)。
 * - hasPat=true:展示"已设置"状态,输入新值覆盖,footer 提供「清除」+「更新」
 * - hasPat=false:首次设置,footer 提供「取消」+「保存」
 *
 * 草稿在 open=true 时重置;保存/清除由父组件调 API 持久化。
 * PAT 仅在沙箱内 trae_cli.yaml 中使用,加密存储于用户记录。
 */
import { computed, reactive, watch } from 'vue'

const props = defineProps<{
  /** 是否显示 */
  open: boolean
  /** 是否已设置 PAT(决定文案与是否显示「清除」按钮) */
  hasPat: boolean
  /** 提交中状态(禁用按钮 + spinner) */
  saving: boolean
  /** 错误信息(父组件 API 失败时传入) */
  error?: string
}>()

const emit = defineEmits<{
  (e: 'save', pat: string): void
  (e: 'clear'): void
  (e: 'cancel'): void
}>()

const draft = reactive({
  pat: '',
  showPat: false,
})

/** open 变 true 时重置草稿 */
watch(
  () => props.open,
  (isOpen) => {
    if (!isOpen) return
    draft.pat = ''
    draft.showPat = false
  },
  { immediate: true },
)

const patError = computed(() => {
  if (!draft.pat) return ''
  if (draft.pat.length < 8) return 'PAT 长度过短'
  return ''
})

const canSubmit = computed(
  () => !!draft.pat && !patError.value && !props.saving,
)

function handleSubmit(): void {
  if (!canSubmit.value) return
  emit('save', draft.pat.trim())
}

function handleClear(): void {
  if (props.saving) return
  emit('clear')
}

function handleCancel(): void {
  if (props.saving) return // 提交中不允许取消
  emit('cancel')
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="dialog-mask" @click.self="handleCancel">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <h3>TRAE CLI PAT</h3>
            <button
              class="dialog-close"
              :disabled="saving"
              aria-label="关闭"
              @click="handleCancel"
            >×</button>
          </header>

          <div class="dialog-body">
            <p class="dialog-tip">
              PAT 用于沙箱内 trae_cli.yaml 的认证,让 TRAE CLI 能访问你的模型配额。
              保存后加密存储,仅在任务执行时注入沙箱。
            </p>

            <!-- 当前状态 -->
            <div :class="['status-row', hasPat ? 'status-ok' : 'status-warn']">
              <span class="status-dot" />
              <span class="status-text">{{ hasPat ? '当前已设置 PAT' : '尚未设置 PAT' }}</span>
            </div>

            <div class="field">
              <label for="trae-pat">{{ hasPat ? '新 PAT(覆盖原值)' : 'PAT' }}</label>
              <div class="input-wrapper">
                <input
                  id="trae-pat"
                  v-model="draft.pat"
                  :type="draft.showPat ? 'text' : 'password'"
                  autocomplete="off"
                  :placeholder="hasPat ? '输入新 PAT 以覆盖' : '粘贴你的 TRAE CLI PAT'"
                  :class="{ invalid: patError }"
                  :disabled="saving"
                />
                <button
                  type="button"
                  class="toggle-pwd"
                  :aria-label="draft.showPat ? '隐藏' : '显示'"
                  @click="draft.showPat = !draft.showPat"
                >
                  <svg v-if="!draft.showPat" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                    <circle cx="12" cy="12" r="3" />
                  </svg>
                  <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                    <line x1="1" y1="1" x2="23" y2="23" />
                  </svg>
                </button>
              </div>
              <span v-if="patError" class="field-error">{{ patError }}</span>
            </div>
          </div>

          <footer class="dialog-footer">
            <span v-if="error" class="validation-error">{{ error }}</span>
            <span v-else></span>
            <div class="footer-actions">
              <button
                v-if="hasPat"
                class="btn btn-danger"
                :disabled="saving"
                @click="handleClear"
              >
                <span v-if="saving" class="btn-spinner danger" />
                清除
              </button>
              <button
                class="btn btn-secondary"
                :disabled="saving"
                @click="handleCancel"
              >取消</button>
              <button
                class="btn btn-primary"
                :disabled="!canSubmit"
                @click="handleSubmit"
              >
                <span v-if="saving" class="btn-spinner" />
                {{ saving ? '处理中...' : (hasPat ? '更新' : '保存') }}
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
  max-width: 460px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
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
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  margin: 0;
  color: var(--color-text);
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

/* ---- 状态行 ---- */
.status-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  margin-bottom: var(--space-4);
  font-size: var(--fs-sm);
}

.status-ok {
  background: var(--color-success-light);
  color: var(--color-success);
}

.status-warn {
  background: #fef3c7;
  color: #92400e;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}

.status-text {
  font-weight: var(--fw-medium);
}

/* ---- 表单字段 ---- */
.field {
  margin-bottom: var(--space-4);
}

.field:last-child {
  margin-bottom: 0;
}

.field label {
  display: block;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  margin-bottom: var(--space-2);
  color: var(--color-text);
}

.field input {
  width: 100%;
  height: 42px;
  padding: 0 var(--space-3);
  font-size: var(--fs-base);
  font-family: var(--font-mono);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.field input::placeholder {
  color: var(--color-text-muted);
  font-family: var(--font-base);
}

.field input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.field input:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.field input.invalid {
  border-color: var(--color-danger);
}

.input-wrapper {
  position: relative;
}

.input-wrapper input {
  padding-right: 44px;
}

.toggle-pwd {
  position: absolute;
  right: var(--space-2);
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  color: var(--color-text-muted);
  border: none;
  background: transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.toggle-pwd:hover {
  color: var(--color-text);
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
}

.footer-actions {
  display: flex;
  gap: var(--space-2);
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
  background: transparent;
  color: var(--color-danger);
  border-color: var(--color-danger);
}

.btn-danger:hover:not(:disabled) {
  background: var(--color-danger);
  color: white;
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
  border-color: rgba(220, 38, 38, 0.3);
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
