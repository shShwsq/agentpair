<script setup lang="ts">
/**
 * 设置页
 *
 * 两个配置区:
 * 1. 修改密码(已登录用户;OAuth 用户未设密码时只输新密码)
 * 2. 绑定/解绑 GitHub(用于任务创建时访问私有仓库)
 *
 * GitHub 绑定流程:
 * - 点击"绑定 GitHub"跳转 GitHub 授权页(scope=user:email repo)
 * - 授权后回调到 /auth/github/callback
 * - OAuthCallbackView 检测到用户已登录 → 调 POST /github/bind 完成绑定
 * - 未登录场景走原登录流程(避免混淆)
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import { changePassword } from '@/api/auth'
import { getGitHubBindURL, getGitHubStatus, unbindGitHub } from '@/api/github'
import type { GitHubStatus } from '@/types/github'
import { useAuthStore } from '@/stores/auth'
import { extractErrorMessage } from '@/utils/error'

const router = useRouter()
const authStore = useAuthStore()

/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

/** OAuth 用户未设密码时为 false,此时表单跳过"当前密码"字段 */
const hasPassword = computed(() => authStore.user?.has_password ?? false)

const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const showPassword = ref(false)
const loading = ref(false)
const error = ref('')
const success = ref('')

const currentPasswordError = computed(() => {
  if (!hasPassword.value) return ''
  if (!currentPassword.value) return '请输入当前密码'
  return ''
})

const newPasswordError = computed(() => {
  if (!newPassword.value) return '请输入新密码'
  if (newPassword.value.length < 8) return '密码至少 8 位'
  if (hasPassword.value && currentPassword.value && newPassword.value === currentPassword.value) {
    return '新密码不能与当前密码相同'
  }
  if (confirmPassword.value && newPassword.value !== confirmPassword.value) {
    return '两次输入的密码不一致'
  }
  return ''
})

const confirmError = computed(() => {
  if (!confirmPassword.value) return '请确认密码'
  if (newPassword.value !== confirmPassword.value) return '两次输入的密码不一致'
  return ''
})

const canSubmit = computed(
  () => !currentPasswordError.value && !newPasswordError.value && !confirmError.value,
)

