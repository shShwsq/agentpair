<script setup lang="ts">
/**
 * 提交任务页
 *
 * 表单:
 * - 场景选择(从 GET /scenarios 拉取)
 * - 仓库地址(必填,自动生成 user_input)
 * - 补充说明(可选,附加到 user_input)
 * - 分支(可选)
 *
 * 提交后:后端立即返回 task_id(异步执行),前端跳转详情页通过 SSE 观看实时进度。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import { createTask, getScenarios } from '@/api/task'
import { extractErrorMessage } from '@/utils/error'
import type { Scenario } from '@/types/task'

const router = useRouter()

// ---- 场景列表 ----

const scenarios = ref<Scenario[]>([])
const selectedScenario = ref('')

onMounted(async () => {
  try {
    scenarios.value = await getScenarios()
    if (scenarios.value.length > 0) {
      selectedScenario.value = scenarios.value[0].id
    }
  } catch {
    // 场景加载失败不阻塞,用默认值
    selectedScenario.value = 'code_security_audit'
  }
})

// ---- 表单 ----

const repoUrl = ref('')
const branch = ref('')
const note = ref('')
const loading = ref(false)
const error = ref('')

const githubUrlPattern = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+(?:\.git)?$/

const errors = computed(() => {
  const e: { repoUrl?: string } = {}
  if (!repoUrl.value.trim()) {
    e.repoUrl = '请输入仓库地址'
  } else if (!githubUrlPattern.test(repoUrl.value.trim())) {
    e.repoUrl = '请输入有效的 GitHub 仓库地址(https://github.com/owner/repo)'
  }
  return e
})

const canSubmit = computed(() => Object.keys(errors.value).length === 0 && !loading.value)

// ---- 提交 ----

async function handleSubmit(): Promise<void> {
  error.value = ''
  if (Object.keys(errors.value).length > 0) return

  loading.value = true
  try {
    // 构造 user_input:仓库地址 + 补充说明
    const url = repoUrl.value.trim()
    let userInput = `请审计这个仓库: ${url}`
    if (branch.value.trim()) {
      userInput += `\n分支: ${branch.value.trim()}`
    }
    if (note.value.trim()) {
      userInput += `\n补充说明: ${note.value.trim()}`
    }

    // 后端立即返回 task_id(后台线程异步执行)
    const res = await createTask({
      scenario: selectedScenario.value,
      user_input: userInput,
      params: {
        repo_url: url,
        ...(branch.value.trim() ? { branch: branch.value.trim() } : {}),
      },
    })

    // 立即跳转详情页,SSE 接收实时进度
    await router.push({ name: 'task-detail', params: { id: res.id } })
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="page">
    <AppHeader>
      <template #nav>
        <RouterLink to="/">首页</RouterLink>
        <RouterLink to="/tasks/new" class="router-link-active">提交任务</RouterLink>
      </template>
    </AppHeader>

    <main class="main">
      <div class="form-card">
        <h1>提交任务</h1>
        <p class="subtitle">输入 GitHub 仓库地址,双智能体将协作完成审计</p>

        <!-- 错误提示 -->
        <Transition name="fade">
          <div v-if="error" class="alert alert-error" role="alert">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
            <span>{{ error }}</span>
          </div>
        </Transition>

        <form @submit.prevent="handleSubmit" novalidate>
          <!-- 场景选择 -->
          <div class="field">
            <label>场景</label>
            <div class="scenario-list">
              <label
                v-for="s in scenarios"
                :key="s.id"
                :class="['scenario-card', { active: selectedScenario === s.id }]"
              >
                <input
                  type="radio"
                  v-model="selectedScenario"
                  :value="s.id"
                  name="scenario"
                />
                <span class="scenario-name">{{ s.name }}</span>
              </label>
            </div>
            <p v-if="scenarios.length === 0" class="field-hint">场景加载中...</p>
          </div>

          <!-- 仓库地址 -->
          <div class="field">
            <label for="repo-url">GitHub 仓库地址</label>
            <input
              id="repo-url"
              v-model.trim="repoUrl"
              type="url"
              placeholder="https://github.com/owner/repo"
              :class="{ invalid: errors.repoUrl }"
            />
            <span v-if="errors.repoUrl" class="field-error">{{ errors.repoUrl }}</span>
          </div>

          <!-- 分支(可选) -->
          <div class="field">
            <label for="branch">分支(可选)</label>
            <input
              id="branch"
              v-model.trim="branch"
              type="text"
              placeholder="默认主分支"
            />
          </div>

          <!-- 补充说明(可选) -->
          <div class="field">
            <label for="note">补充说明(可选)</label>
            <textarea
              id="note"
              v-model="note"
              rows="3"
              placeholder="如:重点关注认证模块、只审计 src/ 目录等"
            />
          </div>

          <button type="submit" class="btn-primary" :disabled="!canSubmit">
            <span v-if="loading" class="spinner" />
            {{ loading ? '处理中...' : '开始审计' }}
          </button>
        </form>
      </div>
    </main>
  </div>
</template>

<style scoped>
.page {
  min-height: 100vh;
  background: var(--color-bg);
}

.main {
  max-width: 640px;
  margin: 0 auto;
  padding: var(--space-8) var(--space-6);
}

.form-card {
  position: relative;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  padding: var(--space-8);
}

.form-card h1 {
  font-size: var(--fs-xl);
  margin-bottom: var(--space-2);
}

.subtitle {
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
  margin-bottom: var(--space-6);
}

/* ---- 加载遮罩 ---- */
.loading-overlay {
  position: absolute;
  inset: 0;
  background: rgba(255, 255, 255, 0.95);
  border-radius: var(--radius-xl);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: var(--space-8);
  z-index: 5;
}

.loading-overlay h2 {
  margin-top: var(--space-4);
  font-size: var(--fs-lg);
}

.loading-overlay p {
  margin-top: var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
}

.loading-overlay .hint {
  margin-top: var(--space-4);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.spinner-lg {
  width: 48px;
  height: 48px;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* ---- 提示 ---- */
.alert {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  margin-bottom: var(--space-4);
}

.alert svg { flex-shrink: 0; margin-top: 2px; }

.alert-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border: 1px solid #fecaca;
}

/* ---- 表单字段 ---- */
.field {
  margin-bottom: var(--space-5);
}

.field label {
  display: block;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  margin-bottom: var(--space-2);
}

.field input,
.field textarea {
  width: 100%;
  padding: var(--space-3);
  font-size: var(--fs-base);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.field input {
  height: 42px;
}

.field textarea {
  resize: vertical;
  font-family: var(--font-sans);
}

.field input::placeholder,
.field textarea::placeholder {
  color: var(--color-text-muted);
}

.field input:focus,
.field textarea:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.field input.invalid {
  border-color: var(--color-danger);
}

.field-error {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

.field-hint {
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

/* ---- 场景选择卡片 ---- */
.scenario-list {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
}

.scenario-card {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.scenario-card:hover {
  border-color: var(--color-primary-border);
}

.scenario-card.active {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.scenario-card input {
  width: auto;
  height: auto;
  margin: 0;
}

.scenario-name {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
}

/* ---- 按钮 ---- */
.btn-primary {
  width: 100%;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: white;
  background: var(--color-primary);
  border-radius: var(--radius-md);
  transition: background var(--transition-fast);
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-primary:disabled {
  opacity: 0.6;
}

.spinner {
  width: 16px;
  height: 16px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

/* ---- 过渡 ---- */
.fade-enter-active, .fade-leave-active {
  transition: opacity var(--transition-base);
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}
</style>
