<script setup lang="ts">
/**
 * 提交任务页(对话式输入框)
 *
 * 布局类似常见大模型 Web 聊天输入框:
 * - 顶部:场景(mode)选择 + 使用模型选择
 * - 中部:大尺寸 textarea(用户主输入,提交时作为 user_input 拼到智能体上下文)
 * - 输入框底部:GitHub 仓库选择/输入 + 分支 + 发送按钮
 *
 * 字段映射(对齐后端 params):
 * - userInput → user_input(用户主输入)
 * - repoUrl   → params.repo_url
 * - branch    → params.branch
 *
 * 提交后:后端立即返回 task_id(异步执行),前端跳转详情页通过 SSE 观看实时进度。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import { createTask, getScenarios } from '@/api/task'
import { getMyModels } from '@/api/model_configs'
import { getGitHubStatus, listGitHubRepos } from '@/api/github'
import { extractErrorMessage } from '@/utils/error'
import type { Scenario } from '@/types/task'
import type { LLMConfigItemOut } from '@/types/model_configs'
import type { GitHubRepoItem, GitHubStatus } from '@/types/github'

const router = useRouter()

/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

// ---- 场景列表 ----

const scenarios = ref<Scenario[]>([])
const selectedScenario = ref('')

/** 当前选中场景声明(用于读取 note 字段 placeholder 等元信息) */
const selectedScenarioDecl = computed<Scenario | null>(() =>
  scenarios.value.find((s) => s.id === selectedScenario.value) ?? null,
)

// ---- 模型列表 ----

const llmConfigs = ref<LLMConfigItemOut[]>([])
const selectedLlmConfigId = ref('')
const loadingModels = ref(true)

// ---- 表单数据(扁平化,对应场景声明字段) ----

/** 用户主输入(对话式 textarea,对应 note 字段) */
const userInput = ref('')
/** GitHub 仓库地址(对应 repo_url 字段) */
const repoUrl = ref('')
/** 分支(对应 branch 字段) */
const branch = ref('')

/** 通用 URL 校验 */
const urlPattern = /^https?:\/\/[^\s/$.?#].[^\s]*$/

const loading = ref(false)
const error = ref('')

// ============================================================
// GitHub 仓库选择(repo_url 字段专用增强)
// ============================================================

/**
 * repo_url 字段支持两种输入方式:
 * - 'url':手动输入公开仓库地址(默认,场景无关)
 * - 'select':从已绑定 GitHub 账号的仓库列表中选择(可含私有仓库)
 *
 * 仅当用户已绑定 GitHub 时显示模式切换;未绑定时走普通 url 输入,不阻塞流程。
 */
const githubStatus = ref<GitHubStatus | null>(null)
const githubBound = computed(() => githubStatus.value?.bound ?? false)

const githubRepos = ref<GitHubRepoItem[]>([])
const reposLoaded = ref(false)
const reposLoading = ref(false)
const reposError = ref('')

/** repo_url 输入模式 */
const repoInputMode = ref<'url' | 'select'>('url')
/** 选择模式下,当前选中的仓库 full_name(owner/repo) */
const selectedRepoFullName = ref('')

async function loadGitHubRepos(): Promise<void> {
  if (reposLoaded.value || reposLoading.value) return
  reposLoading.value = true
  reposError.value = ''
  try {
    const res = await listGitHubRepos()
    githubRepos.value = res.repos
    reposLoaded.value = true
  } catch (err) {
    reposError.value = extractErrorMessage(err)
  } finally {
    reposLoading.value = false
  }
}

function switchToSelectMode(): void {
  repoInputMode.value = 'select'
  // 懒加载仓库列表(首次进入选择模式才请求)
  if (!reposLoaded.value) loadGitHubRepos()
}

// 选中仓库 → 同步 clone_url 到 repoUrl,并填默认分支
watch(selectedRepoFullName, (fullName) => {
  if (!fullName) return
  const repo = githubRepos.value.find((r) => r.full_name === fullName)
  if (!repo) return
  repoUrl.value = repo.clone_url
  if (!branch.value.trim()) {
    branch.value = repo.default_branch
  }
})

// 切换场景时重置输入(避免上一场景的选择残留)
watch(selectedScenario, () => {
  userInput.value = ''
  repoUrl.value = ''
  branch.value = ''
  repoInputMode.value = 'url'
  selectedRepoFullName.value = ''
  nextTick(autoResize)
})

// ---- 校验 ----

const repoUrlError = computed(() => {
  const v = repoUrl.value.trim()
  if (!v) return '' // 仓库地址可选(用户可只输入文字说明)
  if (!urlPattern.test(v)) return '请输入有效的仓库地址'
  return ''
})

const canSubmit = computed(() => {
  if (loading.value) return false
  const hasInput = userInput.value.trim().length > 0
  const hasRepo = repoUrl.value.trim().length > 0 && !repoUrlError.value
  return hasInput || hasRepo
})

/** textarea placeholder:优先用场景声明 note 字段的 placeholder */
const chatPlaceholder = computed(() => {
  const noteField = selectedScenarioDecl.value?.form_fields.find(
    (f) => f.name === 'note',
  )
  return (
    noteField?.placeholder ??
    '请输入任务说明,如:审计 src/ 目录的 SQL 注入风险'
  )
})

// ---- 自动调整 textarea 高度 ----

const textareaRef = ref<HTMLTextAreaElement | null>(null)

function autoResize(): void {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  // 限制最大高度 ~240px,超过则内部滚动
  el.style.height = Math.min(el.scrollHeight, 240) + 'px'
}

watch(userInput, () => {
  nextTick(autoResize)
})

function onTextareaKeydown(e: KeyboardEvent): void {
  // Enter 提交,Shift+Enter 换行
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    if (canSubmit.value) handleSubmit()
  }
}

