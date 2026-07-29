<script setup lang="ts">
/**
 * 模型设置页(列表式)
 *
 * 支持配置多个 LLM 与 Embedding 模型,每个配置有唯一 id 和自定义名称。
 * 任务提交时从列表中选择一个使用。
 *
 * 交互:
 * - 顶部"+ 添加"按钮新增配置(生成 uuid,默认展开)
 * - 每个配置是一张卡片,可折叠/展开编辑、删除、测试连通性
 * - 底部"保存"按钮整体提交列表(后端按 id 匹配保留 api_key)
 * - 测试按钮:先保存当前列表,再按 config_id 测试指定配置
 */
import { computed, onMounted, reactive, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import { getCatalog, getMyModels, saveModels, testEmbedding, testLLM } from '@/api/settings'
import { extractErrorMessage } from '@/utils/error'
import type {
  EmbeddingConfigItem,
  EmbeddingConfigItemOut,
  LLMConfigItem,
  LLMConfigItemOut,
  LLMProvider,
  EmbeddingProvider,
  ModelsCatalog,
} from '@/types/settings'

// ---- 厂商清单 ----
const catalog = ref<ModelsCatalog | null>(null)
const loadingCatalog = ref(true)

// ---- LLM 配置列表(带前端编辑状态) ----
interface LLMConfigEditable extends LLMConfigItem {
  has_api_key: boolean
  expanded: boolean
  testing: boolean
}
const llmConfigs = reactive<LLMConfigEditable[]>([])

// ---- Embedding 配置列表 ----
interface EmbeddingConfigEditable extends EmbeddingConfigItem {
  has_api_key: boolean
  expanded: boolean
  testing: boolean
}
const embConfigs = reactive<EmbeddingConfigEditable[]>([])

// ---- 状态 ----
const loadingConfig = ref(true)
const saving = ref(false)
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)

// ============================================================
// 计算属性:当前选中的厂商
// ============================================================

function getLlmProvider(providerId: string): LLMProvider | null {
  if (!catalog.value || !providerId) return null
  return catalog.value.llmProviders.find((p) => p.id === providerId) ?? null
}

function getEmbProvider(providerId: string): EmbeddingProvider | null {
  if (!catalog.value || !providerId) return null
  return catalog.value.embeddingProviders.find((p) => p.id === providerId) ?? null
}

function embDimensionHint(cfg: EmbeddingConfigEditable): string {
  const p = getEmbProvider(cfg.provider)
  if (!p || !cfg.model) return ''
  const m = p.models.find((x) => x.id === cfg.model)
  const dim = m?.dimension ?? p.fallbackDimension ?? 1024
  const mm = m?.multimodal ?? p.fallbackMultimodal ?? false
  const dp = m?.dimensionsParam ?? p.fallbackDimensionsParam ?? false
  let text = `维度: ${dim}`
  if (mm) text += ' · 多模态'
  if (dp) text += ' · 通过 dimensions 参数指定'
  return text
}

// ============================================================
// 厂商切换:自动填充 baseUrl / 刷新思考开关 / 选默认模型
// ============================================================

function onLlmProviderChange(cfg: LLMConfigEditable): void {
  const p = getLlmProvider(cfg.provider)
  cfg.model = ''
  if (!p) {
    cfg.base_url = null
    return
  }
  cfg.base_url = p.baseUrl
  if (p.models.length > 0) {
    cfg.model = p.models[0].id
  }
}

function onEmbProviderChange(cfg: EmbeddingConfigEditable): void {
  const p = getEmbProvider(cfg.provider)
  cfg.model = ''
  if (!p) {
    cfg.base_url = null
    return
  }
  cfg.base_url = p.baseUrl
  if (p.models.length > 0) {
    cfg.model = p.models[0].id
  }
}

function llmThinkingSupported(cfg: LLMConfigEditable): boolean {
  return !!getLlmProvider(cfg.provider)?.supportsThinking
}

function llmThinkingOnly(cfg: LLMConfigEditable): boolean {
  const p = getLlmProvider(cfg.provider)
  if (!p?.supportsThinking) return false
  const m = p.models.find((x) => x.id === cfg.model)
  return (m?.thinking ?? p.fallbackThinking ?? 'none') === 'only'
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
    llmConfigs.splice(0, llmConfigs.length, ...res.llm_configs.map(llmFromOut))
    embConfigs.splice(0, embConfigs.length, ...res.embedding_configs.map(embFromOut))
  } catch (e) {
    showToast(`配置加载失败: ${extractErrorMessage(e)}`, 'error')
  } finally {
    loadingConfig.value = false
  }
}

