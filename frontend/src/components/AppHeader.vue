<script setup lang="ts">
/**
 * 应用顶栏(登录后的页面共用)
 *
 * 左侧品牌 + 导航(首页/提交任务/模型设置/CLI 设置/协作策略/记忆管理),右侧用户信息 + 问号(帮助文档)+ 齿轮(账号设置)+ 登出。
 *
 * 导航为 slot 的默认内容:所有界面默认显示这 6 项,无需每个视图重复声明;
 * 当前页高亮依赖 Vue Router 自动添加的 router-link-exact-active。
 * 个别视图若需自定义导航,仍可用 <template #nav> 覆盖默认内容。
 *
 * 硬约束:账号设置(/settings)只由齿轮按钮进入,不入主导航。
 * 记忆管理(/memory)、协作策略(/agent-policy)作为主导航项,与模型设置/CLI 设置并列。
 * 问号按钮:打开帮助文档弹窗(所有路由行为一致,展示完整 help.md)。
 */
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import BrandLogo from '@/components/BrandLogo.vue'
import HelpDialog from '@/components/HelpDialog.vue'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const authStore = useAuthStore()

/** 帮助文档弹窗是否显示 */
const helpOpen = ref(false)

function handleLogout(): void {
  authStore.logout()
  router.push('/login')
}

/** 打开帮助文档弹窗 */
function handleOpenHelp(): void {
  helpOpen.value = true
}

/** 关闭帮助文档弹窗 */
function handleCloseHelp(): void {
  helpOpen.value = false
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
        <nav class="nav" data-onboarding="app-header-nav">
          <slot name="nav">
            <RouterLink to="/">首页</RouterLink>
            <RouterLink to="/tasks/new">提交任务</RouterLink>
            <RouterLink to="/models">模型设置</RouterLink>
            <RouterLink to="/cli">CLI 设置</RouterLink>
            <RouterLink to="/agent-policy">协作策略</RouterLink>
            <RouterLink to="/memory">记忆管理</RouterLink>
          </slot>
        </nav>
      </div>
      <!-- 右侧前置区(如右侧栏开关按钮);空 slot 时该 div 的 margin-left:auto 仍把用户区钉到右侧 -->
      <div class="header-trailing">
        <slot name="trailing" />
      </div>
      <div class="user-area">
        <span class="user-email">{{ authStore.user?.email }}</span>
        <button
          class="btn-help"
          title="帮助文档"
          aria-label="帮助文档"
          @click="handleOpenHelp"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10" />
            <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
            <line x1="12" y1="17" x2="12.01" y2="17" />
          </svg>
        </button>
        <button
          class="btn-settings"
          title="账号设置"
          aria-label="账号设置"
          data-onboarding="app-header-settings"
          @click="router.push('/settings')"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </button>
        <button class="btn-logout" @click="handleLogout">登出</button>
      </div>
    </div>
  </header>

  <!-- 帮助文档弹窗(所有路由通用) -->
  <HelpDialog :open="helpOpen" @close="handleCloseHelp" />
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
.btn-settings,
.btn-help {
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

.btn-settings:hover,
.btn-help:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
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
/* 高亮依赖 Vue Router 自动添加的 router-link-exact-active(精确匹配,避免 / 在子路由也高亮) */
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

:deep(.nav a.router-link-exact-active) {
  color: var(--color-primary);
  background: transparent;
  font-weight: var(--fw-semibold);
}

:deep(.nav a.router-link-exact-active)::after {
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