// ---- 提交 ----

async function handleSubmit(): Promise<void> {
  error.value = ''
  if (!canSubmit.value) return

  loading.value = true
  try {
    const params: Record<string, unknown> = {}
    const repoUrlVal = repoUrl.value.trim()
    const branchVal = branch.value.trim()
    if (repoUrlVal) params.repo_url = repoUrlVal
    if (branchVal) params.branch = branchVal

    // user_input 优先用用户主输入;若为空但仓库地址已填,自动兜底生成
    let finalUserInput = userInput.value.trim()
    if (!finalUserInput && repoUrlVal) {
      finalUserInput = `请处理这个仓库: ${repoUrlVal}`
    }
    if (!finalUserInput) {
      error.value = '请输入任务说明或仓库地址'
      return
    }

    // 后端立即返回 task_id(后台线程异步执行)
    const res = await createTask({
      scenario: selectedScenario.value,
      user_input: finalUserInput,
      llm_config_id: selectedLlmConfigId.value || undefined,
      params,
    })

    // 立即跳转详情页,SSE 接收实时进度
    await router.push({ name: 'task-detail', params: { id: res.id } })
  } catch (err) {
    error.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}

/** 模型选项的显示文本 */
function modelLabel(cfg: LLMConfigItemOut): string {
  const name = cfg.name || cfg.model
  return cfg.has_api_key ? name : `${name}(未配置 Key)`
}

onMounted(async () => {
  try {
    const [scenarioList, models, ghStatus] = await Promise.all([
      getScenarios(),
      getMyModels().catch(() => null),
      getGitHubStatus().catch(() => null), // 静默失败,未绑定不影响提交
    ])
    scenarios.value = scenarioList
    if (scenarioList.length > 0) {
      selectedScenario.value = scenarioList[0].id
    }
    if (models && models.llm_configs.length > 0) {
      llmConfigs.value = models.llm_configs
      // 默认选第一个已配置 Key 的,否则选第一个
      const firstWithKey = models.llm_configs.find((c) => c.has_api_key)
      selectedLlmConfigId.value = (firstWithKey ?? models.llm_configs[0]).id
    }
    if (ghStatus) githubStatus.value = ghStatus
  } catch {
    // 场景拉取失败兜底(不应发生,保留旧默认以便能提交)
    selectedScenario.value = 'code_security_audit'
  } finally {
    loadingModels.value = false
  }
  nextTick(() => {
    autoResize()
    // 自动聚焦输入框
    textareaRef.value?.focus()
  })
})
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
        <RouterLink to="/">首页</RouterLink>
        <RouterLink to="/tasks/new" class="router-link-active">提交任务</RouterLink>
        <RouterLink to="/models">模型设置</RouterLink>
      </template>
    </AppHeader>

    <div class="page-body">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <main class="main">
        <!-- 顶部:场景模式选择 + 模型选择 -->
        <div class="topbar">
          <div class="scenario-segmented" role="tablist" aria-label="场景选择">
            <button
              v-for="s in scenarios"
              :key="s.id"
              type="button"
              :class="['seg-btn', { active: selectedScenario === s.id }]"
              role="tab"
              :aria-selected="selectedScenario === s.id"
              @click="selectedScenario = s.id"
            >{{ s.name }}</button>
            <span v-if="scenarios.length === 0" class="seg-loading">场景加载中...</span>
          </div>

          <div class="model-select">
            <select
              v-model="selectedLlmConfigId"
              :disabled="loadingModels"
              aria-label="使用模型"
            >
              <option value="">默认模型</option>
              <option
                v-for="cfg in llmConfigs"
                :key="cfg.id"
                :value="cfg.id"
              >
                {{ modelLabel(cfg) }}
              </option>
            </select>
            <RouterLink
              v-if="llmConfigs.length === 0 && !loadingModels"
              to="/models"
              class="model-empty-link"
            >配置 →</RouterLink>
          </div>
        </div>

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

        <!-- 对话式输入框 -->
        <div class="chat-card">
          <!-- 主体:大 textarea -->
          <textarea
            ref="textareaRef"
            v-model="userInput"
            class="chat-input"
            rows="3"
            :placeholder="chatPlaceholder"
            @keydown="onTextareaKeydown"
          />

          <!-- 分隔线 -->
          <div class="chat-divider" />

          <!-- 底部:GitHub 仓库 + 分支 + 提交按钮 -->
          <div class="chat-footer">
            <!-- 仓库输入/选择区 -->
            <div class="repo-area">
              <!-- 模式切换(已绑定 GitHub 才显示) -->
              <div
                v-if="githubBound"
                class="repo-mode-toggle"
                role="tablist"
                aria-label="仓库输入方式"
              >
                <button
                  type="button"
                  :class="['mode-btn', { active: repoInputMode === 'url' }]"
                  role="tab"
                  :aria-selected="repoInputMode === 'url'"
                  @click="repoInputMode = 'url'"
                >输入地址</button>
                <button
                  type="button"
                  :class="['mode-btn', { active: repoInputMode === 'select' }]"
                  role="tab"
                  :aria-selected="repoInputMode === 'select'"
                  @click="switchToSelectMode"
                >GitHub 选择</button>
              </div>

              <!-- URL 输入 + 分支(同一行) -->
              <div v-if="repoInputMode === 'url'" class="repo-input-row">
                <input
                  v-model.trim="repoUrl"
                  type="url"
                  class="repo-input"
                  :class="{ invalid: repoUrlError }"
                  placeholder="https://github.com/owner/repo"
                  aria-label="GitHub 仓库地址"
                />
                <input
                  v-model.trim="branch"
                  type="text"
                  class="branch-input"
                  placeholder="默认分支"
                  aria-label="分支"
                />
              </div>

              <!-- 选择模式 -->
              <div v-else class="repo-select-row">
                <select
                  v-model="selectedRepoFullName"
                  :disabled="reposLoading"
                  class="repo-select"
                  aria-label="选择 GitHub 仓库"
                >
                  <option value="">选择仓库...</option>
                  <option
                    v-for="r in githubRepos"
                    :key="r.full_name"
                    :value="r.full_name"
                  >
                    {{ r.full_name }}{{ r.private ? ' (私有)' : '' }}
                  </option>
                </select>
                <input
                  v-model.trim="branch"
                  type="text"
                  class="branch-input"
                  placeholder="默认分支"
                  aria-label="分支"
                />
              </div>

              <!-- 仓库加载/错误提示 -->
              <p v-if="reposLoading" class="repo-hint">加载仓库列表...</p>
              <p v-if="reposError" class="repo-error">{{ reposError }}</p>
              <p v-if="repoUrlError" class="repo-error">{{ repoUrlError }}</p>
              <p
                v-if="repoInputMode === 'select'
                  && !reposLoading
                  && !reposError
                  && reposLoaded
                  && githubRepos.length === 0"
                class="repo-hint"
              >你的 GitHub 账号下暂无仓库</p>
              <p
                v-if="repoInputMode === 'select'
                  && !reposLoading
                  && !reposLoaded"
                class="repo-hint"
              >
                未绑定 GitHub?
                <RouterLink to="/settings" class="repo-link">前往设置绑定 →</RouterLink>
              </p>
            </div>

            <!-- 发送按钮 -->
            <button
              type="button"
              class="send-btn"
              :disabled="!canSubmit"
              :title="loading ? '处理中...' : '开始任务 (Enter)'"
              aria-label="开始任务"
              @click="handleSubmit"
            >
              <span v-if="loading" class="spinner" />
              <svg
                v-else
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <line x1="22" y1="2" x2="11" y2="13" />
                <polygon points="22 2 15 22 11 13 2 9 22 2" />
              </svg>
            </button>
          </div>
        </div>

        <!-- 操作提示 -->
        <p class="chat-tip">
          <kbd>Enter</kbd> 发送 ·
          <kbd>Shift</kbd>+<kbd>Enter</kbd> 换行
        </p>
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
  max-width: 768px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  overflow-y: auto;
  padding: var(--space-6) var(--space-6) var(--space-8);
}

