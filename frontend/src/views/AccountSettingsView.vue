<script setup lang="ts">
/**
 * 账号设置页
 *
 * 当前只提供修改密码功能。已登录用户进入此页:
 * - 普通用户(已设密码):需输入当前密码 + 新密码 + 确认
 * - OAuth 用户(未设密码):只需输入新密码 + 确认(相当于设置初始密码)
 *
 * 修改成功后自动登出并跳转登录页(出于安全考虑,旧 token 不再使用)。
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import { changePassword } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import { extractErrorMessage } from '@/utils/error'

const router = useRouter()
const authStore = useAuthStore()

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
</script>

<template>
  <div class="page">
    <AppHeader>
      <template #nav>
        <RouterLink to="/">首页</RouterLink>
        <RouterLink to="/tasks/new">提交任务</RouterLink>
        <RouterLink to="/settings">模型设置</RouterLink>
        <RouterLink to="/account" class="router-link-active">账号设置</RouterLink>
      </template>
    </AppHeader>

    <main class="main">
      <div class="page-header">
        <div>
          <h1>账号设置</h1>
          <p class="subtitle">管理账号密码与登录方式</p>
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
    </main>
  </div>
</template>

<style scoped>
.page {
  min-height: 100vh;
  background: var(--color-bg);
}

.main {
  max-width: 560px;
  margin: 0 auto;
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

/* ---- 配置区(复用 SettingsView 风格) ---- */
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
</style>
