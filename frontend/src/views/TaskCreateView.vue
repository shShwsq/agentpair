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
import { getSkills, type SkillSummary } from '@/api/skill'
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

/**
 * 当前选中场景(用于读取 preset_prompt 等元信息)
 *
 * 场景已降级为模板:仅提供 preset_prompt 预填到输入框,
 * 不再声明 form_fields/result_grouping/coverage 等。
 */
const selectedScenarioDecl = computed<Scenario | null>(() =>
  scenarios.value.find((s) => s.id === selectedScenario.value) ?? null,
)

// ---- 模型列表 ----

const llmConfigs = ref<LLMConfigItemOut[]>([])
const selectedLlmConfigId = ref('')
const loadingModels = ref(true)

// ---- 执行器选择(builtin / trae_cli) ----

/**
 * 执行器:决定 react 角色由哪个 agent 执行
 * - builtin:系统内置 react_agent(使用上方选择的 LLM 配置)
 * - trae_cli:TRAE CLI(沙箱内运行,模型由 trae_cli.yaml 管理,忽略 LLM 配置)
 */
const selectedExecutor = ref<'builtin' | 'trae_cli'>('builtin')

// ---- Skill 多选(高级选项) ----

/** 所有可用 skill(从后端 GET /skills 加载) */
const allSkills = ref<SkillSummary[]>([])
/** skill 列表加载错误(静默失败,不阻塞提交) */
const skillsError = ref('')
/** 高级选项面板是否展开(默认折叠) */
const advancedOpen = ref(false)
/**
 * 当前选中的 skill name 集合
 *
 * 语义:
 * - 默认(无场景推荐 / general):全部勾选(等同于不限制,提交时不传 allowed_skills)
 * - 选场景后:勾选该场景 recommended_skills 中实际存在的 skill
 * - 用户可手动勾选/取消
 * - 提交时:若全部勾选 → 传 undefined(全部可用);否则传选中的数组
 */
const selectedSkillNames = ref<Set<string>>(new Set())

/** 是否全部 skill 都已选中 */
const allSkillsSelected = computed(
  () => allSkills.value.length > 0 && selectedSkillNames.value.size === allSkills.value.length,
)

/** 选中的 skill 数量(展示用) */
const selectedSkillCount = computed(() => selectedSkillNames.value.size)

