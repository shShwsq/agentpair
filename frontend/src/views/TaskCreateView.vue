<script setup lang="ts">
/**
 * 提交任务页
 *
 * 表单字段由选中场景的 form_fields 声明动态渲染(场景无关):
 * - 场景选择(从 GET /scenarios 拉取,每个场景自带表单字段定义)
 * - 使用模型(从 GET /models/models 拉取用户已配置的 LLM 列表)
 * - 动态字段(按场景声明渲染 text/url/textarea/select/number)
 *
 * 提交后:后端立即返回 task_id(异步执行),前端跳转详情页通过 SSE 观看实时进度。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import { createTask, getScenarios } from '@/api/task'
import { getMyModels } from '@/api/settings'
import { extractErrorMessage } from '@/utils/error'
import type { Scenario } from '@/types/task'
import type { LLMConfigItemOut } from '@/types/settings'

const router = useRouter()

/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

// ---- 场景列表 ----

const scenarios = ref<Scenario[]>([])
const selectedScenario = ref('')

/** 当前选中场景的声明(驱动表单字段渲染) */
const selectedScenarioDecl = computed<Scenario | null>(() =>
  scenarios.value.find((s) => s.id === selectedScenario.value) ?? null,
)

// ---- 模型列表 ----

const llmConfigs = ref<LLMConfigItemOut[]>([])
const selectedLlmConfigId = ref('')
const loadingModels = ref(true)

// ---- 动态表单数据 ----

/** 表单字段值,key 对应场景声明 form_fields[].name */
const formData = reactive<Record<string, string>>({})

/** 通用 URL 校验(不绑定 github,场景无关) */
const urlPattern = /^https?:\/\/[^\s/$.?#].[^\s]*$/

/** 按选中场景声明重置表单字段(切场景时调用) */
function resetFormData(s: Scenario | null): void {
  // 清空旧字段
  Object.keys(formData).forEach((k) => delete formData[k])
  // 按声明填默认值
  if (s) {
    for (const f of s.form_fields) {
      formData[f.name] = f.default ?? ''
    }
  }
}

// 切换场景时重置表单
watch(selectedScenario, (newId) => {
  const s = scenarios.value.find((x) => x.id === newId) ?? null
  resetFormData(s)
})

const loading = ref(false)
const error = ref('')

/** 基于场景声明校验字段 */
const errors = computed<Record<string, string>>(() => {
  const e: Record<string, string> = {}
  const s = selectedScenarioDecl.value
  if (!s) return e
  for (const f of s.form_fields) {
    const v = String(formData[f.name] ?? '').trim()
    if (f.required && !v) {
      e[f.name] = `${f.label}不能为空`
      continue
    }
    if (v && f.type === 'url' && !urlPattern.test(v)) {
      e[f.name] = `请输入有效的 ${f.label}`
    }
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
    // 构造 params:从 formData 取场景声明字段的值
    const params: Record<string, unknown> = {}
    const s = selectedScenarioDecl.value
    if (s) {
      for (const f of s.form_fields) {
        const v = String(formData[f.name] ?? '').trim()
        if (v) params[f.name] = v
      }
    }

    // user_input 生成:repo_url 优先,否则用所有字段拼成文本
    // (A5 阶段会进一步场景化,这里先保证通用可用)
    let userInput = ''
    if (params.repo_url) {
      userInput = `请处理这个仓库: ${params.repo_url}`
      if (params.branch) userInput += `\n分支: ${params.branch}`
      if (params.note) userInput += `\n补充说明: ${params.note}`
    } else {
      const parts: string[] = []
      if (s) {
        for (const f of s.form_fields) {
          const v = String(formData[f.name] ?? '').trim()
          if (v) parts.push(`${f.label}: ${v}`)
        }
      }
      userInput = parts.join('\n') || '(无明确指令)'
    }

    // 后端立即返回 task_id(后台线程异步执行)
    const res = await createTask({
      scenario: selectedScenario.value,
      user_input: userInput,
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
    const [scenarioList, models] = await Promise.all([getScenarios(), getMyModels().catch(() => null)])
    scenarios.value = scenarioList
    if (scenarioList.length > 0) {
      selectedScenario.value = scenarioList[0].id // 触发 watch 重置 formData
    }
    if (models && models.llm_configs.length > 0) {
      llmConfigs.value = models.llm_configs
      // 默认选第一个已配置 Key 的,否则选第一个
      const firstWithKey = models.llm_configs.find((c) => c.has_api_key)
      selectedLlmConfigId.value = (firstWithKey ?? models.llm_configs[0]).id
    }
  } catch {
    // 场景拉取失败兜底(不应发生,保留旧默认以便能提交)
    selectedScenario.value = 'code_security_audit'
  } finally {
    loadingModels.value = false
  }
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
      <div class="form-card">
        <h1>提交任务</h1>
        <p class="subtitle">输入任务信息,双智能体将协作完成任务</p>

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

          <!-- 模型选择 -->
          <div class="field">
            <label>使用模型</label>
            <select v-model="selectedLlmConfigId" :disabled="loadingModels">
              <option value="">默认(服务器 env 配置)</option>
              <option
                v-for="cfg in llmConfigs"
                :key="cfg.id"
                :value="cfg.id"
              >
                {{ modelLabel(cfg) }}
              </option>
            </select>
            <p v-if="llmConfigs.length === 0 && !loadingModels" class="field-hint">
              尚未配置模型,将使用服务器默认配置。
              <RouterLink to="/models" class="field-link">前往模型设置 →</RouterLink>
            </p>
          </div>

          <!-- 动态字段(由选中场景的 form_fields 声明驱动) -->
          <div
            v-for="f in selectedScenarioDecl?.form_fields ?? []"
            :key="f.name"
            class="field"
          >
            <label :for="`field-${f.name}`">
              {{ f.label }}<span v-if="f.required" class="required-mark"> *</span>
            </label>
            <input
              v-if="f.type === 'text' || f.type === 'url' || f.type === 'number'"
              :id="`field-${f.name}`"
              v-model.trim="formData[f.name]"
              :type="f.type === 'number' ? 'number' : (f.type === 'url' ? 'url' : 'text')"
              :placeholder="f.placeholder"
              :class="{ invalid: errors[f.name] }"
            />
            <textarea
              v-else-if="f.type === 'textarea'"
              :id="`field-${f.name}`"
              v-model="formData[f.name]"
              rows="3"
              :placeholder="f.placeholder"
              :class="{ invalid: errors[f.name] }"
            />
            <select
              v-else-if="f.type === 'select'"
              :id="`field-${f.name}`"
              v-model="formData[f.name]"
            >
              <option v-for="opt in f.options" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
            </select>
            <p v-if="f.description" class="field-hint">{{ f.description }}</p>
            <span v-if="errors[f.name]" class="field-error">{{ errors[f.name] }}</span>
          </div>

          <button type="submit" class="btn-primary" :disabled="!canSubmit">
            <span v-if="loading" class="spinner" />
            {{ loading ? '处理中...' : '开始任务' }}
          </button>
        </form>
      </div>
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
  max-width: 640px;
  margin: 0 auto;
  overflow-y: auto;
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
.field select,
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

.field select {
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
.field select:focus,
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

.required-mark {
  color: var(--color-danger);
  font-weight: var(--fw-semibold);
}

.field-hint {
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.field-link {
  color: var(--color-primary);
  text-decoration: none;
}

.field-link:hover {
  text-decoration: underline;
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
