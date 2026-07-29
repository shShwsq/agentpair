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
      <!-- 最左侧前置区(如工作区开关按钮) -->
      <div v-if="$slots.leading" class="header-leading">
        <slot name="leading" />
      </div>
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
  /* 顶栏内容撑满宽度,侧栏按钮紧贴左边缘;用户区靠 margin-left:auto 钉到右侧 */
  padding: var(--space-4) var(--space-6) var(--space-4) var(--space-4);
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: var(--space-3);
}

.header-left {
  display: flex;
  align-items: center;
  gap: var(--space-8);
}

.header-leading {
  display: flex;
  align-items: center;
  flex-shrink: 0;
}

/* 品牌:深色字标 + 主色图标,收紧字距,克制不张扬 */
.brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-weight: var(--fw-semibold);
  font-size: var(--fs-lg);
  letter-spacing: -0.01em;
  color: var(--color-text);
}

.brand svg {
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
  margin-left: auto;
}

.user-email {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  letter-spacing: 0.01em;
}

.btn-logout {
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  letter-spacing: 0.01em;
  transition: all var(--transition-fast);
}

.btn-logout:hover {
  color: var(--color-danger);
  border-color: var(--color-danger);
  background: var(--color-danger-light);
}

/* 导航项(slot 里的 RouterLink/a):去掉强填充,用文字色 + 极细下划线指示当前页 */
:deep(.nav a) {
  position: relative;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  letter-spacing: 0.01em;
  border-radius: var(--radius-md);
  transition: color var(--transition-fast), background var(--transition-fast);
}

:deep(.nav a:hover) {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

:deep(.nav a.router-link-active) {
  color: var(--color-primary);
  background: transparent;
  font-weight: var(--fw-semibold);
}

:deep(.nav a.router-link-active)::after {
  content: '';
  position: absolute;
  left: var(--space-3);
  right: var(--space-3);
  bottom: 3px;
  height: 2px;
  background: var(--color-primary);
  border-radius: var(--radius-full);
}
</style>
