<script setup lang="ts">
/**
 * 模型设置页
 *
 * 两个独立配置区:
 * - LLM 大模型:厂商/API Key/模型/思考开关/baseUrl
 * - Embedding 模型:厂商/API Key/模型/baseUrl/维度
 *
 * 安全约定:
 * - 已配置的 api_key 不回传原文,输入框 placeholder 提示"已配置,输入新值以替换"
 * - 保存时 api_key 为空表示保留已存的 key(首次保存必须填)
 * - 测试按钮会先保存配置,再用已存配置发起测试
 *
 * 设计参考:C:\Users\njwjx\Documents\BaiduSyncdisk\course_大四\pro\ai-plugin\popup\settings.html
 */
import { computed, onMounted, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import { getCatalog, getMyModels, saveModels, testEmbedding, testLLM } from '@/api/settings'
import { extractErrorMessage } from '@/utils/error'
import type {
  EmbeddingProvider,
  LLMProvider,
  ModelsCatalog,
} from '@/types/settings'

// ---- 厂商清单 ----
const catalog = ref<ModelsCatalog | null>(null)
const loadingCatalog = ref(true)

// ---- LLM 表单 ----
const llmProvider = ref('')
const llmApiKey = ref('')
const llmModel = ref('')
const llmEnableThinking = ref(true)
const llmBaseUrl = ref('')
const llmHasApiKey = ref(false)
const llmThinkingSupported = ref(false)
const llmThinkingOnly = ref(false) // 仅思考模式,强制开启不可关

// ---- Embedding 表单 ----
const embProvider = ref('')
const embApiKey = ref('')
const embModel = ref('')
const embBaseUrl = ref('')
const embHasApiKey = ref(false)

// ---- 状态 ----
const loadingConfig = ref(true)
const saving = ref(false)
const testingLlm = ref(false)
const testingEmb = ref(false)
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)

// ============================================================
// 计算属性:当前选中的厂商与模型元信息
// ============================================================

const currentLlmProvider = computed<LLMProvider | null>(() => {
  if (!catalog.value || !llmProvider.value) return null
  return catalog.value.llmProviders.find((p) => p.id === llmProvider.value) ?? null
})

const currentEmbProvider = computed<EmbeddingProvider | null>(() => {
  if (!catalog.value || !embProvider.value) return null
  return catalog.value.embeddingProviders.find((p) => p.id === embProvider.value) ?? null
})

const embDimensionHint = computed(() => {
  if (!currentEmbProvider.value || !embModel.value) return ''
  const m = currentEmbProvider.value.models.find((x) => x.id === embModel.value)
  const dim = m?.dimension ?? currentEmbProvider.value.fallbackDimension ?? 1024
  const mm = m?.multimodal ?? currentEmbProvider.value.fallbackMultimodal ?? false
  const dp = m?.dimensionsParam ?? currentEmbProvider.value.fallbackDimensionsParam ?? false
  let text = `维度: ${dim}`
  if (mm) text += ' · 多模态'
  if (dp) text += ' · 通过 dimensions 参数指定'
  return text
})

// ============================================================
// 厂商切换:自动填充 baseUrl / 刷新思考开关 / 清空模型
// ============================================================

function onLlmProviderChange(): void {
  const p = currentLlmProvider.value
  llmModel.value = ''
  if (!p) {
    llmBaseUrl.value = ''
    llmThinkingSupported.value = false
    llmThinkingOnly.value = false
    return
  }
  llmBaseUrl.value = p.baseUrl
  llmThinkingSupported.value = !!p.supportsThinking
  // 选厂商后默认选第一个模型
  if (p.models.length > 0) {
    llmModel.value = p.models[0].id
  }
  updateLlmThinkingByModel()
}

function onEmbProviderChange(): void {
  const p = currentEmbProvider.value
  embModel.value = ''
  if (!p) {
    embBaseUrl.value = ''
    return
  }
  embBaseUrl.value = p.baseUrl
  if (p.models.length > 0) {
    embModel.value = p.models[0].id
  }
}

function updateLlmThinkingByModel(): void {
  const p = currentLlmProvider.value
  if (!p || !p.supportsThinking) {
    llmThinkingSupported.value = false
    llmThinkingOnly.value = false
    return
  }
  const m = p.models.find((x) => x.id === llmModel.value)
  const thinking = m?.thinking ?? p.fallbackThinking ?? 'none'
  llmThinkingOnly.value = thinking === 'only'
  if (llmThinkingOnly.value) {
    llmEnableThinking.value = true
  } else if (thinking === 'hybrid' && m?.thinkingDefault !== undefined) {
    llmEnableThinking.value = m.thinkingDefault
  }
}

