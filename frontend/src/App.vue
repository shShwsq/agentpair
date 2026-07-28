<script setup lang="ts">
/**
 * 根组件
 *
 * 只做 RouterView 容器 + 全局过渡。
 * 具体页面布局由各 View 自行负责(全屏 vs 带导航等)。
 */
</script>

<template>
  <RouterView />
</template>

<style>
/* 全局样式在 main.ts 导入,这里不放任何 scoped 样式 */
</style>
