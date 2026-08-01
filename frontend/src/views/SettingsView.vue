<script setup lang="ts">
/**
 * 模型设置页(统一表格 + 弹窗编辑)
 *
 * LLM 与 Embedding 配置合并为一张表格展示,通过「类型」列区分。
 * 每条配置有唯一 id 和自定义名称,任务提交时从列表中选择使用。
 *
 * 交互:
 * - 表格列:名称 / 类型 / 厂商·模型 / Key 状态 / 操作(测试·编辑·删除)
 * - 顶部「+ 添加 LLM」「+ 添加 Embedding」→ 打开 ModelConfigDialog
 * - 点击行(非操作区)→ 打开编辑弹窗
 * - 弹窗「确定」→ 写回列表 → 立即整体保存(PUT /settings/models)
 * - 删除 → 从列表移除 → 立即保存
 * - 测试:先保存当前列表,再按 config_id 测试指定配置
 *
 * 由于每次增/改/删都立即持久化,页面不再保留手动「保存设置」按钮。
 */
import { computed, onMounted, reactive, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import ModelConfigDialog from '@/components/ModelConfigDialog.vue'
import { getCatalog, getMyModels, saveModels, testEmbedding, testLLM } from '@/api/settings'
import { extractErrorMessage } from '@/utils/error'
import type {
  EmbeddingConfigItem,
  EmbeddingConfigItemOut,
  LLMConfigItem,
  LLMConfigItemOut,
  ModelsCatalog,
} from '@/types/settings'

type Kind = 'llm' | 'embedding'

// ---- 厂商清单 ----
const catalog = ref<ModelsCatalog | null>(null)
const loadingCatalog = ref(true)

// ---- LLM 配置列表(带前端状态) ----
interface LLMConfigEditable extends LLMConfigItem {
  has_api_key: boolean
  testing: boolean
}
const llmConfigs = reactive<LLMConfigEditable[]>([])

// ---- Embedding 配置列表 ----
interface EmbeddingConfigEditable extends EmbeddingConfigItem {
  has_api_key: boolean
  testing: boolean
}
const embConfigs = reactive<EmbeddingConfigEditable[]>([])

// ---- 状态 ----
const loadingConfig = ref(true)
const saving = ref(false)
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)

// ---- 弹窗状态 ----
const dialogOpen = ref(false)
const dialogMode = ref<'add' | 'edit'>('add')
const dialogKind = ref<Kind>('llm')
type LLMInitial = LLMConfigItem & { has_api_key: boolean }
type EmbInitial = EmbeddingConfigItem & { has_api_key: boolean }
const dialogInitial = ref<LLMInitial | EmbInitial | null>(null)

// ============================================================
// 统一表格行:合并两类配置用于展示,操作时按 kind 分发回原数组
// ============================================================

interface TableRow {
  kind: Kind
  id: string
  name: string
  provider: string
  model: string
  has_api_key: boolean
  testing: boolean
  cfg: LLMConfigEditable | EmbeddingConfigEditable
}

const tableRows = computed<TableRow[]>(() => [
  ...llmConfigs.map((c) => ({
    kind: 'llm' as const,
    id: c.id,
    name: c.name,
    provider: c.provider,
    model: c.model,
    has_api_key: c.has_api_key,
    testing: c.testing,
    cfg: c,
  })),
  ...embConfigs.map((c) => ({
    kind: 'embedding' as const,
    id: c.id,
    name: c.name,
    provider: c.provider,
    model: c.model,
    has_api_key: c.has_api_key,
    testing: c.testing,
    cfg: c,
  })),
])

const hasRows = computed(() => tableRows.value.length > 0)

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
    testing: false,
  }
}

// ============================================================
// 弹窗:打开(新增 / 编辑)
// ============================================================

function openAddDialog(kind: Kind): void {
  dialogKind.value = kind
  dialogMode.value = 'add'
  dialogInitial.value = null
  dialogOpen.value = true
}

function openEditLlm(cfg: LLMConfigEditable): void {
  dialogKind.value = 'llm'
  dialogMode.value = 'edit'
  dialogInitial.value = {
    id: cfg.id,
    name: cfg.name,
    provider: cfg.provider,
    api_key: '',
    model: cfg.model,
    enable_thinking: cfg.enable_thinking,
    base_url: cfg.base_url,
    has_api_key: cfg.has_api_key,
  }
  dialogOpen.value = true
}

