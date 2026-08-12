<script setup lang="ts">
/**
 * 提交任务页(对话式输入框)
 *
 * 布局类似常见大模型 Web 聊天输入框:
 * - 顶部:场景(mode)选择 + 使用模型选择
 * - 中部:大尺寸 textarea(用户主输入,提交时作为 user_input 拼到智能体上下文)
 * - 输入框底部:Git 仓库选择/输入(GitHub / Gitee)+ 分支 + 发送按钮
 *
 * 字段映射(对齐后端 params):
 * - userInput → user_input(用户主输入)
 * - repoUrl   → params.repo_url
 * - branch    → params.branch
 *
 * 提交后:后端立即返回 task_id(异步执行),前端跳转详情页通过 SSE 观看实时进度。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
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
import {
  getGitProviderStatus,
  listGitProviderRepos,
} from '@/api/git_provider'
import { getSkills, type SkillSummary } from '@/api/skill'
import { getPolicyLimits, getPreferences } from '@/api/memory'
import { extractErrorMessage } from '@/utils/error'
import type { Scenario } from '@/types/task'
import type { LLMConfigItemOut } from '@/types/model_configs'
import type { AgentConfigOut } from '@/types/agent_configs'
import type { GitProvider, GitRepoItem } from '@/types/git_provider'

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

// ---- Agent 策略配置(高级设置,任务级覆盖用户级默认) ----

/** 高级设置面板是否展开(默认折叠) */
const policyOpen = ref(false)
/** 是否分别配置内置/CLI 的 K 值(高级中的高级) */
const policyAdvanced = ref(false)
/** 统一 K 值:每 K 个迭代评估一次 */
const policyInterval = ref(3)
/** 内置 react_agent 专用 K 值(null=用统一值) */
const policyIntervalBuiltin = ref<number | null>(null)
/** CLI agent 专用 K 值(null=用统一值) */
const policyIntervalCli = ref<number | null>(null)
/** 是否启用 user_agent(关闭=单 agent 模式,跳过评估/打断/验证) */
const policyUserAgentEnabled = ref(true)
/** user_agent 协作总轮次(1-10,仅 user_agent 启用时生效) */
const policyMaxRounds = ref(4)
/** user_agent 是否能打断 react_agent */
const policyAllowInterrupt = ref(true)
/** 每轮最多打断次数 */
const policyMaxInterrupts = ref(2)
/** user_agent 是否能自己验证(实验性) */
const policyAllowVerify = ref(false)

/** 系统默认策略值(与后端 DEFAULT_AGENT_POLICY 对齐,作为未配置用户级默认时的兜底) */
const DEFAULT_POLICY = {
  user_agent_enabled: true,
  max_rounds: 4,
  checkpoint_interval: 3,
  allow_interrupt: true,
  max_interrupts_per_round: 2,
  allow_verify: false,
}

/**
 * 用户级默认策略(比较基准,用于判断是否需要提交任务级覆盖)。
 * - 初始为系统默认值;onMounted 加载用户偏好(协作策略设置页保存的)后替换为实际值。
 * - 加载失败/未配置时保持系统默认,与后端 resolve_agent_policy 的合并结果一致。
 */
const userPolicyDefaults = ref({
  userAgentEnabled: DEFAULT_POLICY.user_agent_enabled,
  maxRounds: DEFAULT_POLICY.max_rounds,
  interval: DEFAULT_POLICY.checkpoint_interval,
  intervalBuiltin: null as number | null,
  intervalCli: null as number | null,
  allowInterrupt: DEFAULT_POLICY.allow_interrupt,
  maxInterrupts: DEFAULT_POLICY.max_interrupts_per_round,
  allowVerify: DEFAULT_POLICY.allow_verify,
  verifierAuthMode: 'per_action' as 'direct' | 'per_action',
})

// 协作总轮次上限:从后端 GET /memory/policy-limits 动态拉取(默认 10 兜底)
const MAX_ROUNDS_LIMIT = ref(10)

/** 协作总轮次帮助气泡是否展开 */
const showMaxRoundsHelp = ref(false)

/** 切换协作总轮次帮助气泡 */
function toggleMaxRoundsHelp(e: Event): void {
  e.stopPropagation()
  showMaxRoundsHelp.value = !showMaxRoundsHelp.value
}

