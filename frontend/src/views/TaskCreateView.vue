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
import ModelCombobox from '@/components/ModelCombobox.vue'
import BaseSelect from '@/components/BaseSelect.vue'
import BrandLogo from '@/components/BrandLogo.vue'
import { createTask, getScenarios } from '@/api/task'
import { getMyModels } from '@/api/model_configs'
import { getAgentConfigs } from '@/api/agent_configs'
import { getGitHubStatus, listGitHubRepos } from '@/api/github'
import { getSkills, type SkillSummary } from '@/api/skill'
import { extractErrorMessage } from '@/utils/error'
import type { Scenario } from '@/types/task'
import type { LLMConfigItemOut } from '@/types/model_configs'
import type { AgentConfigOut } from '@/types/agent_configs'
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
/** user_agent 评估模型 */
const selectedLlmConfigId = ref('')
/**
 * 内置 react_agent 模型(仅 builtin 模式生效)。
 * 空字符串 = 同评估模型(回退到 selectedLlmConfigId);
 * 非空 = 显式选另一个模型。
 */
const selectedReactLlmConfigId = ref('')
const loadingModels = ref(true)

// ---- 执行器选择(内置 + 用户已配置且启用的 agent) ----

/**
 * 执行器:决定 react 角色由哪个 agent 执行
 * - 'builtin':系统内置 react_agent(使用上方选择的 LLM 配置)
 * - agent_type(如 'qoder_cli'):对应 agent CLI,react 角色模型由 CLI 自管;
 *   上方选择的 LLM 配置仅用于 user_agent 评估
 *
 * 候选列表由后端 GET /agents/configs 动态返回(is_active=true 的)。
 */
const agentExecutors = ref<AgentConfigOut[]>([])
/** 当前选中执行器:'builtin' 或某个 agent_type */
const selectedExecutor = ref<string>('builtin')

/** 是否选中了非内置执行器(CLI 自管 react 模型,LLM 配置仅供 user_agent) */
const useAgentExecutor = computed(() => selectedExecutor.value !== 'builtin')

/**
 * 模型选择器在当前执行器下的语义标签
 * - builtin:模型同时用于内置 react_agent 与 user_agent 评估
 * - CLI:模型仅用于 user_agent 评估(执行模型由 CLI 自管)
 */
const modelSelectLabel = computed(() =>
  useAgentExecutor.value ? '评估模型' : '使用模型',
)

// ---- Qoder CLI 模型配置(仅 qoder_cli 执行器显示) ----
// 见 https://docs.qoder.cn/cli/model

/** Qoder CLI 模型选项(value 必须与 CLI --model 接受的名称严格大小写匹配,
 * 见 https://docs.qoder.cn/cli/model 及 ACP 错误返回的 Available models 列表) */
const qoderModelOptions: { value: string; label: string }[] = [
  { value: '', label: '默认(智能路由 Auto)' },
  { value: 'Auto', label: '智能路由 (Auto)' },
  { value: 'Qwen3.8-Max', label: 'Qwen3.8-Max' },
  { value: 'Qwen3.7-Max', label: 'Qwen3.7-Max' },
  { value: 'Qwen3.7-Plus', label: 'Qwen3.7-Plus' },
  { value: 'Qwen3.6-Flash', label: 'Qwen3.6-Flash' },
  { value: 'DeepSeek-V4-Pro', label: 'DeepSeek-V4-Pro' },
  { value: 'DeepSeek-V4-Flash', label: 'DeepSeek-V4-Flash' },
  { value: 'GLM-5.2', label: 'GLM-5.2' },
  { value: 'Kimi-K2.7-Code', label: 'Kimi-K2.7-Code' },
  { value: 'MiniMax-M2.7', label: 'MiniMax-M2.7' },
]

/** 思考强度选项 */
const qoderEffortOptions: { value: string; label: string }[] = [
  { value: '', label: '默认' },
  { value: 'low', label: 'Low(最快)' },
  { value: 'medium', label: 'Medium(适中)' },
  { value: 'high', label: 'High(深入)' },
  { value: 'xhigh', label: 'XHigh(深度分析)' },
  { value: 'max', label: 'Max(最大推理)' },
]

/** 上下文窗口选项 */
const qoderContextOptions: { value: number; label: string }[] = [
  { value: 0, label: '默认' },
  { value: 200000, label: '200K' },
  { value: 400000, label: '400K' },
  { value: 1000000, label: '1M' },
]