function llmFromOut(out: LLMConfigItemOut): LLMConfigEditable {
  return {
    id: out.id,
    name: out.name,
    provider: out.provider,
    api_key: '', // 空串=保留已存
    model: out.model,
    enable_thinking: out.enable_thinking,
    base_url: out.base_url,
    has_api_key: out.has_api_key,
    expanded: false,
    testing: false,
  }
}

function embFromOut(out: EmbeddingConfigItemOut): EmbeddingConfigEditable {
  return {
    id: out.id,
    name: out.name,
    provider: out.provider,
    api_key: '',
    model: out.model,
    base_url: out.base_url,
    dimension: out.dimension,
    has_api_key: out.has_api_key,
    expanded: false,
    testing: false,
  }
}

// ============================================================
// 增删
// ============================================================

function addLlmConfig(): void {
  llmConfigs.push({
    id: crypto.randomUUID(),
    name: '',
    provider: '',
    api_key: '',
    model: '',
    enable_thinking: true,
    base_url: null,
    has_api_key: false,
    expanded: true,
    testing: false,
  })
}

function removeLlmConfig(idx: number): void {
  llmConfigs.splice(idx, 1)
}

function addEmbConfig(): void {
  embConfigs.push({
    id: crypto.randomUUID(),
    name: '',
    provider: '',
    api_key: '',
    model: '',
    base_url: null,
    dimension: 1024,
    has_api_key: false,
    expanded: true,
    testing: false,
  })
}

function removeEmbConfig(idx: number): void {
  embConfigs.splice(idx, 1)
}

// ============================================================
// 保存(整体提交)
// ============================================================

