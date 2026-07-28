<script setup lang="ts">
/**
 * 应用顶栏(登录后的页面共用)
 *
 * 左侧品牌 + 导航,右侧用户信息 + 登出。
 * 通过 slot 支持页面自定义导航项。
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
  <header class="app-header">
    <div class="header-inner">
      <div class="header-left">
        <RouterLink to="/" class="brand">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 2a4 4 0 0 1 4 4v1h1a3 3 0 0 1 3 3v3a3 3 0 0 1-3 3h-1v1a4 4 0 0 1-4 4" />
            <path d="M12 2a4 4 0 0 0-4 4v1H7a3 3 0 0 0-3 3v3a3 3 0 0 0 3 3h1v1a4 4 0 0 0 4 4" />
          </svg>
          <span>AgentPair</span>
        </RouterLink>
        <nav class="nav">
          <slot name="nav" />
        </nav>
      </div>
      <div class="user-area">
        <span class="user-email">{{ authStore.user?.email }}</span>
        <button class="btn-logout" @click="handleLogout">登出</button>
      </div>
    </div>
  </header>
</template>

<style scoped>
.app-header {
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
  position: sticky;
  top: 0;
  z-index: 10;
}

.header-inner {
  max-width: var(--content-width);
  margin: 0 auto;
  padding: var(--space-4) var(--space-6);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-6);
}

.header-left {
  display: flex;
  align-items: center;
  gap: var(--space-8);
}

.brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-weight: var(--fw-bold);
  font-size: var(--fs-lg);
  color: var(--color-primary);
}

.nav {
  display: flex;
  align-items: center;
  gap: var(--space-1);
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

/* 导航项(slot 里的 RouterLink/a)通用样式 */
:deep(.nav a) {
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);
}

:deep(.nav a:hover) {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

:deep(.nav a.router-link-active) {
  color: var(--color-primary);
  background: var(--color-primary-light);
}
</style>