/** Qoder CLI 配置面板是否展开 */
const qoderConfigOpen = ref(false)
/** 选中的模型(空字符串=默认) */
const qoderModel = ref('')
/** 选中的思考强度(空字符串=默认) */
const qoderReasoningEffort = ref('')
/** 选中的上下文窗口(0=默认) */
const qoderContextWindow = ref(0)

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

/** 仓库提示弹窗(加载失败/无仓库/未绑定 等),用一个统一弹窗承载,避免行内提示抖动 */
const repoDialogOpen = ref(false)
/** 弹窗正文(空串=不显示) */
const repoDialogMessage = ref('')
/** 弹窗是否提供"前往设置绑定"链接 */
const repoDialogShowBindLink = ref(false)

/** 打开仓库提示弹窗 */
function showRepoDialog(message: string, showBindLink = false): void {
  repoDialogMessage.value = message
  repoDialogShowBindLink.value = showBindLink
  repoDialogOpen.value = true
}

function closeRepoDialog(): void {
  repoDialogOpen.value = false
}

async function loadGitHubRepos(): Promise<void> {
  if (reposLoaded.value || reposLoading.value) return
  reposLoading.value = true
  reposError.value = ''
  try {
    const res = await listGitHubRepos()
    githubRepos.value = res.repos
    reposLoaded.value = true
    if (res.repos.length === 0) {
      showRepoDialog('你的 GitHub 账号下暂无仓库')
    }
  } catch (err) {
    reposError.value = extractErrorMessage(err)
    showRepoDialog(`加载仓库列表失败:${reposError.value}`)
  } finally {
    reposLoading.value = false
  }
}

/**
 * 仓库下拉框选项(可输入下拉框)。
 * value=clone_url(选中后写入 repoUrl),label=full_name(私有标记)。
 */
const repoComboboxOptions = computed(() =>
  githubRepos.value.map((r) => ({
    value: r.clone_url,
    label: `${r.full_name}${r.private ? ' (私有)' : ''}`,
  })),
)