/** 切换单个 skill 的选中状态 */
function toggleSkill(name: string): void {
  const next = new Set(selectedSkillNames.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  selectedSkillNames.value = next
}

/** 全选 / 全不选切换 */
function toggleAllSkills(): void {
  if (allSkillsSelected.value) {
    selectedSkillNames.value = new Set()
  } else {
    selectedSkillNames.value = new Set(allSkills.value.map((s) => s.name))
  }
}

/**
 * 根据场景的 recommended_skills 重置 skill 选中状态
 *
 * - recommended_skills 为空或 general 场景:全选(默认全部可用)
 * - recommended_skills 非空:只勾选推荐且实际存在的 skill
 */
function applyRecommendedSkills(): void {
  const recommended = selectedScenarioDecl.value?.recommended_skills ?? []
  if (recommended.length === 0) {
    // 无推荐 → 全选(等同于不限制)
    selectedSkillNames.value = new Set(allSkills.value.map((s) => s.name))
    return
  }
  // 只勾选推荐且实际存在的 skill
  const existingNames = new Set(allSkills.value.map((s) => s.name))
  const validRecommended = recommended.filter((name) => existingNames.has(name))
  selectedSkillNames.value = new Set(validRecommended)
}

// ---- 表单数据(扁平化,对应场景声明字段) ----

/** 用户主输入(对话式 textarea,对应 note 字段) */
const userInput = ref('')
/** 任务标题(可选,便于在历史列表识别;留空回退到 user_input 截断展示) */
const taskTitle = ref('')
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

// 切换场景时:把场景的 preset_prompt 预填到 userInput(用户可自由编辑),
// 并重置其他字段(避免上一场景的选择残留),同时根据推荐重置 skill 选中状态
watch(selectedScenario, () => {
  userInput.value = selectedScenarioDecl.value?.preset_prompt ?? ''
  taskTitle.value = ''
  repoUrl.value = ''
  branch.value = ''
  repoInputMode.value = 'url'
  selectedRepoFullName.value = ''
  // 根据场景推荐重置 skill 选中状态(skill 列表已加载时才生效)
  applyRecommendedSkills()
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

/** textarea placeholder(固定文案;场景降级后不再从场景声明读取) */
const chatPlaceholder = '请输入任务说明,如:审计这个仓库的安全风险,或分析代码质量'

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
    // allowed_skills:全部勾选时不传(等同于全部可用);部分勾选时传选中数组
    const allowedSkillsPayload: string[] | undefined = allSkillsSelected.value
      ? undefined
      : Array.from(selectedSkillNames.value)

    const res = await createTask({
      scenario: selectedScenario.value,
      title: taskTitle.value.trim() || undefined,
      user_input: finalUserInput,
      llm_config_id: selectedLlmConfigId.value || undefined,
      executor: selectedExecutor.value,
      allowed_skills: allowedSkillsPayload,
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
    const [scenarioList, models, ghStatus, skills] = await Promise.all([
      getScenarios(),
      getMyModels().catch(() => null),
      getGitHubStatus().catch(() => null), // 静默失败,未绑定不影响提交
      getSkills().catch(() => null as SkillSummary[] | null), // 静默失败,无 skill 不阻塞提交
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
    // skill 列表加载成功后,默认全选;若已选场景则按推荐重置
    if (skills && skills.length > 0) {
      allSkills.value = skills
      selectedSkillNames.value = new Set(skills.map((s) => s.name))
      applyRecommendedSkills()
    }
  } catch {
    // 场景拉取失败兜底(不应发生,保留旧默认以便能提交)
    selectedScenario.value = 'general'
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
        <!-- 顶部:场景模式选择 + 模型选择 + 高级选项(技能多选) -->
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

          <div class="topbar-right">
            <!-- 执行器选择:内置 react_agent / TRAE CLI -->
            <div
              class="executor-segmented"
              role="tablist"
              aria-label="执行器选择"
            >
              <button
                type="button"
                :class="['exec-btn', { active: selectedExecutor === 'builtin' }]"
                role="tab"
                :aria-selected="selectedExecutor === 'builtin'"
                title="系统内置 ReAct 智能体(使用下方选择的 LLM 配置)"
                @click="selectedExecutor = 'builtin'"
              >内置</button>
              <button
                type="button"
                :class="['exec-btn', { active: selectedExecutor === 'trae_cli' }]"
                role="tab"
                :aria-selected="selectedExecutor === 'trae_cli'"
                title="TRAE CLI(沙箱内运行,模型由 trae_cli.yaml 管理)"
                @click="selectedExecutor = 'trae_cli'"
              >TRAE CLI</button>
            </div>

            <!-- 模型选择(TRAE CLI 模式下禁用:模型由沙箱内 trae_cli.yaml 管理) -->
            <div class="model-select" :class="{ disabled: selectedExecutor === 'trae_cli' }">
              <select
                v-model="selectedLlmConfigId"
                :disabled="loadingModels || selectedExecutor === 'trae_cli'"
                :aria-label="selectedExecutor === 'trae_cli' ? 'TRAE CLI 模式下模型由 trae_cli.yaml 管理' : '使用模型'"
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

            <!-- 高级选项:Skill 多选(下拉浮层,默认折叠) -->
            <div v-if="allSkills.length > 0" class="advanced-panel">
              <button
                type="button"
                class="advanced-toggle"
                :aria-expanded="advancedOpen"
                @click="advancedOpen = !advancedOpen"
              >
                <svg
                  class="advanced-chevron"
                  :class="{ expanded: advancedOpen }"
                  width="12"
                  height="12"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                >
                  <polyline points="9 18 15 12 9 6" />
                </svg>
                <span>技能</span>
                <span class="advanced-summary">
                  {{ selectedSkillCount }}/{{ allSkills.length }}
                </span>
              </button>

              <Transition name="collapse">
                <div v-show="advancedOpen" class="advanced-dropdown">
                  <div class="skill-header">
                    <label class="skill-select-all">
                      <input
                        type="checkbox"
                        :checked="allSkillsSelected"
                        @change="toggleAllSkills"
                      />
                      <span>全选 / 全不选</span>
                    </label>
                    <p class="skill-hint">
                      勾选的技能将作为 react_agent 可调用的专家知识。
                      全选=不限制(默认);部分勾选=仅允许选中的;全不选=不启用任何技能。
                    </p>
                  </div>

                  <div class="skill-list">
                    <label
                      v-for="skill in allSkills"
                      :key="skill.name"
                      class="skill-item"
                      :class="{ checked: selectedSkillNames.has(skill.name) }"
                    >
                      <input
                        type="checkbox"
                        :checked="selectedSkillNames.has(skill.name)"
                        @change="toggleSkill(skill.name)"
                      />
                      <div class="skill-info">
                        <span class="skill-name">{{ skill.name }}</span>
                        <span class="skill-desc">{{ skill.description }}</span>
                      </div>
                    </label>
                  </div>
                </div>
              </Transition>
            </div>
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
          <!-- 任务标题(可选) -->
          <div class="title-row">
            <input
              v-model.trim="taskTitle"
              type="text"
              class="title-input"
              maxlength="255"
              placeholder="任务标题(可选,便于在历史列表识别)"
              aria-label="任务标题"
            />
          </div>

          <!-- 分隔线 -->
          <div class="chat-divider" />

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

        <!-- skill 加载错误提示(静默,不阻塞) -->
        <p v-if="skillsError" class="skill-load-error">
          技能列表加载失败:{{ skillsError }}(不影响任务提交)
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

/* TRAE CLI 模式下模型选择禁用样式 */
.model-select.disabled {
  opacity: 0.45;
  pointer-events: none;
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

/* ---- 执行器选择(builtin / trae_cli 分段控件) ---- */
.executor-segmented {
  display: inline-flex;
  align-items: center;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 2px;
  gap: 2px;
}

.exec-btn {
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

.exec-btn:hover {
  color: var(--color-text);
}

.exec-btn.active {
  color: var(--color-primary);
  background: var(--color-surface-alt);
  box-shadow: var(--shadow-sm);
}

.model-empty-link {
  font-size: var(--fs-xs);
  color: var(--color-primary);
  text-decoration: none;
}

.model-empty-link:hover {
  text-decoration: underline;
}

/* ---- topbar 右侧:模型 + 高级选项 ---- */
.topbar-right {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
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

/* ---- 任务标题输入(可选) ---- */
.title-row {
  display: flex;
}

.title-input {
  width: 100%;
  height: 32px;
  padding: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  font-family: var(--font-sans);
  color: var(--color-text);
  background: transparent;
  border: none;
  outline: none;
  transition: color var(--transition-fast);
}

.title-input::placeholder {
  color: var(--color-text-muted);
  font-weight: var(--fw-normal);
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

  .topbar-right {
    justify-content: space-between;
  }

  .model-select {
    flex: 1;
  }

  .model-select select {
    max-width: 100%;
  }

  .branch-input {
    flex: 0 0 96px;
  }

  /* 窄屏下拉浮层撑满宽度 */
  .advanced-dropdown {
    min-width: 0;
    max-width: calc(100vw - var(--space-6) * 2);
    right: 0;
    left: 0;
  }
}

/* ---- 高级选项:Skill 多选(下拉浮层,挂在模型选择右边) ---- */
.advanced-panel {
  position: relative;
}

.advanced-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  height: 36px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.advanced-toggle:hover {
  color: var(--color-text);
  border-color: var(--color-primary-border);
}

.advanced-toggle[aria-expanded="true"] {
  color: var(--color-primary);
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.advanced-chevron {
  flex-shrink: 0;
  transition: transform var(--transition-fast);
  color: var(--color-text-muted);
}

.advanced-chevron.expanded {
  transform: rotate(90deg);
}

.advanced-summary {
  font-size: var(--fs-xs);
  font-weight: var(--fw-normal);
  color: var(--color-text-muted);
  font-variant-numeric: tabular-nums;
}

/* 下拉浮层:绝对定位,不撑高 topbar */
.advanced-dropdown {
  position: absolute;
  top: calc(100% + var(--space-2));
  right: 0;
  z-index: var(--z-dropdown, 100);
  min-width: 420px;
  max-width: min(80vw, 640px);
  padding: var(--space-3) var(--space-4) var(--space-4);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg, 0 10px 25px rgba(0, 0, 0, 0.12));
}

.skill-header {
  display: flex;
  align-items: flex-start;
  gap: var(--space-4);
  margin-bottom: var(--space-3);
  flex-wrap: wrap;
}

.skill-select-all {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  cursor: pointer;
  white-space: nowrap;
}

.skill-select-all input {
  cursor: pointer;
}

.skill-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  line-height: var(--lh-relaxed);
  flex: 1;
  min-width: 200px;
}

.skill-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--space-2);
}

.skill-item {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.skill-item:hover {
  border-color: var(--color-primary-border);
  background: var(--color-surface-alt);
}

.skill-item.checked {
  border-color: var(--color-primary-border);
  background: var(--color-primary-light);
}

.skill-item input {
  margin-top: 2px;
  cursor: pointer;
  flex-shrink: 0;
}

.skill-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.skill-name {
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  word-break: break-all;
}

.skill-desc {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  line-height: var(--lh-snug);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.skill-load-error {
  margin-top: var(--space-2);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  text-align: center;
}

/* ---- 折叠过渡 ---- */
.collapse-enter-active,
.collapse-leave-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}

.collapse-enter-from,
.collapse-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}
</style>