async function handleSubmit(): Promise<void> {
  error.value = ''
  success.value = ''
  if (!canSubmit.value) return

  loading.value = true
  try {
    const res = await changePassword({
      current_password: hasPassword.value ? currentPassword.value : undefined,
      new_password: newPassword.value,
    })
    success.value = res.message
    // 修改成功后登出并跳转登录页
    setTimeout(() => {
      authStore.logout()
      router.push('/login')
    }, 1500)
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}

// ============================================================
// GitHub 绑定区
// ============================================================

const githubStatus = ref<GitHubStatus | null>(null)
const githubLoading = ref(false)
const githubAction = ref<'bind' | 'unbind' | ''>('')
const githubError = ref('')
const githubSuccess = ref('')

async function refreshGitHubStatus(): Promise<void> {
  githubLoading.value = true
  githubError.value = ''
  try {
    githubStatus.value = await getGitHubStatus()
  } catch (err) {
    // 静默失败,不阻塞页面(只在控制台提示)
    console.warn('加载 GitHub 状态失败:', err)
  } finally {
    githubLoading.value = false
  }
}

function startBind(): void {
  // 跳到 GitHub 授权页(scope=user:email repo)
  // 用户授权后回调到 /auth/github/callback,OAuthCallbackView 检测已登录后调 bind API
  window.location.href = getGitHubBindURL()
}

async function startUnbind(): Promise<void> {
  githubAction.value = 'unbind'
  githubError.value = ''
  githubSuccess.value = ''
  try {
    githubStatus.value = await unbindGitHub()
    githubSuccess.value = '已解绑 GitHub,任务执行将无法访问你的私有仓库'
    setTimeout(() => (githubSuccess.value = ''), 5000)
  } catch (err) {
    githubError.value = extractErrorMessage(err)
  } finally {
    githubAction.value = ''
  }
}

onMounted(() => {
  refreshGitHubStatus()
})
</script>

<template>
  <div class="page">
    <AppHeader>
      <template #leading>
        <WorkspaceToggleButton
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
      </template>
    </AppHeader>

    <div class="page-body">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <main class="main">
      <div class="page-header">
        <div>
          <h1>设置</h1>
          <p class="subtitle">管理账号密码与 GitHub 集成</p>
        </div>
      </div>

      <section class="config-section">
        <div class="section-header">
          <div>
            <h2>{{ hasPassword ? '修改密码' : '设置密码' }}</h2>
            <p class="section-desc">
              {{ hasPassword
                ? '需先验证当前密码,新密码生效后需重新登录'
                : '当前账号通过 GitHub OAuth 登录,设置密码后可用邮箱密码登录' }}
            </p>
          </div>
        </div>

        <Transition name="fade">
          <div v-if="error" class="alert alert-error" role="alert">{{ error }}</div>
        </Transition>
        <Transition name="fade">
          <div v-if="success" class="alert alert-success" role="status">
            {{ success }}(将自动登出,请用新密码登录...)
          </div>
        </Transition>

        <form v-if="!success" @submit.prevent="handleSubmit" novalidate>
          <div v-if="hasPassword" class="field">
            <label for="current-password">当前密码</label>
            <div class="input-wrapper">
              <input
                id="current-password"
                v-model="currentPassword"
                :type="showPassword ? 'text' : 'password'"
                autocomplete="current-password"
                placeholder="输入当前密码"
                :class="{ invalid: currentPasswordError }"
              />
            </div>
            <span v-if="currentPasswordError" class="field-error">{{ currentPasswordError }}</span>
          </div>

          <div class="field">
            <label for="new-password">新密码</label>
            <div class="input-wrapper">
              <input
                id="new-password"
                v-model="newPassword"
                :type="showPassword ? 'text' : 'password'"
                autocomplete="new-password"
                placeholder="至少 8 位"
                :class="{ invalid: newPasswordError }"
              />
              <button
                type="button"
                class="toggle-pwd"
                :aria-label="showPassword ? '隐藏密码' : '显示密码'"
                @click="showPassword = !showPassword"
              >
                <svg v-if="!showPassword" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                  <circle cx="12" cy="12" r="3" />
                </svg>
                <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                  <line x1="1" y1="1" x2="23" y2="23" />
                </svg>
              </button>
            </div>
            <span v-if="newPasswordError" class="field-error">{{ newPasswordError }}</span>
          </div>

          <div class="field">
            <label for="confirm-password">确认密码</label>
            <input
              id="confirm-password"
              v-model="confirmPassword"
              :type="showPassword ? 'text' : 'password'"
              autocomplete="new-password"
              placeholder="再输入一次"
              :class="{ invalid: confirmError }"
            />
            <span v-if="confirmError" class="field-error">{{ confirmError }}</span>
          </div>

          <button type="submit" class="btn-primary" :disabled="loading || !canSubmit">
            <span v-if="loading" class="spinner-sm" />
            {{ loading ? '处理中...' : (hasPassword ? '修改密码' : '设置密码') }}
          </button>
        </form>
      </section>

      <!-- ============================================================ -->
      <!-- GitHub 账号绑定区                                            -->
      <!-- ============================================================ -->
      <section class="config-section github-section">
        <div class="section-header">
          <div>
            <h2>GitHub 账号绑定</h2>
            <p class="section-desc">
              绑定后可在任务创建时选择你的私有仓库,授权范围: user:email (读取邮箱) + repo (访问私有仓库)
            </p>
          </div>
        </div>

        <Transition name="fade">
          <div v-if="githubError" class="alert alert-error" role="alert">{{ githubError }}</div>
        </Transition>
        <Transition name="fade">
          <div v-if="githubSuccess" class="alert alert-success" role="status">{{ githubSuccess }}</div>
        </Transition>

        <div v-if="githubLoading" class="status-loading">
          <div class="spinner"></div>
          <p>加载中...</p>
        </div>

        <div v-else-if="githubStatus" class="github-status">
          <div v-if="githubStatus.bound" class="bound-status">
            <div class="avatar-container">
              <img v-if="githubStatus.avatar_url" :src="githubStatus.avatar_url" alt="GitHub 头像" class="avatar">
              <div v-else class="avatar-placeholder">GH</div>
            </div>
            <div class="user-info">
              <p class="login">@{{ githubStatus.github_login || 'unknown' }}</p>
              <p class="desc">已绑定 GitHub 账号,可访问私有仓库</p>
            </div>
            <button
              type="button"
              class="btn-danger"
              :disabled="githubAction === 'unbind'"
              @click="startUnbind"
            >
              <span v-if="githubAction === 'unbind'" class="spinner-sm"></span>
              {{ githubAction === 'unbind' ? '解绑中...' : '解绑 GitHub' }}
            </button>
          </div>

          <div v-else class="unbound-status">
            <div class="illustration">
              <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>
              </svg>
            </div>
            <p class="desc">未绑定 GitHub 账号,无法访问私有仓库</p>
            <button
              type="button"
              class="btn-primary"
              @click="startBind"
            >
              绑定 GitHub
            </button>
          </div>
        </div>

        <div v-else class="status-error">
          <p>加载 GitHub 状态失败,请刷新页面重试</p>
          <button type="button" class="btn-text" @click="refreshGitHubStatus">
            刷新
          </button>
        </div>
      </section>
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
  max-width: 560px;
  margin: 0 auto;
  overflow-y: auto;
  padding: var(--space-8) var(--space-6) var(--space-12);
}