/* ---- 顶部:场景模式 + 模型 ---- */
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
  flex-wrap: wrap;
}

.scenario-segmented {
  display: inline-flex;
  align-items: center;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  padding: 3px;
  gap: 2px;
}

.seg-btn {
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-full);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.seg-btn:hover {
  color: var(--color-text);
}

.seg-btn.active {
  color: white;
  background: var(--color-primary);
}

.seg-loading {
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  color: var(--color-text-muted);
}

.model-select {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.model-select select {
  height: 36px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: border-color var(--transition-fast);
  max-width: 200px;
}

.model-select select:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.model-empty-link {
  font-size: var(--fs-xs);
  color: var(--color-primary);
  text-decoration: none;
}

.model-empty-link:hover {
  text-decoration: underline;
}

/* ---- 错误提示 ---- */
.alert {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  margin-bottom: var(--space-4);
}

.alert svg {
  flex-shrink: 0;
  margin-top: 2px;
}

.alert-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border: 1px solid #fecaca;
}

/* ---- 对话式输入框 ---- */
.chat-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-md);
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.chat-card:focus-within {
  border-color: var(--color-primary-border);
  box-shadow: 0 0 0 3px var(--color-primary-light), var(--shadow-md);
}

.chat-input {
  width: 100%;
  min-height: 96px;
  max-height: 240px;
  padding: 0;
  font-size: var(--fs-base);
  font-family: var(--font-sans);
  color: var(--color-text);
  background: transparent;
  border: none;
  outline: none;
  resize: none;
  line-height: var(--lh-relaxed);
  overflow-y: auto;
}