// ============================================================
// 加载:catalog + 用户已存配置
// ============================================================

onMounted(async () => {
  await Promise.all([loadCatalog(), loadConfig()])
})

async function loadCatalog(): Promise<void> {
  loadingCatalog.value = true
  try {
    catalog.value = await getCatalog()
  } catch (e) {
    showToast(`厂商清单加载失败: ${extractErrorMessage(e)}`, 'error')
  } finally {
    loadingCatalog.value = false
  }
}

async function loadConfig(): Promise<void> {
  loadingConfig.value = true
  try {
    const res = await getMyModels()
    // LLM
    if (res.llm) {
      llmProvider.value = res.llm.provider ?? ''
      llmModel.value = res.llm.model ?? ''
      llmEnableThinking.value = res.llm.enable_thinking
      llmBaseUrl.value = res.llm.base_url ?? ''
      llmHasApiKey.value = res.llm.has_api_key
      // 触发思考开关刷新(不重新填 baseUrl,保留已存值)
      if (llmProvider.value) {
        const p = catalog.value?.llmProviders.find((x) => x.id === llmProvider.value)
        llmThinkingSupported.value = !!p?.supportsThinking
        const m = p?.models.find((x) => x.id === llmModel.value)
        const thinking = m?.thinking ?? p?.fallbackThinking ?? 'none'
        llmThinkingOnly.value = thinking === 'only'
      }
    }
    // Embedding
    if (res.embedding) {
      embProvider.value = res.embedding.provider ?? ''
      embModel.value = res.embedding.model ?? ''
      embBaseUrl.value = res.embedding.base_url ?? ''
      embHasApiKey.value = res.embedding.has_api_key
    }
  } catch (e) {
    showToast(`配置加载失败: ${extractErrorMessage(e)}`, 'error')
  } finally {
    loadingConfig.value = false
  }
}

// ============================================================
// 保存
// ============================================================

async function handleSave(): Promise<boolean> {
  saving.value = true
  toast.value = null
  try {
    const req: import('@/types/settings').SaveModelsRequest = {}
    // LLM:有选厂商才保存
    if (llmProvider.value) {
      if (!llmModel.value) {
        showToast('请选择 LLM 模型', 'error')
        return false
      }
      req.llm = {
        provider: llmProvider.value,
        api_key: llmApiKey.value,
        model: llmModel.value,
        enable_thinking: llmEnableThinking.value,
        base_url: llmBaseUrl.value || null,
      }
    }
    // Embedding:有选厂商才保存
    if (embProvider.value) {
      if (!embModel.value) {
        showToast('请选择 Embedding 模型', 'error')
        return false
      }
      req.embedding = {
        provider: embProvider.value,
        api_key: embApiKey.value,
        model: embModel.value,
        base_url: embBaseUrl.value || null,
        dimension: 1024,
      }
    }
    if (!req.llm && !req.embedding) {
      showToast('请至少配置一项', 'error')
      return false
    }
    const res = await saveModels(req)
    // 更新 has_api_key 状态,清空输入框(已保存到后端)
    if (res.llm) {
      llmHasApiKey.value = res.llm.has_api_key
      llmApiKey.value = ''
    }
    if (res.embedding) {
      embHasApiKey.value = res.embedding.has_api_key
      embApiKey.value = ''
    }
    showToast('设置已保存', 'success')
    return true
  } catch (e) {
    showToast(`保存失败: ${extractErrorMessage(e)}`, 'error')
    return false
  } finally {
    saving.value = false
  }
}

// ============================================================
// 测试(先保存,再用已存配置测试)
// ============================================================

async function handleTestLLM(): Promise<void> {
  testingLlm.value = true
  toast.value = null
  try {
    const ok = await handleSave()
    if (!ok) return
    const res = await testLLM()
    if (res.success) {
      showToast(`LLM 测试成功 · 耗时 ${res.latency_ms ?? '?'}ms`, 'success')
    } else {
      showToast(res.message, 'error')
    }
  } catch (e) {
    showToast(`测试异常: ${extractErrorMessage(e)}`, 'error')
  } finally {
    testingLlm.value = false
  }
}

async function handleTestEmbedding(): Promise<void> {
  testingEmb.value = true
  toast.value = null
  try {
    const ok = await handleSave()
    if (!ok) return
    const res = await testEmbedding()
    if (res.success) {
      showToast(
        `Embedding 测试成功 · 维度 ${res.dimension ?? '?'} · 耗时 ${res.latency_ms ?? '?'}ms`,
        'success',
      )
    } else {
      showToast(res.message, 'error')
    }
  } catch (e) {
    showToast(`测试异常: ${extractErrorMessage(e)}`, 'error')
  } finally {
    testingEmb.value = false
  }
}