/* ---- 页头 ---- */
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-6);
}

.page-header h1 {
  font-size: var(--fs-xl);
  margin-bottom: var(--space-1);
}

.subtitle {
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
}

/* ---- 配置区(复用 ModelSettingsView 风格) ---- */
.config-section {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  padding: var(--space-6);
}

.section-header {
  margin-bottom: var(--space-4);
}

.section-header h2 {
  font-size: var(--fs-lg);
}

.section-desc {
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
  margin: var(--space-1) 0 0;
}

/* ---- 提示 ---- */
.alert {
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  margin-bottom: var(--space-4);
}

.alert-success {
  background: var(--color-success-light);
  color: var(--color-success);
  border: 1px solid #bbf7d0;
}

.alert-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border: 1px solid #fecaca;
}

/* ---- 表单字段(复用 ResetPasswordView 风格) ---- */
.field {
  margin-bottom: var(--space-4);
}

.field label {
  display: block;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  margin-bottom: var(--space-2);
}

.field input {
  width: 100%;
  height: 42px;
  padding: 0 var(--space-3);
  font-size: var(--fs-base);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.field input::placeholder {
  color: var(--color-text-muted);
}

.field input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
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

/* ---- 按钮 ---- */
.btn-primary {
  width: 100%;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: white;
  background: var(--color-primary);
  border: none;
  border-radius: var(--radius-md);
  transition: background var(--transition-fast);
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.spinner-sm {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* ---- 过渡 ---- */
.fade-enter-active,
.fade-leave-active {
  transition: opacity var(--transition-fast);
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

/* ============================================================ */
/* GitHub 绑定区                                                */
/* ============================================================ */
.github-section {
  margin-top: var(--space-6);
}

.status-loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-8) 0;
  color: var(--color-text-secondary);
}

.spinner {
  display: inline-block;
  width: 28px;
  height: 28px;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.github-status {
  padding: var(--space-2) 0;
}

/* ---- 已绑定状态 ---- */
.bound-status {
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
  margin-bottom: var(--space-1);
  word-break: break-all;
}

.user-info .desc {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  margin: 0;
}

/* ---- 未绑定状态 ---- */
.unbound-status {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-6) var(--space-4);
  text-align: center;
}

.illustration {
  color: var(--color-text-muted);
  opacity: 0.6;
}

.unbound-status .desc {
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
  margin: 0;
}

.unbound-status .btn-primary {
  width: auto;
  padding: 0 var(--space-6);
  margin-top: var(--space-2);
}

/* ---- 加载失败状态 ---- */
.status-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-6) 0;
  text-align: center;
  color: var(--color-text-secondary);
}

/* ---- 危险按钮(解绑) ---- */
.btn-danger {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  height: 36px;
  padding: 0 var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-danger);
  background: transparent;
  border: 1px solid var(--color-danger);
  border-radius: var(--radius-md);
  transition: background var(--transition-fast), color var(--transition-fast);
}

.btn-danger:hover:not(:disabled) {
  background: var(--color-danger);
  color: white;
}

.btn-danger:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn-danger .spinner-sm {
  border-color: rgba(220, 38, 38, 0.3);
  border-top-color: var(--color-danger);
}

/* ---- 文本按钮(刷新) ---- */
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
</style>
