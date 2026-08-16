<script setup lang="ts">
/**
 * 练习设置弹窗
 *
 * 复用 PasswordDialog 的视觉语言(mask + card + header/body/footer)。
 * 目前只有一项:任务完成后自动生成练习题开关(全局生效,
 * 产出的候选题仍需在任务详情页预览确认才入库)。
 *
 * 开关切换即保存:emit toggle 由父组件调 API 持久化并 toast,
 * 父组件更新 autoGenerate 后弹窗内状态同步。
 */
defineProps<{
  /** 是否显示 */
  open: boolean
  /** 自动生成练习题当前开关状态 */
  autoGenerate: boolean
  /** 保存中状态(禁用开关与关闭) */
  loading: boolean
  /** 错误信息(父组件 API 失败时传入) */
  error?: string
}>()

const emit = defineEmits<{
  (e: 'toggle'): void
  (e: 'cancel'): void
}>()

function handleToggle(loading: boolean): void {
  if (loading) return
  emit('toggle')
}

function handleCancel(loading: boolean): void {
  if (loading) return // 保存中不允许关闭
  emit('cancel')
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="dialog-mask" @click.self="handleCancel(loading)">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <h3>练习设置</h3>
            <button
              class="dialog-close"
              :disabled="loading"
              aria-label="关闭"
              @click="handleCancel(loading)"
            >×</button>
          </header>

          <div class="dialog-body">
            <div class="setting-row" @click="handleToggle(loading)">
              <div class="setting-info">
                <span class="setting-title">自动生成练习题</span>
                <span class="setting-desc">
                  审计任务完成后自动生成练习题候选题(全局生效,仍需预览确认才入库)
                </span>
              </div>
              <button
                type="button"
                role="switch"
                :aria-checked="autoGenerate"
                :class="['switch', { 'switch-on': autoGenerate }]"
                :disabled="loading"
                @click.stop="handleToggle(loading)"
              >
                <span class="switch-thumb" />
              </button>
            </div>
            <p v-if="loading" class="saving-hint">
              <span class="btn-spinner" /> 保存中...
            </p>
          </div>

          <footer class="dialog-footer">
            <span v-if="error" class="validation-error">{{ error }}</span>
            <span v-else></span>
            <div class="footer-actions">
              <button
                class="btn btn-primary"
                :disabled="loading"
                @click="handleCancel(loading)"
              >完成</button>
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

/* ---- 设置项 ---- */
.setting-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: border-color var(--transition-fast);
}

.setting-row:hover {
  border-color: var(--color-border-strong);
}

.setting-info {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

.setting-title {
  font-size: var(--fs-base);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

.setting-desc {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

/* ---- 开关 ---- */
.switch {
  flex-shrink: 0;
  position: relative;
  width: 40px;
  height: 22px;
  margin-top: 2px;
  border: none;
  border-radius: var(--radius-full);
  background: var(--color-border-strong);
  cursor: pointer;
  padding: 0;
  transition: background var(--transition-fast);
}

.switch-on {
  background: var(--color-primary);
}

.switch:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.switch-thumb {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #fff;
  transition: transform var(--transition-fast);
}

.switch-on .switch-thumb {
  transform: translateX(18px);
}

.saving-hint {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: var(--space-3) 0 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
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
  color: var(--color-text-inverse);
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, currentColor 30%, transparent);
  border-top-color: currentColor;
  border-radius: 50%;
  animation: btn-spin 0.8s linear infinite;
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