function openEditEmb(cfg: EmbeddingConfigEditable): void {
  dialogKind.value = 'embedding'
  dialogMode.value = 'edit'
  dialogInitial.value = {
    id: cfg.id,
    name: cfg.name,
    provider: cfg.provider,
    api_key: '',
    model: cfg.model,
    base_url: cfg.base_url,
    dimension: cfg.dimension,
    has_api_key: cfg.has_api_key,
  }
  dialogOpen.value = true
}

function openEditRow(row: TableRow): void {
  if (row.kind === 'llm') openEditLlm(row.cfg as LLMConfigEditable)
  else openEditEmb(row.cfg as EmbeddingConfigEditable)
}

// 弹窗确认:写回列表 → 立即保存 → 成功则关闭
async function handleDialogConfirm(payload: {
  kind: Kind
  config: LLMConfigItem | EmbeddingConfigItem
}): Promise<void> {
  const { kind, config } = payload
  if (kind === 'llm') {
    const c = config as LLMConfigItem
    const idx = llmConfigs.findIndex((x) => x.id === c.id)
    if (idx >= 0) {
      Object.assign(llmConfigs[idx], c)
    } else {
      llmConfigs.push({ ...c, has_api_key: false, testing: false })
    }
  } else {
    const c = config as EmbeddingConfigItem
    const idx = embConfigs.findIndex((x) => x.id === c.id)
    if (idx >= 0) {
      Object.assign(embConfigs[idx], c)
    } else {
      embConfigs.push({ ...c, has_api_key: false, testing: false })
    }
  }
  const ok = await handleSave()
  if (ok) dialogOpen.value = false
}

// ============================================================
// 删除(立即保存)
// ============================================================

async function removeRow(row: TableRow): Promise<void> {
  if (row.kind === 'llm') {
    const idx = llmConfigs.findIndex((x) => x.id === row.id)
    if (idx >= 0) llmConfigs.splice(idx, 1)
  } else {
    const idx = embConfigs.findIndex((x) => x.id === row.id)
    if (idx >= 0) embConfigs.splice(idx, 1)
  }
  await handleSave()
}

// ============================================================
// 保存(整体提交)
// ============================================================