/** 点击帮助气泡外部时关闭 */
function onDocClickCloseHelp(e: MouseEvent): void {
  const target = e.target as HTMLElement
  if (!target.closest('.field-help-wrap')) {
    showMaxRoundsHelp.value = false
  }
}

/**
 * 协作总轮次输入处理:只允许非负整数,实时过滤非数字字符,钳制到 [1, MAX_ROUNDS_LIMIT]
 * - 禁止负号、小数点、字母等非法字符
 * - 超过上限自动钳制
 * - 临时空值允许(让用户能删除后重新输入),由 @blur 兜底
 */
function onMaxRoundsInput(e: Event): void {
  const input = e.target as HTMLInputElement
  // 只保留数字字符,过滤负号/小数点/字母
  const filtered = input.value.replace(/\D/g, '')
  if (filtered !== input.value) {
    input.value = filtered
  }
  if (filtered === '') return  // 临时空,不更新 ref
  let n = parseInt(filtered, 10)
  if (n > MAX_ROUNDS_LIMIT.value) {
    n = MAX_ROUNDS_LIMIT.value
    input.value = String(n)
  }
  if (n < 1) n = 1
  policyMaxRounds.value = n
}

/** 协作总轮次失焦:若为空,填默认值 1 */
function onMaxRoundsBlur(e: Event): void {
  const input = e.target as HTMLInputElement
  if (input.value === '') {
    input.value = '1'
    policyMaxRounds.value = 1
  }
}

// ---- 测试环境 / 动态验证配置(高级设置) ----
// user_agent 可在已部署的测试环境动态验证 react_agent 发现的安全问题。
// 对用户透明:不出现 verifier_agent 字样,只显示"正在验证"。
/** 测试环境面板是否展开(默认折叠) */
const verifierOpen = ref(false)
/** 是否启用动态验证 */
const verifierEnabled = ref(false)
/** 测试环境 URL(已部署的应用地址,如 http://localhost:3000) */
const testEnvUrl = ref('')
/**
 * 验证授权模式:
 * - "direct":验证动作直接执行不弹窗
 * - "per_action":每个 HTTP 请求/PoC 运行前弹窗授权
 */
const verifierAuthMode = ref<'direct' | 'per_action'>('per_action')
/**
 * 登录凭证列表(可选):LLM 调 http_request 时按 auth_profile=label 注入对应请求头。
 * 用于越权测试:同一端点用不同身份访问,对比响应差异。
 * 每项 = { label, header_name, header_value };LLM 只看到 label,看不到 header_value。
 */
const verifierAuthTokens = ref<Array<{ label: string; header_name: string; header_value: string }>>([])

/** 添加一个空凭证行 */
function addAuthToken(): void {
  verifierAuthTokens.value.push({ label: '', header_name: 'Authorization', header_value: '' })
}

/** 删除指定索引的凭证行 */
function removeAuthToken(idx: number): void {
  verifierAuthTokens.value.splice(idx, 1)
}

// ============================================================
// Git 仓库选择(repo_url 字段专用增强,统一 GitHub / Gitee)
// ============================================================

/**
 * repo_url 字段支持两种输入方式:
 * - 'url':手动输入公开仓库地址(默认,场景无关)
 * - 'select':从已绑定的 Git 平台账号(GitHub / Gitee)仓库列表中选择(可含私有仓库)
 *
 * 仅当用户已绑定任一平台时显示下拉选择;未绑定时走普通 url 输入,不阻塞流程。
 * 多平台绑定时,两组仓库合并到同一个下拉,选项 label 带 provider 标记。
 */
/** 支持的平台列表 */
const PROVIDERS: GitProvider[] = ['github', 'gitee']

/** 各平台绑定状态 */
const providerStatus = ref<Record<GitProvider, { bound: boolean } | null>>({
  github: null,
  gitee: null,
})

/** 是否已绑定任一平台(决定走下拉选择还是纯输入框) */
const anyProviderBound = computed(() =>
  PROVIDERS.some((p) => providerStatus.value[p]?.bound),
)

/** 仓库项(带 provider 标记,用于下拉选项 label 与 default_branch 填充) */
interface RepoEntry extends GitRepoItem {
  provider: GitProvider
}

/** 合并后的所有仓库列表 */
const allRepos = ref<RepoEntry[]>([])
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

/** 平台显示名(label 前缀用) */
function providerDisplayName(p: GitProvider): string {
  return p === 'gitee' ? 'Gitee' : 'GitHub'
}