/** 从下拉选中仓库时,若分支为空则自动填默认分支 */
watch(repoUrl, (url) => {
  if (!url) return
  const repo = githubRepos.value.find((r) => r.clone_url === url)
  if (repo && !branch.value.trim()) {
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

    // Qoder CLI 模型配置(qoder_cli / qoder_cli_cn 执行器时写入)
    if (selectedExecutor.value.startsWith('qoder_cli')) {
      if (qoderModel.value) params.model = qoderModel.value
      if (qoderReasoningEffort.value) params.reasoning_effort = qoderReasoningEffort.value
      if (qoderContextWindow.value) params.context_window = qoderContextWindow.value
    }

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
      // 仅 builtin 模式下传 react_llm_config_id;空串不传(后端回退到 llm_config_id)
      react_llm_config_id:
        selectedExecutor.value === 'builtin'
          ? (selectedReactLlmConfigId.value || undefined)
          : undefined,
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

// ---- BaseSelect 选项(computed,适配 {value,label} 结构) ----

/** user_agent 评估模型选项 */
const llmConfigOptions = computed(() => [
  { value: '', label: useAgentExecutor.value ? '默认评估模型' : '默认模型' },
  ...llmConfigs.value.map((cfg) => ({ value: cfg.id, label: modelLabel(cfg) })),
])

/** react_agent 模型选项(仅 builtin,空=同评估模型) */
const reactLlmConfigOptions = computed(() => [
  { value: '', label: '同评估模型' },
  ...llmConfigs.value.map((cfg) => ({ value: cfg.id, label: modelLabel(cfg) })),
])

/** 执行器选项:内置 + 用户已配置且启用的 agent CLI */
const executorOptions = computed(() => [
  { value: 'builtin', label: '内置' },
  ...agentExecutors.value.map((a) => ({ value: a.agent_type, label: a.display_name })),
])

onMounted(async () => {
  try {
    const [scenarioList, models, ghStatus, skills, agentCfgs] = await Promise.all([
      getScenarios(),
      getMyModels().catch(() => null),
      getGitHubStatus().catch(() => null), // 静默失败,未绑定不影响提交
      getSkills().catch(() => null as SkillSummary[] | null), // 静默失败,无 skill 不阻塞提交
      getAgentConfigs().catch(() => null), // 静默失败,无 agent 配置不影响提交
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
    // 已绑定 GitHub:预加载仓库列表,供可输入下拉框选择(失败由弹窗提示)
    if (ghStatus?.bound) loadGitHubRepos()
    // skill 列表加载成功后,默认全选;若已选场景则按推荐重置
    if (skills && skills.length > 0) {
      allSkills.value = skills
      selectedSkillNames.value = new Set(skills.map((s) => s.name))
      applyRecommendedSkills()
    }
    // 仅展示已启用且已配置凭据的 agent 作为可选执行器
    if (agentCfgs) {
      agentExecutors.value = agentCfgs.configs.filter((c) => c.is_active && c.has_credentials)
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
        <!-- 顶部配置区:三行布局(场景 / user_agent 模型 / react_agent 设置) -->
        <div class="topbar">
          <!-- 第 1 行:场景(无标签,直接靠左) -->
          <div class="config-row config-row-scenario">
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
          </div>

          <!-- 第 2 行:user_agent 模型 -->
          <div class="config-row">
            <div class="config-label-group">
              <span class="agent-avatar avatar-user-agent" aria-hidden="true">
                <BrandLogo :size="16" />
              </span>
              <span class="config-label">user_agent</span>
            </div>
            <div class="model-select">
              <BaseSelect
                v-model="selectedLlmConfigId"
                :options="llmConfigOptions"
                :disabled="loadingModels"
                :aria-label="modelSelectLabel"
              />
              <RouterLink
                v-if="llmConfigs.length === 0 && !loadingModels"
                to="/models"
                class="model-empty-link"
              >配置 →</RouterLink>
            </div>
          </div>

          <!-- 第 3 行:react_agent 设置(执行器 + CLI 模型配置 / 技能) -->
          <div class="config-row">
            <div class="config-label-group">
              <span class="agent-avatar avatar-react-agent" aria-hidden="true">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                  <!-- 机器人头部 -->
                  <rect x="4" y="7" width="16" height="12" rx="3" />
                  <!-- 天线 -->
                  <line x1="12" y1="3" x2="12" y2="7" />
                  <circle cx="12" cy="3" r="1.2" fill="currentColor" stroke="none" />
                  <!-- 双眼 -->
                  <circle cx="9" cy="13" r="1.2" fill="currentColor" stroke="none" />
                  <circle cx="15" cy="13" r="1.2" fill="currentColor" stroke="none" />
                  <!-- 底部支架/底座 -->
                  <line x1="8" y1="19" x2="8" y2="21" />
                  <line x1="16" y1="19" x2="16" y2="21" />
                </svg>
              </span>
              <span class="config-label">react_agent</span>
            </div>
            <div class="react-controls">
              <!-- 执行器选择(下拉框):内置 + 用户已配置且启用的 agent CLI -->
              <div class="executor-select">
                <BaseSelect
                  v-model="selectedExecutor"
                  :options="executorOptions"
                  aria-label="执行器选择"
                />
              </div>

            <!-- react_agent 模型(仅 builtin:可与 user_agent 用不同模型;空=同评估模型) -->
            <div v-if="!useAgentExecutor" class="model-select react-model-select">
              <BaseSelect
                v-model="selectedReactLlmConfigId"
                :options="reactLlmConfigOptions"
                :disabled="loadingModels"
                aria-label="react_agent 模型"
              />
            </div>

              <!-- Qoder CLI 模型配置(仅 qoder_cli / qoder_cli_cn 执行器显示) -->
              <div v-if="selectedExecutor.startsWith('qoder_cli')" class="qoder-config-panel">
                <button
                  type="button"
                  class="qoder-config-toggle"
                  :aria-expanded="qoderConfigOpen"
                  @click="qoderConfigOpen = !qoderConfigOpen"
                >
                  <svg
                    class="qoder-chevron"
                    :class="{ expanded: qoderConfigOpen }"
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
                  <span>Qoder 模型</span>
                  <span class="qoder-config-summary">
                    {{ qoderModel || 'Auto' }} · {{ qoderReasoningEffort || '默认' }} · {{ qoderContextWindow ? (qoderContextWindow >= 1000000 ? '1M' : (qoderContextWindow / 1000) + 'K') : '默认' }}
                  </span>
                </button>

                <Transition name="collapse">
                  <div v-show="qoderConfigOpen" class="qoder-config-dropdown">
                    <div class="qoder-config-row">
                      <label class="qoder-config-label">模型</label>
                      <BaseSelect
                        v-model="qoderModel"
                        :options="qoderModelOptions"
                        size="sm"
                        class="qoder-config-select"
                      />
                    </div>
                    <div class="qoder-config-row">
                      <label class="qoder-config-label">思考强度</label>
                      <BaseSelect
                        v-model="qoderReasoningEffort"
                        :options="qoderEffortOptions"
                        size="sm"
                        class="qoder-config-select"
                      />
                    </div>
                    <div class="qoder-config-row">
                      <label class="qoder-config-label">上下文窗口</label>
                      <BaseSelect
                        v-model.number="qoderContextWindow"
                        :options="qoderContextOptions"
                        size="sm"
                        class="qoder-config-select"
                      />
                    </div>
                  </div>
                </Transition>
              </div>

              <!-- 技能多选(仅内置执行器显示:CLI 用自身工具系统,本地 skill 无效) -->
              <div v-if="!useAgentExecutor && allSkills.length > 0" class="advanced-panel">
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
            <!-- 仓库输入/选择区:可输入下拉框(已绑定 GitHub 时可从仓库列表选;否则纯输入) -->
            <div class="repo-area">
              <div class="repo-input-row">
                <!-- 已绑定 GitHub:可输入 + 下拉选择仓库 -->
                <ModelCombobox
                  v-if="githubBound"
                  :model-value="repoUrl"
                  :options="repoComboboxOptions"
                  :disabled="reposLoading"
                  :placeholder="reposLoading ? '加载仓库列表...' : '选择或输入 GitHub 仓库地址'"
                  @update:model-value="repoUrl = $event"
                />
                <!-- 未绑定:纯输入框 -->
                <input
                  v-else
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

    <!-- 仓库提示弹窗(加载失败/无仓库/未绑定) -->
    <Teleport to="body">
      <Transition name="dialog-fade">
        <div v-if="repoDialogOpen" class="repo-dialog-mask" @click.self="closeRepoDialog">
          <div class="repo-dialog-card" role="dialog" aria-modal="true">
            <header class="repo-dialog-header">
              <h3>提示</h3>
              <button
                class="repo-dialog-close"
                aria-label="关闭"
                @click="closeRepoDialog"
              >×</button>
            </header>
            <div class="repo-dialog-body">
              <p class="repo-dialog-message">{{ repoDialogMessage }}</p>
              <RouterLink
                v-if="repoDialogShowBindLink"
                to="/settings"
                class="repo-dialog-link"
                @click="closeRepoDialog"
              >前往设置绑定 →</RouterLink>
            </div>
            <footer class="repo-dialog-footer">
              <button class="btn btn-primary" @click="closeRepoDialog">知道了</button>
            </footer>
          </div>
        </div>
      </Transition>
    </Teleport>
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

/* ---- 顶部配置区:三行布局(场景 / user_agent 模型 / react_agent 设置) ---- */
.topbar {
  display: flex;
  flex-direction: column;
  margin-bottom: var(--space-4);
}

/* 单行配置:左侧标签 + 右侧控件 */
.config-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
  padding: var(--space-2) 0;
}

/* 第 1 行场景:无标签,直接靠左 */
.config-row-scenario {
  padding-left: 0;
}

.config-label-group {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  flex-shrink: 0;
  /* 固定宽度,确保三行控件起点严格对齐(容纳头像 + 标签文字) */
  width: 116px;
}

.config-label {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  white-space: nowrap;
}

/* agent 头像:圆形徽标 + 白色图标 */
.agent-avatar {
  flex-shrink: 0;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #fff;
}

.avatar-user-agent {
  background: var(--color-info);
}

.avatar-react-agent {
  background: #7c3aed;
}

/* 第 3 行 react_agent 控件容器:横向排列执行器 + CLI 配置 / 技能 */
.react-controls {
  display: flex;
  align-items: center;
  gap: var(--space-3);
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

/* BaseSelect 宽度约束(高度/边框/内边距由组件内部 size="md" 处理) */
.model-select .base-select {
  max-width: 200px;
}

/* ---- 执行器选择(下拉框) ---- */
.executor-select {
  display: inline-flex;
}

.executor-select .base-select {
  min-width: 120px;
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

.repo-input-row {
  display: flex;
  gap: var(--space-2);
}

/* repo 下拉框撑满(已绑定时);纯输入框同样撑满 */
.repo-input-row .combobox {
  flex: 1;
  min-width: 0;
}

.repo-input,
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

.repo-input {
  flex: 1;
  min-width: 0;
}

.branch-input {
  flex: 0 0 120px;
}

.repo-input:focus,
.branch-input:focus {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.repo-input.invalid {
  border-color: var(--color-danger);
  box-shadow: 0 0 0 3px var(--color-danger-light);
}

/* ---- 仓库提示弹窗 ---- */
.repo-dialog-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: var(--space-4);
}

.repo-dialog-card {
  background: var(--color-surface);
  border-radius: var(--radius-xl);
  box-shadow: 0 20px 40px rgba(0, 0, 0, 0.15);
  width: 100%;
  max-width: 420px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.repo-dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.repo-dialog-header h3 {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  margin: 0;
  color: var(--color-text);
}

.repo-dialog-close {
  background: none;
  border: none;
  font-size: 24px;
  line-height: 1;
  color: var(--color-text-muted);
  cursor: pointer;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.repo-dialog-close:hover {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.repo-dialog-body {
  padding: var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.repo-dialog-message {
  font-size: var(--fs-sm);
  color: var(--color-text);
  margin: 0;
  line-height: 1.6;
}

.repo-dialog-link {
  font-size: var(--fs-sm);
  color: var(--color-primary);
  text-decoration: none;
  font-weight: var(--fw-medium);
}

.repo-dialog-link:hover {
  text-decoration: underline;
}

.repo-dialog-footer {
  display: flex;
  justify-content: flex-end;
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
}

.repo-dialog-footer .btn {
  padding: var(--space-2) var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  border: 1px solid transparent;
  display: inline-flex;
  align-items: center;
}

.repo-dialog-footer .btn-primary {
  background: var(--color-primary);
  color: white;
}

.repo-dialog-footer .btn-primary:hover {
  filter: brightness(1.05);
}

/* 弹窗淡入淡出 */
.dialog-fade-enter-active,
.dialog-fade-leave-active {
  transition: opacity 0.2s ease;
}

.dialog-fade-enter-from,
.dialog-fade-leave-to {
  opacity: 0;
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
  /* 窄屏每行标签与控件上下排列 */
  .config-row {
    align-items: flex-start;
    flex-direction: column;
    gap: var(--space-1);
  }

  .config-label-group {
    min-width: 0;
  }

  .model-select {
    flex: 1;
    width: 100%;
  }

  .model-select .base-select {
    max-width: 100%;
    flex: 1;
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

  .qoder-config-dropdown {
    min-width: 0;
    max-width: calc(100vw - var(--space-6) * 2);
  }
}

/* ---- Qoder CLI 模型配置(下拉浮层,与高级选项同模式) ---- */
.qoder-config-panel {
  position: relative;
}

.qoder-config-toggle {
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

.qoder-config-toggle:hover {
  color: var(--color-text);
  border-color: var(--color-primary-border);
}

.qoder-config-toggle[aria-expanded="true"] {
  color: var(--color-primary);
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.qoder-chevron {
  flex-shrink: 0;
  transition: transform var(--transition-fast);
  color: var(--color-text-muted);
}

.qoder-chevron.expanded {
  transform: rotate(90deg);
}

.qoder-config-summary {
  font-size: var(--fs-xs);
  font-weight: var(--fw-normal);
  color: var(--color-text-muted);
  font-variant-numeric: tabular-nums;
}

.qoder-config-dropdown {
  position: absolute;
  top: calc(100% + var(--space-2));
  right: 0;
  z-index: var(--z-dropdown, 100);
  min-width: 320px;
  max-width: min(80vw, 480px);
  padding: var(--space-3) var(--space-4) var(--space-4);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg, 0 10px 25px rgba(0, 0, 0, 0.12));
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.qoder-config-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.qoder-config-label {
  flex-shrink: 0;
  width: 80px;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
}

/* BaseSelect 根容器:仅需撑满 flex 行;高度/边框/内边距由组件内部 size="sm" 处理 */
.qoder-config-select {
  flex: 1;
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
