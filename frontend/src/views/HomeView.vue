<script setup lang="ts">
/**
 * 首页(占位)
 *
 * 阶段 7 后续会扩展为任务列表页。当前仅展示登录状态 + 登出按钮,
 * 作为登录后的落地页,验证完整鉴权链路。
 */
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const authStore = useAuthStore()

function handleLogout(): void {
  authStore.logout()
  router.push('/login')
}
</script>

<template>
  <div class="home-page">
    <header class="home-header">
      <div class="header-inner">
        <div class="brand">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 2a4 4 0 0 1 4 4v1h1a3 3 0 0 1 3 3v3a3 3 0 0 1-3 3h-1v1a4 4 0 0 1-4 4" />
            <path d="M12 2a4 4 0 0 0-4 4v1H7a3 3 0 0 0-3 3v3a3 3 0 0 0 3 3h1v1a4 4 0 0 0 4 4" />
          </svg>
          <span>AgentPair</span>
        </div>
        <div class="user-area">
          <span class="user-email">{{ authStore.user?.email }}</span>
          <button class="btn-logout" @click="handleLogout">登出</button>
        </div>
      </div>
    </header>

    <main class="home-main">
      <div class="welcome-card">
        <div class="welcome-icon">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
            <polyline points="22 4 12 14.01 9 11.01" />
          </svg>
        </div>
        <h1>登录成功</h1>
        <p>欢迎回来,{{ authStore.user?.email }}</p>
        <p class="hint">阶段 7 后续将在此展示任务列表、提交任务等功能。</p>
      </div>
    </main>
  </div>
</template>

<style scoped>
.home-page {
  min-height: 100vh;
  background: var(--color-bg);
}

.home-header {
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
}

.header-inner {
  max-width: var(--content-width);
  margin: 0 auto;
  padding: var(--space-4) var(--space-6);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-weight: var(--fw-bold);
  font-size: var(--fs-lg);
  color: var(--color-primary);
}

.user-area {
  display: flex;
  align-items: center;
  gap: var(--space-4);
}

.user-email {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.btn-logout {
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);
}

.btn-logout:hover {
  color: var(--color-danger);
  border-color: var(--color-danger);
}

.home-main {
  max-width: var(--content-width);
  margin: 0 auto;
  padding: var(--space-12) var(--space-6);
}

.welcome-card {
  text-align: center;
  padding: var(--space-12);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
}

.welcome-icon {
  display: inline-flex;
  color: var(--color-success);
  margin-bottom: var(--space-4);
}

.welcome-card h1 {
  font-size: var(--fs-2xl);
  margin-bottom: var(--space-2);
}

.welcome-card p {
  color: var(--color-text-secondary);
  font-size: var(--fs-base);
}

.welcome-card .hint {
  margin-top: var(--space-6);
  font-size: var(--fs-sm);
  color: var(--color-text-muted);
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  display: inline-block;
}
</style>
