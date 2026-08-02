<script setup lang="ts">
/**
 * GitHub OAuth 回调处理页
 *
 * 同一个 redirect_uri 承担两种场景,按当前登录状态分流:
 * - 已登录:用户从设置页"绑定 GitHub"过来 → 调 POST /github/bind 写入 access_token
 * - 未登录:用户从登录页"GitHub 登录"过来 → 调 POST /auth/oauth/github 走登录
 *
 * 区分依据:authStore.isAuthenticated(页面刷新场景由路由守卫已 fetchMe 恢复)
 */
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { bindGitHub } from '@/api/github'
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
    if (authStore.isAuthenticated) {
      // 已登录 → 绑定流程:用 code 换 token 并加密落库
      const res = await bindGitHub({ code })
      // 邮箱不一致时带 query 跳设置页,由设置页弹窗询问是否同步
      if (res.email_mismatch && res.github_email) {
        await router.push({
          path: '/settings',
          query: {
            email_mismatch: '1',
            github_email: res.github_email,
            current_email: res.current_email ?? '',
          },
        })
      } else {
        // 绑定成功跳回设置页(用户能看到绑定状态)
        await router.push('/settings')
      }
    } else {
      // 未登录 → 登录流程:用 code 换 token + 创建/关联账号
      await authStore.handleGitHubCallback(code)
      await router.push('/')
    }
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
