<script setup lang="ts">
/**
 * 首页
 *
 * 登录后的落地页。展示欢迎信息 + 提交任务 CTA。
 * 左侧可展开历史任务栏(WorkspaceSidebar),与任务详情页一致。
 */
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'

const router = useRouter()

/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}
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
        <RouterLink to="/" class="router-link-active">首页</RouterLink>
        <RouterLink to="/tasks/new">提交任务</RouterLink>
        <RouterLink to="/settings">模型设置</RouterLink>
      </template>
    </AppHeader>

    <div class="page-body">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <main class="main">
        <div class="welcome-card">
          <div class="welcome-icon">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 2a4 4 0 0 1 4 4v1h1a3 3 0 0 1 3 3v3a3 3 0 0 1-3 3h-1v1a4 4 0 0 1-4 4" />
              <path d="M12 2a4 4 0 0 0-4 4v1H7a3 3 0 0 0-3 3v3a3 3 0 0 0 3 3h1v1a4 4 0 0 0 4 4" />
            </svg>
          </div>
          <h1>欢迎使用 AgentPair</h1>
          <p>双智能体协作系统 · user_agent 澄清意图,react_agent 执行任务</p>

          <div class="cta-area">
            <button class="btn-primary" @click="router.push('/tasks/new')">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
              提交新任务
            </button>
          </div>

          <div class="feature-grid">
            <div class="feature">
              <div class="feature-icon">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M9 11H1l8-8 8 8h-8v10" transform="rotate(180 9 11)" />
                </svg>
              </div>
              <h3>user_agent</h3>
              <p>对照场景判据评估结果,针对未覆盖项追问,确保任务完整</p>
            </div>
            <div class="feature">
              <div class="feature-icon">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polyline points="16 18 22 12 16 6" />
                  <polyline points="8 6 2 12 8 18" />
                </svg>
              </div>
              <h3>react_agent</h3>
              <p>调用工具执行任务,提交结构化结果供 user_agent 评估</p>
            </div>
          </div>
        </div>
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
  max-width: var(--content-width);
  margin: 0 auto;
  overflow-y: auto;
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
  color: var(--color-primary);
  margin-bottom: var(--space-4);
}

.welcome-card h1 {
  font-size: var(--fs-2xl);
  margin-bottom: var(--space-2);
}

.welcome-card > p {
  color: var(--color-text-secondary);
  font-size: var(--fs-base);
}

.cta-area {
  margin: var(--space-8) 0;
}

.btn-primary {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: 48px;
  padding: 0 var(--space-6);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: white;
  background: var(--color-primary);
  border-radius: var(--radius-md);
  transition: background var(--transition-fast);
}

.btn-primary:hover {
  background: var(--color-primary-hover);
}

.feature-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: var(--space-5);
  margin-top: var(--space-8);
  text-align: left;
}

.feature {
  padding: var(--space-5);
  background: var(--color-surface-alt);
  border-radius: var(--radius-lg);
}

.feature-icon {
  display: inline-flex;
  color: var(--color-primary);
  margin-bottom: var(--space-3);
}

.feature h3 {
  font-size: var(--fs-base);
  margin-bottom: var(--space-2);
}

.feature p {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}
</style>