async function handleSave(opts?: { silent?: boolean }): Promise<boolean> {
  saving.value = true
  toast.value = null
  try {
    // 校验(防御性:列表本应始终合法,因弹窗已做校验)
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
    // 刷新 has_api_key 状态 + 清空输入框
    llmConfigs.splice(0, llmConfigs.length, ...res.llm_configs.map(llmFromOut))
    embConfigs.splice(0, embConfigs.length, ...res.embedding_configs.map(embFromOut))
    if (!opts?.silent) showToast('设置已保存', 'success')
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

async function handleTestRow(row: TableRow): Promise<void> {
  if (row.kind === 'llm') {
    const cfg = row.cfg as LLMConfigEditable
    cfg.testing = true
    toast.value = null
    try {
      const ok = await handleSave({ silent: true })
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
  } else {
    const cfg = row.cfg as EmbeddingConfigEditable
    cfg.testing = true
    toast.value = null
    try {
      const ok = await handleSave({ silent: true })
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

function configTitle(name: string, model: string): string {
  return name || model || '未命名配置'
}

function providerLabel(row: TableRow): string {
  return row.provider || '—'
}

function modelLabel(row: TableRow): string {
  return row.model || '—'
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
        <!-- 页头 + 添加按钮 -->
        <div class="page-header">
          <div>
            <h1>模型设置</h1>
            <p class="subtitle">配置多个 LLM 与 Embedding 模型,提交任务时选择使用</p>
          </div>
          <div class="header-actions">
            <button class="btn-add" :disabled="saving" @click="openAddDialog('llm')">
              + 添加 LLM
            </button>
            <button class="btn-add" :disabled="saving" @click="openAddDialog('embedding')">
              + 添加 Embedding
            </button>
          </div>
        </div>

        <!-- Toast -->
        <Transition name="fade">
          <div v-if="toast" :class="['alert', toast.type === 'error' ? 'alert-error' : 'alert-success']">
            <span>{{ toast.msg }}</span>
          </div>
        </Transition>

        <!-- ============ 统一表格 ============ -->
        <section class="table-section">
          <div class="table-wrap">
            <table class="config-table">
              <thead>
                <tr>
                  <th class="col-name">名称</th>
                  <th class="col-type">类型</th>
                  <th class="col-provider">厂商</th>
                  <th class="col-model">模型</th>
                  <th class="col-key">Key</th>
                  <th class="col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-if="!hasRows" class="empty-row">
                  <td colspan="6">
                    <div class="empty-hint">
                      尚未配置任何模型,点击右上角「+ 添加」新增
                    </div>
                  </td>
                </tr>

                <tr
                  v-for="row in tableRows"
                  :key="`${row.kind}-${row.id}`"
                  class="data-row"
                  @click="openEditRow(row)"
                >
                  <td class="col-name">
                    <span class="cell-title">{{ configTitle(row.name, row.model) }}</span>
                  </td>
                  <td class="col-type">
                    <span :class="['type-tag', row.kind === 'llm' ? 'tag-llm' : 'tag-emb']">
                      {{ row.kind === 'llm' ? 'LLM' : 'Embedding' }}
                    </span>
                  </td>
                  <td class="col-provider">
                    <span class="cell-mono">{{ providerLabel(row) }}</span>
                  </td>
                  <td class="col-model">
                    <span class="cell-mono">{{ modelLabel(row) }}</span>
                  </td>
                  <td class="col-key">
                    <span v-if="row.has_api_key" class="badge badge-ok">已配置</span>
                    <span v-else class="badge badge-warn">未配置</span>
                  </td>
                  <td class="col-actions" @click.stop>
                    <div class="row-actions">
                      <button
                        class="btn-icon"
                        title="测试连通性"
                        :disabled="row.testing || saving"
                        @click="handleTestRow(row)"
                      >
                        <span v-if="row.testing" class="spinner-sm" />
                        <template v-else>⚡</template>
                      </button>
                      <button
                        class="btn-icon"
                        title="编辑"
                        :disabled="saving"
                        @click="openEditRow(row)"
                      >✎</button>
                      <button
                        class="btn-icon btn-danger"
                        title="删除"
                        :disabled="saving"
                        @click="removeRow(row)"
                      >✕</button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <!-- ============ 弹窗 ============ -->
        <ModelConfigDialog
          :open="dialogOpen"
          :kind="dialogKind"
          :mode="dialogMode"
          :initial="dialogInitial"
          :catalog="catalog"
          :saving="saving"
          @confirm="handleDialogConfirm"
          @cancel="dialogOpen = false"
        />
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
  max-width: 920px;
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

.header-actions {
  display: flex;
  gap: var(--space-2);
  flex-shrink: 0;
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

/* ---- 表格区 ---- */
.table-section {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}

.table-wrap {
  overflow-x: auto;
}

.config-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-sm);
}

.config-table thead th {
  text-align: left;
  padding: var(--space-3) var(--space-4);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-bottom: 1px solid var(--color-border);
  white-space: nowrap;
}

.col-name { width: 28%; }
.col-type { width: 110px; }
.col-provider { width: 16%; }
.col-model { width: 22%; }
.col-key { width: 90px; }
.col-actions { width: 130px; text-align: right; }

.config-table tbody td {
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  vertical-align: middle;
}

.config-table tbody tr:last-child td {
  border-bottom: none;
}

.data-row {
  cursor: pointer;
  transition: background var(--transition-fast);
}

.data-row:hover {
  background: var(--color-surface-alt);
}

/* ---- 单元格内容 ---- */
.cell-title {
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cell-mono {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 类型标签 ---- */
.type-tag {
  display: inline-block;
  font-size: 10px;
  font-weight: var(--fw-semibold);
  padding: 2px 8px;
  border-radius: var(--radius-full);
  letter-spacing: 0.02em;
}

.tag-llm {
  background: var(--color-primary-light);
  color: var(--color-primary);
  border: 1px solid var(--color-primary-border);
}

.tag-emb {
  background: var(--color-info-light);
  color: var(--color-info);
  border: 1px solid #bfdbfe;
}

/* ---- 徽标 ---- */
.badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  font-weight: var(--fw-medium);
  white-space: nowrap;
}

.badge-ok {
  background: var(--color-success-light);
  color: var(--color-success);
}

.badge-warn {
  background: #fef3c7;
  color: #92400e;
}

/* ---- 空状态 ---- */
.empty-row td {
  padding: 0;
}

.empty-hint {
  padding: var(--space-10);
  text-align: center;
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
}

/* ---- 操作列 ---- */
.row-actions {
  display: flex;
  gap: 4px;
  justify-content: flex-end;
}

/* ---- 按钮 ---- */
.btn-add {
  height: 32px;
  padding: 0 var(--space-3);
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

.btn-add:hover:not(:disabled) {
  background: var(--color-primary);
  color: white;
}

.btn-add:disabled {
  opacity: 0.6;
  cursor: not-allowed;
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
  color: var(--color-text-secondary);
  transition: all var(--transition-fast);
}

.btn-icon:hover:not(:disabled) {
  background: var(--color-surface);
  color: var(--color-text);
}

.btn-icon:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-danger:hover:not(:disabled) {
  background: var(--color-danger-light);
  color: var(--color-danger);
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