.chat-input::placeholder {
  color: var(--color-text-muted);
}

.chat-divider {
  height: 1px;
  background: var(--color-border);
  margin: 0 calc(-1 * var(--space-4));
}

.chat-footer {
  display: flex;
  align-items: flex-end;
  gap: var(--space-3);
}

/* ---- 仓库输入区 ---- */
.repo-area {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.repo-mode-toggle {
  display: inline-flex;
  align-self: flex-start;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 2px;
  gap: 2px;
}

.mode-btn {
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.mode-btn:hover {
  color: var(--color-text);
}

.mode-btn.active {
  color: var(--color-primary);
  background: var(--color-surface);
  box-shadow: var(--shadow-sm);
}

.repo-input-row,
.repo-select-row {
  display: flex;
  gap: var(--space-2);
}

.repo-input,
.repo-select,
.branch-input {
  height: 36px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  outline: none;
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.repo-input,
.repo-select {
  flex: 1;
  min-width: 0;
}

.branch-input {
  flex: 0 0 120px;
}

.repo-input:focus,
.repo-select:focus,
.branch-input:focus {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.repo-input.invalid {
  border-color: var(--color-danger);
  box-shadow: 0 0 0 3px var(--color-danger-light);
}

.repo-hint,
.repo-error {
  font-size: var(--fs-xs);
  margin: 0;
}

.repo-hint {
  color: var(--color-text-muted);
}

.repo-error {
  color: var(--color-danger);
}

.repo-link {
  color: var(--color-primary);
  text-decoration: none;
}

.repo-link:hover {
  text-decoration: underline;
}

/* ---- 发送按钮 ---- */
.send-btn {
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: white;
  background: var(--color-primary);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.send-btn:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.send-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  background: var(--color-text-muted);
}

.spinner {
  width: 16px;
  height: 16px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* ---- 提示 ---- */
.chat-tip {
  margin: var(--space-3) 0 0;
  text-align: center;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.chat-tip kbd {
  display: inline-block;
  padding: 1px 6px;
  font-size: var(--fs-xs);
  font-family: var(--font-sans);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  box-shadow: 0 1px 0 var(--color-border-strong);
}

/* ---- 过渡 ---- */
.fade-enter-active,
.fade-leave-active {
  transition: opacity var(--transition-base);
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

/* ---- 小屏适配 ---- */
@media (max-width: 640px) {
  .topbar {
    flex-direction: column;
    align-items: stretch;
  }

  .model-select {
    justify-content: flex-end;
  }

  .model-select select {
    max-width: 100%;
  }

  .branch-input {
    flex: 0 0 96px;
  }
}
</style>