// ============================================================
// 工具
// ============================================================

function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  setTimeout(() => {
    toast.value = null
  }, 4500)
}

function apiKeyPlaceholder(hasKey: boolean): string {
  return hasKey ? '已配置,输入新值以替换(留空则保留)' : 'sk-...'
}
</script>

<template>
  <div class="page">
    <AppHeader>
      <template #nav>
        <RouterLink to="/">首页</RouterLink>
        <RouterLink to="/tasks/new">提交任务</RouterLink>
        <RouterLink to="/settings" class="router-link-active">模型设置</RouterLink>
      </template>
    </AppHeader>

    <main class="main">
      <!-- 加载中 -->
      <div v-if="loadingCatalog || loadingConfig" class="loading">
        <div class="spinner" />
        <span>加载中...</span>
      </div>

      <template v-else>
        <!-- 顶部保存按钮 -->
        <div class="page-header">
          <div>
            <h1>模型设置</h1>
            <p class="subtitle">配置 LLM 与 Embedding 模型,任务执行时自动使用你的配置</p>
          </div>
          <button class="btn-primary" :disabled="saving" @click="handleSave">
            <span v-if="saving" class="spinner-sm" />
            {{ saving ? '保存中...' : '保存设置' }}
          </button>
        </div>

        <!-- Toast -->
        <Transition name="fade">
          <div v-if="toast" :class="['alert', toast.type === 'error' ? 'alert-error' : 'alert-success']">
            <span>{{ toast.msg }}</span>
          </div>
        </Transition>

        <!-- ============ LLM 区 ============ -->
        <section class="config-section">
          <div class="section-header">
            <h2>LLM 大模型</h2>
            <button class="btn-secondary" :disabled="testingLlm" @click="handleTestLLM">
              {{ testingLlm ? '测试中...' : '测试 LLM' }}
            </button>
          </div>
          <p class="section-desc">用于 user_agent 评估与 react_agent 推理</p>

          <div class="field">
            <label>厂商</label>
            <select v-model="llmProvider" @change="onLlmProviderChange">
              <option value="">自定义(手动填写下方字段)</option>
              <option v-for="p in catalog?.llmProviders" :key="p.id" :value="p.id">
                {{ p.name }}
              </option>
            </select>
          </div>

          <div class="field">
            <label>API Key</label>
            <input
              v-model="llmApiKey"
              type="password"
              :placeholder="apiKeyPlaceholder(llmHasApiKey)"
            />
            <p v-if="llmHasApiKey" class="field-hint field-hint-success">已配置 API Key</p>
            <a
              v-else-if="currentLlmProvider?.apiKeyUrl"
              :href="currentLlmProvider.apiKeyUrl"
              target="_blank"
              class="field-link"
            >
              在 {{ currentLlmProvider.name }} 获取 API Key →
            </a>
          </div>

          <div class="field">
            <label>模型</label>
            <select v-model="llmModel" @change="updateLlmThinkingByModel" :disabled="!llmProvider">
              <option value="" disabled>请选择模型</option>
              <option v-for="m in currentLlmProvider?.models" :key="m.id" :value="m.id">
                {{ m.id }}
              </option>
            </select>
          </div>

          <div v-if="llmThinkingSupported" class="field field-checkbox">
            <label>
              <input
                v-model="llmEnableThinking"
                type="checkbox"
                :disabled="llmThinkingOnly"
              />
              <span>启用深度思考(质量更高、耗时更长)</span>
            </label>
            <p v-if="llmThinkingOnly" class="field-hint">该模型为仅思考模式,无法关闭</p>
          </div>

          <div class="field">
            <label>Base URL(可选)</label>
            <input v-model.trim="llmBaseUrl" type="text" placeholder="选厂商后自动填充,可手动修改" />
            <p class="field-hint">留空使用厂商预设;自定义厂商时需手动填写</p>
          </div>
        </section>

        <!-- ============ Embedding 区 ============ -->
        <section class="config-section">
          <div class="section-header">
            <h2>Embedding 模型</h2>
            <button class="btn-secondary" :disabled="testingEmb" @click="handleTestEmbedding">
              {{ testingEmb ? '测试中...' : '测试 Embedding' }}
            </button>
          </div>
          <p class="section-desc">用于向量化文本(当前阶段仅存储与测试,尚未接入检索流程)</p>

          <div class="field">
            <label>厂商</label>
            <select v-model="embProvider" @change="onEmbProviderChange">
              <option value="">自定义(手动填写下方字段)</option>
              <option v-for="p in catalog?.embeddingProviders" :key="p.id" :value="p.id">
                {{ p.name }}
              </option>
            </select>
          </div>

          <div class="field">
            <label>API Key</label>
            <input
              v-model="embApiKey"
              type="password"
              :placeholder="apiKeyPlaceholder(embHasApiKey)"
            />
            <p v-if="embHasApiKey" class="field-hint field-hint-success">已配置 API Key</p>
            <a
              v-else-if="currentEmbProvider?.apiKeyUrl"
              :href="currentEmbProvider.apiKeyUrl"
              target="_blank"
              class="field-link"
            >
              在 {{ currentEmbProvider.name }} 获取 API Key →
            </a>
          </div>

          <div class="field">
            <label>模型</label>
            <select v-model="embModel" :disabled="!embProvider">
              <option value="" disabled>请选择模型</option>
              <option v-for="m in currentEmbProvider?.models" :key="m.id" :value="m.id">
                {{ m.name }} ({{ m.id }})
              </option>
            </select>
            <p v-if="embDimensionHint" class="field-hint">{{ embDimensionHint }}</p>
          </div>

          <div class="field">
            <label>Base URL(可选)</label>
            <input v-model.trim="embBaseUrl" type="text" placeholder="选厂商后自动填充,可手动修改" />
          </div>
        </section>

        <!-- 底部保存按钮(移动端友好) -->
        <div class="bottom-actions">
          <button class="btn-primary" :disabled="saving" @click="handleSave">
            <span v-if="saving" class="spinner-sm" />
            {{ saving ? '保存中...' : '保存设置' }}
          </button>
        </div>
      </template>
    </main>
  </div>