async function handleSave(): Promise<boolean> {
  saving.value = true
  toast.value = null
  try {
    // 校验
    for (const cfg of llmConfigs) {
      if (!cfg.provider || !cfg.model) {
        showToast(`LLM 配置"${cfg.name || '未命名'}"缺少厂商或模型`, 'error')
        return false
      }
    }
    for (const cfg of embConfigs) {
      if (!cfg.provider || !cfg.model) {
        showToast(`Embedding 配置"${cfg.name || '未命名'}"缺少厂商或模型`, 'error')
        return false
      }
    }

    const res = await saveModels({
      llm_configs: llmConfigs.map((c) => ({
        id: c.id,
        name: c.name,
        provider: c.provider,
        api_key: c.api_key,
        model: c.model,
        enable_thinking: c.enable_thinking,
        base_url: c.base_url,
      })),
      embedding_configs: embConfigs.map((c) => ({
        id: c.id,
        name: c.name,
        provider: c.provider,
        api_key: c.api_key,
        model: c.model,
        base_url: c.base_url,
        dimension: c.dimension,
      })),
    })
    // 更新 has_api_key 状态 + 清空输入框
    llmConfigs.splice(0, llmConfigs.length, ...res.llm_configs.map(llmFromOut))
    embConfigs.splice(0, embConfigs.length, ...res.embedding_configs.map(embFromOut))
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
// 测试(先保存,再按 config_id 测试)
// ============================================================

async function handleTestLLM(cfg: LLMConfigEditable): Promise<void> {
  cfg.testing = true
  toast.value = null
  try {
    const ok = await handleSave()
    if (!ok) return
    const res = await testLLM({ config_id: cfg.id })
    if (res.success) {
      showToast(`"${cfg.name || cfg.model}" 测试成功 · ${res.latency_ms ?? '?'}ms`, 'success')
    } else {
      showToast(`"${cfg.name || cfg.model}" 测试失败: ${res.message}`, 'error')
    }
  } catch (e) {
    showToast(`测试异常: ${extractErrorMessage(e)}`, 'error')
  } finally {
    cfg.testing = false
  }
}

async function handleTestEmbedding(cfg: EmbeddingConfigEditable): Promise<void> {
  cfg.testing = true
  toast.value = null
  try {
    const ok = await handleSave()
    if (!ok) return
    const res = await testEmbedding({ config_id: cfg.id })
    if (res.success) {
      showToast(
        `"${cfg.name || cfg.model}" 测试成功 · 维度 ${res.dimension ?? '?'} · ${res.latency_ms ?? '?'}ms`,
        'success',
      )
    } else {
      showToast(`"${cfg.name || cfg.model}" 测试失败: ${res.message}`, 'error')
    }
  } catch (e) {
    showToast(`测试异常: ${extractErrorMessage(e)}`, 'error')
  } finally {
    cfg.testing = false
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

function configTitle(name: string, model: string): string {
  return name || model || '未命名配置'
}

const hasLlmConfigs = computed(() => llmConfigs.length > 0)
const hasEmbConfigs = computed(() => embConfigs.length > 0)
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
            <p class="subtitle">配置多个 LLM 与 Embedding 模型,提交任务时选择使用</p>
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
            <div>
              <h2>LLM 大模型</h2>
              <p class="section-desc">用于 user_agent 评估与 react_agent 推理,可配置多个</p>
            </div>
            <button class="btn-add" @click="addLlmConfig">+ 添加</button>
          </div>

          <div v-if="!hasLlmConfigs" class="empty-hint">
            尚未配置 LLM 模型,点击"+ 添加"新增
          </div>

          <div v-for="(cfg, idx) in llmConfigs" :key="cfg.id" class="config-card">
            <div class="card-header" @click="cfg.expanded = !cfg.expanded">
              <span class="card-toggle">{{ cfg.expanded ? '▼' : '▶' }}</span>
              <span class="card-title">{{ configTitle(cfg.name, cfg.model) }}</span>
              <span v-if="cfg.has_api_key" class="badge badge-ok">已配置 Key</span>
              <span v-else class="badge badge-warn">未配置 Key</span>
              <span v-if="cfg.provider" class="card-meta">{{ cfg.provider }} · {{ cfg.model }}</span>
              <div class="card-actions" @click.stop>
                <button class="btn-icon" title="测试" :disabled="cfg.testing" @click="handleTestLLM(cfg)">
                  <span v-if="cfg.testing" class="spinner-sm" />
                  <template v-else>⚡</template>
                </button>
                <button class="btn-icon btn-danger" title="删除" @click="removeLlmConfig(idx)">✕</button>
              </div>
            </div>

            <div v-if="cfg.expanded" class="card-body">
              <div class="field-row">
                <div class="field">
                  <label>名称(可选)</label>
                  <input v-model.trim="cfg.name" type="text" placeholder="如:DeepSeek 日常" />
                </div>
                <div class="field">
                  <label>厂商</label>
                  <select v-model="cfg.provider" @change="onLlmProviderChange(cfg)">
                    <option value="">自定义</option>
                    <option v-for="p in catalog?.llmProviders" :key="p.id" :value="p.id">
                      {{ p.name }}
                    </option>
                  </select>
                </div>
              </div>

              <div class="field">
                <label>API Key</label>
                <input
                  v-model="cfg.api_key"
                  type="password"
                  :placeholder="apiKeyPlaceholder(cfg.has_api_key)"
                />
                <p v-if="cfg.has_api_key" class="field-hint field-hint-success">已配置 API Key</p>
              </div>

              <div class="field-row">
                <div class="field">
                  <label>模型</label>
                  <select v-model="cfg.model" :disabled="!cfg.provider">
                    <option value="" disabled>请选择</option>
                    <option v-for="m in getLlmProvider(cfg.provider)?.models" :key="m.id" :value="m.id">
                      {{ m.id }}
                    </option>
                  </select>
                </div>
                <div class="field">
                  <label>Base URL(可选)</label>
                  <input v-model.trim="cfg.base_url" type="text" placeholder="选厂商后自动填充" />
                </div>
              </div>

              <div v-if="llmThinkingSupported(cfg)" class="field field-checkbox">
                <label>
                  <input
                    v-model="cfg.enable_thinking"
                    type="checkbox"
                    :disabled="llmThinkingOnly(cfg)"
                  />
                  <span>启用深度思考</span>
                </label>
                <p v-if="llmThinkingOnly(cfg)" class="field-hint">该模型为仅思考模式,无法关闭</p>
              </div>
            </div>
          </div>
        </section>

        <!-- ============ Embedding 区 ============ -->
        <section class="config-section">
          <div class="section-header">
            <div>
              <h2>Embedding 模型</h2>
              <p class="section-desc">用于向量化文本(当前仅存储与测试,尚未接入检索),可配置多个</p>
            </div>
            <button class="btn-add" @click="addEmbConfig">+ 添加</button>
          </div>

          <div v-if="!hasEmbConfigs" class="empty-hint">
            尚未配置 Embedding 模型,点击"+ 添加"新增
          </div>

          <div v-for="(cfg, idx) in embConfigs" :key="cfg.id" class="config-card">
            <div class="card-header" @click="cfg.expanded = !cfg.expanded">
              <span class="card-toggle">{{ cfg.expanded ? '▼' : '▶' }}</span>
              <span class="card-title">{{ configTitle(cfg.name, cfg.model) }}</span>
              <span v-if="cfg.has_api_key" class="badge badge-ok">已配置 Key</span>
              <span v-else class="badge badge-warn">未配置 Key</span>
              <span v-if="cfg.provider" class="card-meta">{{ cfg.provider }} · {{ cfg.model }}</span>
              <div class="card-actions" @click.stop>
                <button class="btn-icon" title="测试" :disabled="cfg.testing" @click="handleTestEmbedding(cfg)">
                  <span v-if="cfg.testing" class="spinner-sm" />
                  <template v-else>⚡</template>
                </button>
                <button class="btn-icon btn-danger" title="删除" @click="removeEmbConfig(idx)">✕</button>
              </div>
            </div>

            <div v-if="cfg.expanded" class="card-body">
              <div class="field-row">
                <div class="field">
                  <label>名称(可选)</label>
                  <input v-model.trim="cfg.name" type="text" placeholder="如:DashScope 向量" />
                </div>
                <div class="field">
                  <label>厂商</label>
                  <select v-model="cfg.provider" @change="onEmbProviderChange(cfg)">
                    <option value="">自定义</option>
                    <option v-for="p in catalog?.embeddingProviders" :key="p.id" :value="p.id">
                      {{ p.name }}
                    </option>
                  </select>
                </div>
              </div>

              <div class="field">
                <label>API Key</label>
                <input
                  v-model="cfg.api_key"
                  type="password"
                  :placeholder="apiKeyPlaceholder(cfg.has_api_key)"
                />
              </div>

              <div class="field-row">
                <div class="field">
                  <label>模型</label>
                  <select v-model="cfg.model" :disabled="!cfg.provider">
                    <option value="" disabled>请选择</option>
                    <option v-for="m in getEmbProvider(cfg.provider)?.models" :key="m.id" :value="m.id">
                      {{ m.name }} ({{ m.id }})
                    </option>
                  </select>
                  <p v-if="embDimensionHint(cfg)" class="field-hint">{{ embDimensionHint(cfg) }}</p>
                </div>
                <div class="field">
                  <label>Base URL(可选)</label>
                  <input v-model.trim="cfg.base_url" type="text" placeholder="选厂商后自动填充" />
                </div>
              </div>
            </div>
          </div>
        </section>

        <!-- 底部保存按钮 -->
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
  max-width: 760px;
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
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-4);
}

.section-header h2 {
  font-size: var(--fs-lg);
}

.section-desc {
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
  margin: var(--space-1) 0 0;
}

.empty-hint {
  padding: var(--space-6);
  text-align: center;
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-md);
}

/* ---- 配置卡片 ---- */
.config-card {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  margin-bottom: var(--space-3);
  overflow: hidden;
}

.config-card:last-child {
  margin-bottom: 0;
}

.card-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface-alt);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.card-header:hover {
  background: var(--color-border);
}

