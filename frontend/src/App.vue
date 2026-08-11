<script setup lang="ts">
/**
 * 根组件
 *
 * 职责:
 * - RouterView 容器 + 全局过渡
 * - 全局挂载新手引导组件 OnboardingTour(单例,跟随路由按需触发)
 * - 在用户加载完成 / 路由切换时驱动引导状态机(setUser + maybeStartForRoute)
 *
 * 具体页面布局由各 View 自行负责(全屏 vs 带导航等)。
 */
import { watch } from 'vue'
import { useRoute } from 'vue-router'

import OnboardingTour from '@/components/OnboardingTour.vue'
import { useOnboarding } from '@/composables/useOnboarding'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const authStore = useAuthStore()
const { setUser, maybeStartForRoute } = useOnboarding()

// 用户加载/变化时同步 email 到引导状态机(登出 → null,引导不启动)
watch(
  () => authStore.user?.email ?? null,
  (email) => {
    setUser(email)
  },
  { immediate: true },
)

// 路由切换时尝试启动该路由的未读引导
// 延迟一帧执行,等待目标路由组件挂载 + data-onboarding 元素渲染
watch(
  () => route.name,
  (name) => {
    if (!name) return
    // 等下一帧,避免目标元素尚未挂载
    requestAnimationFrame(() => {
      maybeStartForRoute(String(name))
    })
  },
  { immediate: true },
)
</script>

<template>
  <RouterView />
  <OnboardingTour />
</template>

<style>
/* 全局样式在 main.ts 导入,这里不放任何 scoped 样式 */
</style>
