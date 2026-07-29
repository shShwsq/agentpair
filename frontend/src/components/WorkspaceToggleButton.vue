<script setup lang="ts">
/**
 * 工作区开关按钮
 *
 * 方框 + 中间偏左一竖线图标。展开态额外显示右侧一竖线(侧栏+主区)。
 * 渲染在 AppHeader 的 #leading slot 内,用于各页面切换侧栏显隐。
 */
defineProps<{
  /** 当前是否折叠 */
  collapsed: boolean
}>()

defineEmits<{
  toggle: []
}>()
</script>

<template>
  <button
    class="workspace-toggle"
    :class="{ 'is-active': !collapsed }"
    :title="collapsed ? '展开历史任务' : '折叠历史任务'"
    :aria-label="collapsed ? '展开历史任务' : '折叠历史任务'"
    @click="$emit('toggle')"
  >
    <svg
      class="workspace-toggle-icon"
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      stroke-width="2"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      <!-- 外框 -->
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <!-- 中间偏左的竖线(始终显示) -->
      <line x1="9" y1="6" x2="9" y2="18" />
      <!-- 展开态:右侧再加一竖线(侧栏+主区) -->
      <line v-if="!collapsed" x1="15" y1="6" x2="15" y2="18" />
    </svg>
  </button>
</template>

<style scoped>
.workspace-toggle {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  padding: 0;
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.workspace-toggle:hover {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.workspace-toggle.is-active {
  color: var(--color-primary);
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.workspace-toggle-icon {
  display: block;
  flex-shrink: 0;
}
</style>