/**
 * 并行加载所有已绑定平台的仓库列表,合并到 allRepos。
 * 任一平台失败不影响其他平台,仅记录到 reposError。
 *
 * @param force 强制刷新(跳过后端 30s 缓存,直接调平台 API),
 *              用于「刷新」按钮:用户在平台上新建仓库后立即拉取。
 *              force=true 时忽略 reposLoaded 一次性保护。
 */
async function loadAllRepos(force = false): Promise<void> {
  // force 模式跳过一次性保护,允许重复加载
  if (!force && (reposLoaded.value || reposLoading.value)) return
  if (reposLoading.value) return  // 正在加载中,避免并发
  reposLoading.value = true
  reposError.value = ''

  const boundProviders = PROVIDERS.filter((p) => providerStatus.value[p]?.bound)
  if (boundProviders.length === 0) {
    reposLoaded.value = true
    return
  }

  try {
    // 并行拉取所有已绑定平台的仓库;result 与 boundProviders 索引一一对应
    // force=true 时传 refresh=true 给后端,跳过 30s 缓存
    const results = await Promise.allSettled(
      boundProviders.map((p) => listGitProviderRepos(p, force)),
    )

    const merged: RepoEntry[] = []
    const errors: string[] = []
    results.forEach((r, idx) => {
      const p = boundProviders[idx]
      if (r.status === 'fulfilled') {
        for (const repo of r.value.repos) {
          merged.push({ ...repo, provider: p })
        }
      } else {
        errors.push(
          `${providerDisplayName(p)} 仓库加载失败: ${extractErrorMessage(r.reason)}`,
        )
      }
    })
    allRepos.value = merged
    reposLoaded.value = true

    if (merged.length === 0 && errors.length === 0) {
      showRepoDialog('你绑定的账号下暂无仓库')
    } else if (errors.length > 0) {
      // 部分成功时也提示失败的平台(不阻塞使用已加载的仓库)
      showRepoDialog(errors.join('\n'))
    }
  } catch (err) {
    reposError.value = extractErrorMessage(err)
    showRepoDialog(`加载仓库列表失败:${reposError.value}`)
  } finally {
    reposLoading.value = false
  }
}

/**
 * 刷新仓库列表(强制跳过缓存)。
 * 防抖 500ms:防止狂点按钮导致并发请求;
 * 保留当前选中的 repoUrl,刷新后若该仓库仍在列表中则不变,否则保持原值(用户可手动改)。
 */
let refreshReposTimer: ReturnType<typeof setTimeout> | null = null
async function refreshRepos(): Promise<void> {
  if (reposLoading.value) return  // 正在加载,忽略
  if (refreshReposTimer) {
    clearTimeout(refreshReposTimer)
  }
  // 防抖:500ms 内重复点击只执行最后一次
  await new Promise<void>((resolve) => {
    refreshReposTimer = setTimeout(() => resolve(), 500)
  })
  await loadAllRepos(true)
}

/**
 * 仓库下拉框选项(可输入下拉框)。
 * value=clone_url(选中后写入 repoUrl),
 * label=`[平台] owner/repo (私有)?`(带 provider 标记,多平台时便于区分)。
 */
const repoComboboxOptions = computed(() =>
  allRepos.value.map((r) => ({
    value: r.clone_url,
    label: `[${providerDisplayName(r.provider)}] ${r.full_name}${r.private ? ' (私有)' : ''}`,
  })),
)

