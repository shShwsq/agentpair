<script setup lang="ts">
/**
 * 应用顶栏(登录后的页面共用)
 *
 * 左侧品牌 + 导航,右侧用户信息 + 齿轮下拉(账号设置 / 记忆管理)+ 登出。
 * 通过 slot 支持页面自定义导航项。
 *
 * 硬约束:账号类入口(/settings)与新记忆入口(/memory)只由齿轮下拉进入,
 * 不入主导航,避免账号/偏好类入口与功能页并列。
 */
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import BrandLogo from '@/components/BrandLogo.vue'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const authStore = useAuthStore()

/** 齿轮下拉菜单是否展开 */
const menuOpen = ref(false)

function toggleMenu(): void {
  menuOpen.value = !menuOpen.value
}

function goTo(path: string): void {
  menuOpen.value = false
  router.push(path)
}

function handleLogout(): void {
  menuOpen.value = false
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
          <BrandLogo :size="24" />
          <span>AgentPair</span>
        </RouterLink>
        <nav class="nav">
          <slot name="nav" />
        </nav>
      </div>
      <!-- 右侧前置区(如右侧栏开关按钮);空 slot 时该 div 的 margin-left:auto 仍把用户区钉到右侧 -->
      <div class="header-trailing">
        <slot name="trailing" />
      </div>
      <div class="user-area">
        <span class="user-email">{{ authStore.user?.email }}</span>
        <div class="settings-menu">
          <button
            class="btn-settings"
            :class="{ 'btn-settings-active': menuOpen }"
            title="账号与记忆"
            aria-label="账号与记忆"
            :aria-expanded="menuOpen"
            @click="toggleMenu"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </button>
          <Transition name="menu-fade">
            <div v-if="menuOpen" class="menu-dropdown" role="menu">
              <button class="menu-item" role="menuitem" @click="goTo('/settings')">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
                  <circle cx="12" cy="7" r="4" />
                </svg>
                <span>账号设置</span>
              </button>
              <button class="menu-item" role="menuitem" @click="goTo('/memory')">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2z" />
                  <path d="M12 6v6l4 2" />
                </svg>
                <span>记忆管理</span>
              </button>
            </div>
          </Transition>
          <!-- 透明遮罩:捕获齿轮下拉外的点击以关闭菜单 -->
          <div v-if="menuOpen" class="menu-backdrop" @click="menuOpen = false" />
        </div>
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
  padding: var(--space-2) var(--space-6) var(--space-2) var(--space-4);
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

/* 右侧前置区:用 margin-left:auto 把自身及之后的用户区推到右侧;空 slot 时仍生效 */
.header-trailing {
  display: flex;
  align-items: center;
  margin-left: auto;
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
}

.user-email {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  letter-spacing: 0.01em;
}

/* 设置图标按钮:与登出按钮风格一致但更紧凑,圆形 hover 反馈 */
.btn-settings {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-settings:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.btn-settings-active {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

/* 齿轮下拉菜单容器:相对定位,下拉绝对锚定在齿轮下方 */
.settings-menu {
  position: relative;
  display: inline-flex;
}

/* 透明遮罩:覆盖全屏以捕获齿轮外点击关闭菜单(位于下拉之下、内容之上) */
.menu-backdrop {
  position: fixed;
  inset: 0;
  z-index: 20;
  background: transparent;
}

/* 下拉菜单卡片 */
.menu-dropdown {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 21;
  min-width: 168px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  padding: var(--space-1);
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.menu-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  text-align: left;
  transition: background var(--transition-fast), color var(--transition-fast);
}

.menu-item:hover {
  background: var(--color-surface-alt);
  color: var(--color-primary);
}

.menu-item svg {
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.menu-item:hover svg {
  color: var(--color-primary);
}

/* 下拉淡入淡出 */
.menu-fade-enter-active,
.menu-fade-leave-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}

.menu-fade-enter-from,
.menu-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
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
