<script setup lang="ts">
/**
 * GitHub OAuth 回调处理页
 *
 * 流程:
 * 1. GitHub 授权后跳转到此页,URL 带 ?code=XXX(或 ?error=access_denied)
 * 2. 提取 code 调后端 /auth/oauth/github
 * 3. 成功 → 跳首页;失败 → 显示错误 + 返回登录链接
 */
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { extractErrorMessage } from '@/utils/error'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const status = ref<'loading' | 'error'>('loading')
const errorMsg = ref('')

onMounted(async () => {
  const code = route.query.code as string | undefined
  const ghError = route.query.error as string | undefined

  // 用户拒绝授权
  if (ghError) {
    status.value = 'error'
    errorMsg.value = 'GitHub 授权已取消'
    return
  }

  if (!code) {
    status.value = 'error'
    errorMsg.value = '回调参数缺少 code'
    return
  }

  try {
    await authStore.handleGitHubCallback(code)
    await router.push('/')
  } catch (err) {
    status.value = 'error'
    errorMsg.value = extractErrorMessage(err)
  }
})
</script>

<template>
  <div class="callback-page">
    <!-- 加载中 -->
    <div v-if="status === 'loading'" class="callback-card">
      <div class="spinner-lg" />
      <p>正在完成 GitHub 登录...</p>
    </div>

    <!-- 失败 -->
    <div v-else class="callback-card">
      <div class="error-icon">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10" />
          <line x1="15" y1="9" x2="9" y2="15" />
          <line x1="9" y1="9" x2="15" y2="15" />
        </svg>
      </div>
      <p class="error-text">{{ errorMsg }}</p>
      <RouterLink to="/login" class="back-link">返回登录</RouterLink>
    </div>
  </div>
</template>

<style scoped>
.callback-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--color-bg);
}

.callback-card {
  text-align: center;
  padding: var(--space-8);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-md);
  max-width: 360px;
}

.callback-card p {
  margin-top: var(--space-4);
  color: var(--color-text-secondary);
}

.spinner-lg {
  width: 40px;
  height: 40px;
  margin: 0 auto;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.error-icon {
  color: var(--color-danger);
}

.error-text {
  color: var(--color-danger) !important;
  font-weight: var(--fw-medium);
}

.back-link {
  display: inline-block;
  margin-top: var(--space-5);
  font-size: var(--fs-sm);
}
</style>