</template>

<style scoped>
.page {
  min-height: 100vh;
  background: var(--color-bg);
}

.main {
  max-width: 720px;
  margin: 0 auto;
  padding: var(--space-8) var(--space-6) var(--space-12);
}

.loading {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-16);
  color: var(--color-text-secondary);
}

.spinner {
  width: 32px;
  height: 32px;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.spinner-sm {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* ---- 页头 ---- */
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-6);
}

.page-header h1 {
  font-size: var(--fs-xl);
  margin-bottom: var(--space-1);
}

.subtitle {
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
}

/* ---- 提示 ---- */
.alert {
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  margin-bottom: var(--space-4);
}

.alert-success {
  background: var(--color-success-light);
  color: var(--color-success);
  border: 1px solid #bbf7d0;
}

.alert-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border: 1px solid #fecaca;
}

/* ---- 配置区 ---- */
.config-section {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  padding: var(--space-6);
  margin-bottom: var(--space-5);
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.section-header h2 {
  font-size: var(--fs-lg);
}

.section-desc {
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
  margin: var(--space-1) 0 var(--space-5);
}

/* ---- 表单字段 ---- */
.field {
  margin-bottom: var(--space-4);
}

.field label {
  display: block;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  margin-bottom: var(--space-2);
}

.field input,
.field select {
  width: 100%;
  height: 42px;
  padding: 0 var(--space-3);
  font-size: var(--fs-base);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.field input::placeholder {
  color: var(--color-text-muted);
}

.field input:focus,
.field select:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.field input:disabled,
.field select:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.field-checkbox label {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-weight: var(--fw-normal);
  cursor: pointer;
}

.field-checkbox input {
  width: auto;
  height: auto;
  margin: 0;
}

.field-hint {
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.field-hint-success {
  color: var(--color-success);
}

.field-link {
  display: inline-block;
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-primary);
  text-decoration: none;
}

.field-link:hover {
  text-decoration: underline;
}

/* ---- 按钮 ---- */
.btn-primary {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  height: 40px;
  padding: 0 var(--space-5);
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: white;
  background: var(--color-primary);
  border-radius: var(--radius-md);
  transition: background var(--transition-fast);
  white-space: nowrap;
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn-secondary {
  height: 32px;
  padding: 0 var(--space-4);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  background: var(--color-primary-light);
  border: 1px solid var(--color-primary-border);
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.btn-secondary:hover:not(:disabled) {
  background: var(--color-primary);
  color: white;
}

.btn-secondary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.bottom-actions {
  margin-top: var(--space-6);
}

.bottom-actions .btn-primary {
  width: 100%;
  height: 44px;
}

/* ---- 过渡 ---- */
.fade-enter-active,
.fade-leave-active {
  transition: opacity var(--transition-fast);
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
