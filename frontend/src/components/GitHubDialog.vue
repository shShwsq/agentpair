<script setup lang="ts">
/**
 * GitHub 账号管理 弹窗
 *
 * 复用 QuestionDialog / ModelConfigDialog 的视觉语言(mask + card + header/body/footer)。
 * - 已绑定:展示头像 + 用户名,footer 提供「解绑」按钮
 * - 未绑定:展示说明 + 授权范围,footer 提供「绑定 GitHub」按钮
 * - 加载中:展示 spinner
 *
 * 实际绑定(跳转授权页)/解绑(调 API)由父组件处理,本组件仅 emit 事件。
 */
import type { GitHubStatus } from '@/types/github'

const props = defineProps<{
  /** 是否显示 */
  open: boolean
  /** GitHub 绑定状态(父组件加载) */
  status: GitHubStatus | null
  /** 状态加载中 */
  loading: boolean
  /** 操作进行中('bind' | 'unbind'),用于禁用按钮 + spinner */
  action: 'bind' | 'unbind' | ''
  /** 错误信息 */
  error?: string
  /** 成功提示(解绑后) */
  success?: string
}>()

const emit = defineEmits<{
  (e: 'bind'): void
  (e: 'unbind'): void
  (e: 'cancel'): void
}>()

function handleCancel(): void {
  if (props.action) return // 操作进行中不允许关闭
  emit('cancel')
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-fade">
      <div v-if="open" class="dialog-mask" @click.self="handleCancel">
        <div class="dialog-card" role="dialog" aria-modal="true">
          <header class="dialog-header">
            <h3>GitHub 账号</h3>
            <button
              class="dialog-close"
              :disabled="!!action"
              aria-label="关闭"
              @click="handleCancel"
            >×</button>
          </header>

          <div class="dialog-body">
            <!-- 加载中 -->
            <div v-if="loading" class="state-block">
              <div class="spinner" />
              <p class="state-text">加载中...</p>
            </div>

            <template v-else-if="status">
              <!-- 已绑定 -->
              <div v-if="status.bound" class="bound-card">
                <div class="avatar-container">
                  <img
                    v-if="status.avatar_url"
                    :src="status.avatar_url"
                    alt="GitHub 头像"
                    class="avatar"
                  />
                  <div v-else class="avatar-placeholder">GH</div>
                </div>
                <div class="user-info">
                  <p class="login">@{{ status.github_login || 'unknown' }}</p>
                  <p class="desc">已绑定,任务执行可访问你的私有仓库</p>
                </div>
              </div>

              <!-- 未绑定 -->
              <div v-else class="unbound-card">
                <div class="illustration">
                  <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
                  </svg>
                </div>
                <p class="state-text">未绑定 GitHub 账号</p>
                <p class="scope-text">
                  授权范围:user:email(读取邮箱)+ repo(访问私有仓库)<br />
                  绑定后可在提交任务时选择你的私有仓库
                </p>
              </div>
            </template>

            <!-- 加载失败 -->
            <div v-else class="state-block">
              <p class="state-text">加载 GitHub 状态失败</p>
              <button class="btn-text" @click="emit('cancel')">关闭后重试</button>
            </div>
          </div>

          <footer class="dialog-footer">
            <span v-if="error" class="validation-error">{{ error }}</span>
            <span v-else-if="success" class="validation-success">{{ success }}</span>
            <span v-else></span>
            <div class="footer-actions">
              <button
                class="btn btn-secondary"
                :disabled="!!action"
                @click="handleCancel"
              >关闭</button>
              <!-- 已绑定:解绑 -->
              <button
                v-if="status && status.bound"
                class="btn btn-danger"
                :disabled="action === 'unbind'"
                @click="emit('unbind')"
              >
                <span v-if="action === 'unbind'" class="btn-spinner danger" />
                {{ action === 'unbind' ? '解绑中...' : '解绑 GitHub' }}
              </button>
              <!-- 未绑定:绑定 -->
              <button
                v-else-if="status && !status.bound"
                class="btn btn-github"
                :disabled="action === 'bind'"
                @click="emit('bind')"
              >
                <span v-if="action === 'bind'" class="btn-spinner" />
                {{ action === 'bind' ? '跳转中...' : '绑定 GitHub' }}
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

/* ---- 通用状态块(加载/失败) ---- */
.state-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-6) 0;
  text-align: center;
}

.state-text {
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
  margin: 0;
}

.spinner {
  width: 28px;
  height: 28px;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* ---- 已绑定卡片 ---- */
.bound-card {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-4);
  background: var(--color-success-light);
  border: 1px solid #bbf7d0;
  border-radius: var(--radius-md);
}

.avatar-container {
  flex-shrink: 0;
}

.avatar {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  border: 2px solid var(--color-surface);
  box-shadow: var(--shadow-sm);
}

.avatar-placeholder {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-primary);
  color: white;
  font-weight: var(--fw-semibold);
  font-size: var(--fs-sm);
}

.user-info {
  flex: 1;
  min-width: 0;
}

.user-info .login {
  font-weight: var(--fw-semibold);
  font-size: var(--fs-base);
  color: var(--color-text);
  margin: 0 0 var(--space-1);
  word-break: break-all;
}

.user-info .desc {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  margin: 0;
}

/* ---- 未绑定卡片 ---- */
.unbound-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4) var(--space-2);
  text-align: center;
}

.illustration {
  color: var(--color-text-muted);
  opacity: 0.6;
}

.scope-text {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  line-height: var(--lh-relaxed);
  margin: 0;
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

.validation-success {
  font-size: var(--fs-sm);
  color: var(--color-success);
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

/* GitHub 品牌色按钮 */
.btn-github {
  background: var(--color-github);
  color: white;
}

.btn-github:hover:not(:disabled) {
  background: var(--color-github-hover);
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

.btn-text {
  background: transparent;
  border: none;
  color: var(--color-primary);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  cursor: pointer;
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-sm);
  transition: background var(--transition-fast);
}

.btn-text:hover {
  background: var(--color-primary-light);
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
