<script setup lang="ts">
/**
 * 项目品牌 logo
 *
 * 设计语义:两个圆形节点通过 S 形曲线互联,体现 AgentPair 的核心理念——
 * user_agent 与 react_agent 双智能体成对协作。
 *
 * 纯描边矢量(stroke=currentColor),可在任意尺寸/颜色下复用:
 * - AppHeader 品牌标志(primary 色,24px)
 * - LoginView 品牌区(32px)
 * - user_agent 头像(白色,16px,叠在彩色圆形底上)
 */
withDefaults(
  defineProps<{
    /** 图标尺寸(px) */
    size?: number | string
  }>(),
  {
    size: 24,
  },
)
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <!-- 左节点 -->
    <circle cx="6" cy="12" r="3" />
    <!-- 右节点 -->
    <circle cx="18" cy="12" r="3" />
    <!-- S 形互联曲线:两节点上下交叉连接,形成协作/成对的视觉 -->
    <path d="M9 9 C 12 5, 12 5, 15 9" />
    <path d="M9 15 C 12 19, 12 19, 15 15" />
  </svg>
</template>