.card-toggle {
  font-size: 10px;
  color: var(--color-text-muted);
  flex-shrink: 0;
}

.card-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  flex-shrink: 0;
}

.card-meta {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  margin-left: auto;
  font-family: var(--font-mono);
}

.card-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}

.badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  font-weight: var(--fw-medium);
}

.badge-ok {
  background: var(--color-success-light);
  color: var(--color-success);
}

.badge-warn {
  background: #fef3c7;
  color: #92400e;
}

.card-body {
  padding: var(--space-4);
  border-top: 1px solid var(--color-border);
}

/* ---- 表单字段 ---- */
.field-row {
  display: flex;
  gap: var(--space-4);
}

.field-row .field {
  flex: 1;
}

.field {
  margin-bottom: var(--space-3);
}

.field label {
  display: block;
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  margin-bottom: var(--space-1);
  color: var(--color-text-secondary);
}

.field input,
.field select {
  width: 100%;
  height: 38px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
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

.btn-add {
  height: 32px;
  padding: 0 var(--space-4);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  background: var(--color-primary-light);
  border: 1px solid var(--color-primary-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
}

.btn-add:hover {
  background: var(--color-primary);
  color: white;
}

.btn-icon {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  cursor: pointer;
  border-radius: var(--radius-sm);
  font-size: 13px;
  transition: all var(--transition-fast);
}

.btn-icon:hover:not(:disabled) {
  background: var(--color-surface);
}

.btn-icon:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-danger:hover {
  background: var(--color-danger-light);
  color: var(--color-danger);
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
