<script setup lang="ts">
/**
 * 项目品牌 logo
 *
 * 设计语义:上方单个圆代表 user_agent(沟通评估者),
 * 下方三个圆代表 react_agent(调用多工具/技能执行任务),
 * 三根连线表示两者之间的协作通道。
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
    stroke-width="1.5"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <!-- 整体顺时针旋转 90°:单圆(user_agent)在上,三圆(react_agent)在下 -->
    <g transform="rotate(90 12 12)">
      <!-- 单节点:user_agent -->
      <circle cx="5.5" cy="12" r="3.5" />
      <!-- 三个节点:react_agent(三角形排列,象征多工具/技能;拉开间距;实心) -->
      <circle cx="15" cy="5.5" r="2.2" fill="currentColor" stroke="none" />
      <circle cx="21" cy="12" r="2.2" fill="currentColor" stroke="none" />
      <circle cx="15" cy="18.5" r="2.2" fill="currentColor" stroke="none" />
      <!-- 三根连接线:单节点 → 三个节点(沿圆心连线方向,起止于圆边缘;上下两根带弧度) -->
      <path d="M 8.39 10.03 Q 10 7.5, 13.18 6.74" />
      <line x1="9" y1="12" x2="18.8" y2="12" />
      <path d="M 8.39 13.97 Q 10 16.5, 13.18 17.26" />
    </g>
  </svg>
</template>