/** 从下拉选中仓库时,若分支为空则自动填默认分支 */
watch(repoUrl, (url) => {
  if (!url) return
  const repo = allRepos.value.find((r) => r.clone_url === url)
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

    // Agent 策略配置(仅当用户改了用户级默认值时才提交,作为任务级覆盖)
    // 后端 resolve_agent_policy 会合并用户级默认 + 此任务级覆盖;
    // 比较基准 userPolicyDefaults 已在 onMounted 加载用户偏好,未配置时即系统默认
    const agentPolicy: Record<string, unknown> = {}
    if (policyUserAgentEnabled.value !== userPolicyDefaults.value.userAgentEnabled) {
      agentPolicy.user_agent_enabled = policyUserAgentEnabled.value
    }
    if (policyMaxRounds.value !== userPolicyDefaults.value.maxRounds) {
      agentPolicy.max_rounds = policyMaxRounds.value
    }
    if (policyInterval.value !== userPolicyDefaults.value.interval) {
      agentPolicy.checkpoint_interval = policyInterval.value
    }
    if (policyAllowInterrupt.value !== userPolicyDefaults.value.allowInterrupt) {
      agentPolicy.allow_interrupt = policyAllowInterrupt.value
    }
    if (policyMaxInterrupts.value !== userPolicyDefaults.value.maxInterrupts) {
      agentPolicy.max_interrupts_per_round = policyMaxInterrupts.value
    }
    if (policyAllowVerify.value !== userPolicyDefaults.value.allowVerify) {
      agentPolicy.allow_verify = policyAllowVerify.value
    }
    // 关闭高级模式时,专用 K 值强制为 null(用统一值),与协作策略设置页保存逻辑一致;
    // 提交 null 可覆盖用户级默认的专用 K 值
    const intervalBuiltinVal = policyAdvanced.value ? policyIntervalBuiltin.value : null
    const intervalCliVal = policyAdvanced.value ? policyIntervalCli.value : null
    if (intervalBuiltinVal !== userPolicyDefaults.value.intervalBuiltin) {
      agentPolicy.checkpoint_interval_builtin = intervalBuiltinVal
    }
    if (intervalCliVal !== userPolicyDefaults.value.intervalCli) {
      agentPolicy.checkpoint_interval_cli = intervalCliVal
    }
    if (Object.keys(agentPolicy).length > 0) {
      params._agent_policy = agentPolicy
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
      // 测试环境 / 动态验证:仅当启用且填了 URL 时提交
      test_env_url: verifierEnabled.value ? testEnvUrl.value.trim() || undefined : undefined,
      verifier_enabled: verifierEnabled.value,
      verifier_auth_mode: verifierEnabled.value ? verifierAuthMode.value : undefined,
      // 登录凭证:仅提交 label 和 header_value 都非空的项(过滤未填完的空行)
      verifier_auth_tokens: verifierEnabled.value
        ? verifierAuthTokens.value
            .filter((t) => t.label.trim() && t.header_value.trim())
            .map((t) => ({
              label: t.label.trim(),
              header_name: t.header_name.trim() || 'Authorization',
              header_value: t.header_value.trim(),
            }))
        : undefined,
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
  document.addEventListener('click', onDocClickCloseHelp)
  try {
    // 并行拉取场景、模型、各 git provider 状态、技能、agent 配置
    // git provider 状态静默失败:未绑定不影响任务提交
    const [scenarioList, models, ghStatus, giteeStatus, skills, agentCfgs, limits, prefs] = await Promise.all([
      getScenarios(),
      getMyModels().catch(() => null),
      getGitProviderStatus('github').catch(() => null),
      getGitProviderStatus('gitee').catch(() => null),
      getSkills().catch(() => null as SkillSummary[] | null), // 静默失败,无 skill 不阻塞提交
      getAgentConfigs().catch(() => null), // 静默失败,无 agent 配置不影响提交
      getPolicyLimits().catch(() => null), // 静默失败:拿不到限制时保留默认 10
      getPreferences().catch(() => null), // 静默失败:未配置/未登录时用系统默认策略
    ])
    if (limits) MAX_ROUNDS_LIMIT.value = limits.max_rounds
    // 用户级默认策略(协作策略设置页保存的):填充为协作策略表单初始值,
    // 并同步为提交时的比较基准(未配置时表单保持系统默认,行为不变)
    if (prefs?.agent_policy) {
      const p = prefs.agent_policy
      policyUserAgentEnabled.value = p.user_agent_enabled
      policyMaxRounds.value = p.max_rounds
      policyInterval.value = p.checkpoint_interval
      policyIntervalBuiltin.value = p.checkpoint_interval_builtin
      policyIntervalCli.value = p.checkpoint_interval_cli
      policyAllowInterrupt.value = p.allow_interrupt
      policyMaxInterrupts.value = p.max_interrupts_per_round
      policyAllowVerify.value = p.allow_verify
      // 高级模式:仅当任一专用 K 值非 null 时展开(与协作策略设置页一致)
      policyAdvanced.value = p.checkpoint_interval_builtin !== null || p.checkpoint_interval_cli !== null
      // 测试环境授权模式默认值(任务级可单独覆盖)
      verifierAuthMode.value = p.verifier_auth_mode_default
      // 同步比较基准
      userPolicyDefaults.value = {
        userAgentEnabled: policyUserAgentEnabled.value,
        maxRounds: policyMaxRounds.value,
        interval: policyInterval.value,
        intervalBuiltin: policyIntervalBuiltin.value,
        intervalCli: policyIntervalCli.value,
        allowInterrupt: policyAllowInterrupt.value,
        maxInterrupts: policyMaxInterrupts.value,
        allowVerify: policyAllowVerify.value,
        verifierAuthMode: verifierAuthMode.value,
      }
    }
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
    // 记录各 provider 绑定状态(用于决定走下拉选择还是纯输入框)
    if (ghStatus) providerStatus.value.github = { bound: ghStatus.bound }
    if (giteeStatus) providerStatus.value.gitee = { bound: giteeStatus.bound }
    // 已绑定任一平台:预加载仓库列表合并到下拉(失败由弹窗提示)
    if (anyProviderBound.value) loadAllRepos()
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

onUnmounted(() => {
  document.removeEventListener('click', onDocClickCloseHelp)
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
    </AppHeader>

    <div class="page-body">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <main class="main">
        <div class="main-col">
          <!-- 顶部配置区:三行布局(场景 / user_agent 模型 / react_agent 设置) -->
          <div class="topbar">
            <!-- 第 1 行:场景(无标签,直接靠左) -->
            <div class="config-row config-row-scenario">
              <div
                class="scenario-segmented"
                role="tablist"
                aria-label="场景选择"
                data-onboarding="create-scenario"
              >
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
            <div class="config-row" data-onboarding="create-user-model">
              <div class="config-label-group">
                <span class="agent-avatar avatar-user-agent" aria-hidden="true">
                  <BrandLogo :size="22" variant="user-agent" />
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
              <div class="react-controls" data-onboarding="create-react-executor">
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
  
            <!-- 协作策略 + 测试环境(同一行,两个折叠面板并排) -->
            <div class="config-row config-row-scenario config-row-dual">
              <div class="advanced-panel">
                <button
                  type="button"
                  class="advanced-toggle"
                  :aria-expanded="policyOpen"
                  @click="policyOpen = !policyOpen"
                >
                  <svg
                    class="advanced-chevron"
                    :class="{ expanded: policyOpen }"
                    width="16" height="16" viewBox="0 0 24 24" fill="none"
                    stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                  >
                    <polyline points="9 18 15 12 9 6" />
                  </svg>
                  <span>协作策略</span>
                  <span class="advanced-summary">
                    {{ !policyUserAgentEnabled ? '单 agent 模式' : (policyAllowInterrupt ? `每${policyInterval}轮评估·可打断` : `每${policyInterval}轮评估·仅观察`) }}
                  </span>
                </button>
  
                <Transition name="collapse">
                  <div v-show="policyOpen" class="advanced-dropdown advanced-dropdown-left">
                    <!-- 启用 user_agent 开关(最核心,控制全局) -->
                    <label class="policy-toggle-row">
                      <input v-model="policyUserAgentEnabled" type="checkbox" />
                      <span>启用 user_agent</span>
                    </label>
  
                    <!-- 协作总轮次(仅 user_agent 启用时生效) -->
                    <label class="policy-field">
                      <div class="field-head">
                        <span class="policy-label">协作总轮次</span>
                        <div class="field-help-wrap">
                          <button type="button" class="field-help-btn" aria-label="查看说明" @click="toggleMaxRoundsHelp">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10" /><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" /><line x1="12" y1="17" x2="12.01" y2="17" /></svg>
                          </button>
                          <Transition name="help-fade">
                            <div v-if="showMaxRoundsHelp" class="field-help-popover" role="tooltip">
                              user_agent 与 react_agent 之间的协作总轮次。每轮含 react_agent 执行 + user_agent 评估。轮次越多覆盖越全面但耗时越长。仅 user_agent 启用时生效。上限为 {{ MAX_ROUNDS_LIMIT }}。
                            </div>
                          </Transition>
                        </div>
                      </div>
                      <input
                        :value="policyMaxRounds"
                        @input="onMaxRoundsInput"
                        @blur="onMaxRoundsBlur"
                        type="text"
                        inputmode="numeric"
                        pattern="[0-9]*"
                        class="policy-input"
                        :disabled="!policyUserAgentEnabled"
                      />
                      <span class="policy-hint">上限 {{ MAX_ROUNDS_LIMIT }}</span>
                    </label>
  
                    <div class="policy-grid">
                      <label class="policy-field">
                        <span class="policy-label">评估频率 K</span>
                        <input
                          v-model.number="policyInterval"
                          type="number" min="1" max="20"
                          class="policy-input"
                          :disabled="!policyUserAgentEnabled"
                        />
                        <span class="policy-hint">每 K 个迭代评估一次</span>
                      </label>
  
                      <label class="policy-field">
                        <span class="policy-label">每轮最大打断</span>
                        <input
                          v-model.number="policyMaxInterrupts"
                          type="number" min="0" max="10"
                          class="policy-input"
                          :disabled="!policyUserAgentEnabled || !policyAllowInterrupt"
                        />
                        <span class="policy-hint">防死锁上限</span>
                      </label>
                    </div>
  
                    <label class="policy-toggle-row">
                      <input v-model="policyAllowInterrupt" type="checkbox" :disabled="!policyUserAgentEnabled" />
                      <span>允许 user_agent 打断 react_agent</span>
                    </label>
  
                    <label class="policy-toggle-row">
                      <input v-model="policyAllowVerify" type="checkbox" />
                      <span>允许 user_agent 自行验证 <span class="policy-experimental">(实验性)</span></span>
                    </label>
  
                    <label class="policy-toggle-row">
                      <input v-model="policyAdvanced" type="checkbox" :disabled="!policyUserAgentEnabled" />
                      <span>分别配置内置 / CLI agent 的 K 值</span>
                    </label>
  
                    <Transition name="collapse">
                      <div v-show="policyAdvanced" class="policy-grid">
                        <label class="policy-field">
                          <span class="policy-label">内置 react_agent K</span>
                          <input
                            v-model.number="policyIntervalBuiltin"
                            type="number" min="1" max="20"
                            class="policy-input"
                            placeholder="留空用统一值"
                            :disabled="!policyUserAgentEnabled"
                          />
                        </label>
                        <label class="policy-field">
                          <span class="policy-label">CLI agent K</span>
                          <input
                            v-model.number="policyIntervalCli"
                            type="number" min="1" max="20"
                            class="policy-input"
                            placeholder="留空用统一值"
                            :disabled="!policyUserAgentEnabled"
                          />
                        </label>
                      </div>
                    </Transition>
                  </div>
                </Transition>
              </div>
  
              <!-- 测试环境 / 动态验证(可折叠,默认折叠) -->
              <div class="advanced-panel">
                <button
                  type="button"
                  class="advanced-toggle"
                  :aria-expanded="verifierOpen"
                  @click="verifierOpen = !verifierOpen"
                >
                  <svg
                    class="advanced-chevron"
                    :class="{ expanded: verifierOpen }"
                    width="16" height="16" viewBox="0 0 24 24" fill="none"
                    stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                  >
                    <polyline points="9 18 15 12 9 6" />
                  </svg>
                  <span>测试环境</span>
                  <span class="advanced-summary">
                    {{ verifierEnabled
                      ? (verifierAuthMode === 'direct' ? '已启用·直接执行' : '已启用·逐动作授权')
                        + (verifierAuthTokens.filter(t => t.label.trim() && t.header_value.trim()).length > 0
                          ? '·' + verifierAuthTokens.filter(t => t.label.trim() && t.header_value.trim()).length + '个凭证'
                          : '')
                      : '未启用' }}
                  </span>
                </button>
  
                <Transition name="collapse">
                  <div v-show="verifierOpen" class="advanced-dropdown advanced-dropdown-left">
                    <label class="policy-toggle-row">
                      <input v-model="verifierEnabled" type="checkbox" />
                      <span>启用动态验证 <span class="policy-experimental">(实验性)</span></span>
                    </label>
  
                    <Transition name="collapse">
                      <div v-show="verifierEnabled" class="verifier-config">
                        <label class="policy-field">
                          <span class="policy-label">测试环境 URL</span>
                          <input
                            v-model.trim="testEnvUrl"
                            type="url"
                            class="policy-input"
                            placeholder="http://localhost:3000(已部署的应用地址)"
                          />
                          <span class="policy-hint">user_agent 将在此环境动态验证安全发现</span>
                        </label>
  
                        <label class="policy-field">
                          <span class="policy-label">授权模式</span>
                          <select v-model="verifierAuthMode" class="policy-input">
                            <option value="per_action">逐动作授权(每个请求前确认)</option>
                            <option value="direct">直接执行(不弹窗)</option>
                          </select>
                          <span class="policy-hint">控制验证动作执行前是否需要用户确认</span>
                        </label>
  
                        <!-- 登录凭证列表(可选):LLM 按 auth_profile=label 选择身份,
                             工具自动注入对应请求头。用于越权测试(同一端点不同身份访问)。
                             LLM 只看到 label,看不到 header_value(安全)。 -->
                        <div class="auth-tokens-section">
                          <div class="auth-tokens-header">
                            <span class="policy-label">登录凭证 <span class="policy-optional">(可选)</span></span>
                            <button type="button" class="auth-token-add-btn" @click="addAuthToken">
                              + 添加身份
                            </button>
                          </div>
                          <span class="policy-hint">
                            配置不同身份的认证头,LLM 验证越权时会按需选择(如:管理员 vs 普通用户访问同一端点)
                          </span>
  
                          <div
                            v-for="(token, idx) in verifierAuthTokens"
                            :key="idx"
                            class="auth-token-row"
                          >
                            <input
                              v-model.trim="token.label"
                              type="text"
                              class="auth-token-input auth-token-label"
                              placeholder="身份名(如 管理员)"
                            />
                            <input
                              v-model.trim="token.header_name"
                              type="text"
                              class="auth-token-input auth-token-header-name"
                              placeholder="Header 名"
                              list="auth-header-suggestions"
                            />
                            <input
                              v-model.trim="token.header_value"
                              type="text"
                              class="auth-token-input auth-token-header-value"
                              placeholder="Header 值(如 Bearer xxx)"
                            />
                            <button
                              type="button"
                              class="auth-token-remove-btn"
                              aria-label="删除"
                              @click="removeAuthToken(idx)"
                            >×</button>
                          </div>
  
                          <datalist id="auth-header-suggestions">
                            <option value="Authorization" />
                            <option value="Cookie" />
                            <option value="X-API-Key" />
                            <option value="X-Auth-Token" />
                          </datalist>
                        </div>
                      </div>
                    </Transition>
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
              data-onboarding="create-input"
              @keydown="onTextareaKeydown"
            />
  
            <!-- 分隔线 -->
            <div class="chat-divider" />
  
            <!-- 底部:仓库地址 + 分支 + 提交按钮 -->
            <div class="chat-footer">
              <!-- 仓库输入/选择区:可输入下拉框(已绑定任一平台时可从仓库列表选;否则纯输入) -->
              <div class="repo-area">
                <div class="repo-input-row">
                  <!-- 已绑定任一平台:可输入 + 下拉选择仓库(含 GitHub / Gitee 私有仓库) -->
                  <ModelCombobox
                    v-if="anyProviderBound"
                    :model-value="repoUrl"
                    :options="repoComboboxOptions"
                    :disabled="reposLoading"
                    :placeholder="reposLoading ? '加载仓库列表...' : '选择或输入仓库地址(GitHub / Gitee)'"
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
                    aria-label="仓库地址"
                  />
                  <!-- 刷新仓库列表按钮(强制跳过后端缓存,用于在平台上新建仓库后立即拉取) -->
                  <button
                    v-if="anyProviderBound"
                    type="button"
                    class="repo-refresh-btn"
                    :disabled="reposLoading"
                    :title="reposLoading ? '加载中...' : '刷新仓库列表'"
                    aria-label="刷新仓库列表"
                    @click="refreshRepos"
                  >
                    <!-- 旋转动画:loading 时加 .spinning class -->
                    <svg
                      class="refresh-icon"
                      :class="{ spinning: reposLoading }"
                      width="16"
                      height="16"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    >
                      <path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
                      <path d="M3 3v5h5" />
                      <path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16" />
                      <path d="M16 16h5v5" />
                    </svg>
                  </button>
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
                data-onboarding="create-send"
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
        </div>
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
  /* 全宽滚动容器:垂直滚动条贴界面右边,内容在 main-col 内居中 */
  overflow-y: auto;
}

/* 内容列:在滚动容器内居中(与技能管理 / CLI 设置 / 协作策略一致) */
.main-col {
  max-width: 768px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  justify-content: flex-start;
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

/* 协作策略 + 测试环境并排(两个折叠面板同一行) */
.config-row-dual {
  gap: var(--space-4);
  align-items: flex-start;
}

.config-row-dual .advanced-panel {
  flex: 0 0 auto;
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
  background: transparent;
  border-radius: 0;
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
  color: var(--color-text-inverse);
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

/* 深色主题:错误边框改用深红,背景/文字色由 tokens.css 自动切换 */
:global(html[data-theme='dark']) .alert-error {
  border-color: #7f1d1d;
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

/* 刷新仓库列表按钮 */
.repo-refresh-btn {
  flex: 0 0 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  cursor: pointer;
  outline: none;
  transition: color var(--transition-fast), border-color var(--transition-fast);
}

.repo-refresh-btn:hover:not(:disabled) {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.repo-refresh-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

/* loading 时图标持续旋转 */
.refresh-icon.spinning {
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
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
  color: var(--color-text-inverse);
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
  color: var(--color-text-inverse);
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
  border: 2px solid color-mix(in srgb, var(--color-text-inverse) 30%, transparent);
  border-top-color: var(--color-text-inverse);
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

/* 左对齐变体:协作策略行无标签靠左,下拉面板与按钮左对齐 */
.advanced-dropdown-left {
  right: auto;
  left: 0;
}

/* ---- Agent 策略配置面板 ---- */
.policy-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}

.policy-field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.policy-label {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
}

/* 字段头部:标签 + 帮助按钮(问号) */
.field-head {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}

/* 字段级帮助按钮(问号)+ 说明气泡 */
.field-help-wrap {
  position: relative;
  flex-shrink: 0;
  display: inline-flex;
}

.field-help-btn {
  width: 18px;
  height: 18px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-muted);
  background: transparent;
  border: none;
  cursor: pointer;
  transition: color var(--transition-fast);
}

.field-help-btn:hover {
  color: var(--color-primary);
}

.field-help-btn svg {
  width: 16px;
  height: 16px;
  display: block;
}

.field-help-popover {
  position: absolute;
  top: calc(100% + var(--space-1));
  left: 0;
  z-index: 20;
  width: max-content;
  min-width: 2em;
  max-width: min(300px, 80vw);
  max-height: 240px;
  overflow-y: auto;
  padding: var(--space-3);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  word-break: break-word;
}

/* 帮助气泡出现/消失动画 */
.help-fade-enter-active,
.help-fade-leave-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}

.help-fade-enter-from,
.help-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

.policy-input {
  width: 100%;
  height: 34px;
  padding: 0 var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-sm);
}

.policy-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.policy-hint {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.policy-toggle-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) 0;
  font-size: var(--fs-sm);
  color: var(--color-text);
  cursor: pointer;
}

.policy-toggle-row input {
  cursor: pointer;
}

.policy-experimental {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  font-style: italic;
}

/* ---- 测试环境 / 动态验证配置面板 ---- */
.verifier-config {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin-top: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px dashed var(--color-border);
}

.verifier-config .policy-field {
  gap: var(--space-1);
}

.verifier-config select.policy-input {
  height: 34px;
  cursor: pointer;
}

/* ---- 登录凭证列表(verifier_auth_tokens)---- */
.auth-tokens-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: var(--space-1);
}

.auth-tokens-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.policy-optional {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  font-weight: var(--fw-normal);
}

.auth-token-add-btn {
  padding: 2px 10px;
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  background: var(--color-primary-light);
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.auth-token-add-btn:hover {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.auth-token-row {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  margin-top: var(--space-1);
}

.auth-token-input {
  padding: 4px 8px;
  font-size: var(--fs-xs);
  font-family: inherit;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-surface);
  color: var(--color-text);
  transition: border-color var(--transition-fast);
}

.auth-token-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px var(--color-primary-light);
}

.auth-token-label {
  flex: 0 0 110px;
}

.auth-token-header-name {
  flex: 0 0 120px;
}

.auth-token-header-value {
  flex: 1;
  min-width: 0;
}

.auth-token-remove-btn {
  flex: 0 0 auto;
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  line-height: 1;
  color: var(--color-text-muted);
  background: none;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.auth-token-remove-btn:hover {
  background: var(--color-danger-light);
  color: var(--color-danger);
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
  line-clamp: 2;
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
