<script setup lang="ts">
/**
 * 协作策略设置页(/agent-policy)
 *
 * 作为 user_agent 检查点评估的用户级默认配置:
 * - 评估频率 K:每 K 个 react_agent 迭代做一次轻量检查点评估
 * - 打断权限:user_agent 是否能通过中断队列向 react_agent 注入追问指令(软中断)
 * - 验证权限:user_agent 是否能自行调用工具验证(实验性)
 *
 * 与记忆管理(/memory)、模型设置(/models)、CLI 设置(/cli) 并列为主导航项。
 * 后端 API:PUT /memory/preferences/agent_policy(与 user_profile 文本分离保存)。
 *
 * 任务创建时可在 TaskCreateView 做任务级覆盖(不改本页默认值)。
 */
import { computed, onMounted, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import { getPreferences, saveAgentPolicy } from '@/api/memory'
import { extractErrorMessage } from '@/utils/error'
import type { SaveAgentPolicyRequest } from '@/types/memory'

/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

// ============================================================
// 默认策略值(与后端 DEFAULT_AGENT_POLICY 对齐)
// ============================================================
const DEFAULT_POLICY = {
  checkpoint_interval: 3,
  checkpoint_interval_builtin: null as number | null,
  checkpoint_interval_cli: null as number | null,
  allow_interrupt: true,
  max_interrupts_per_round: 2,
  allow_verify: false,
}

// ============================================================
// 表单状态
// ============================================================
/** 统一 K 值:每 K 个迭代评估一次(1-20) */
const policyInterval = ref(DEFAULT_POLICY.checkpoint_interval)
/** 内置 react_agent 专用 K 值(null=用统一值) */
const policyIntervalBuiltin = ref<number | null>(DEFAULT_POLICY.checkpoint_interval_builtin)
/** CLI agent 专用 K 值(null=用统一值) */
const policyIntervalCli = ref<number | null>(DEFAULT_POLICY.checkpoint_interval_cli)
/** user_agent 是否能打断 react_agent */
const policyAllowInterrupt = ref(DEFAULT_POLICY.allow_interrupt)
/** 每轮最多打断次数(防死锁,0-10) */
const policyMaxInterrupts = ref(DEFAULT_POLICY.max_interrupts_per_round)
/** user_agent 是否能自己验证(实验性) */
const policyAllowVerify = ref(DEFAULT_POLICY.allow_verify)
/** 是否分别配置内置/CLI 的 K 值(高级) */
const policyAdvanced = ref(false)

/** 策略原始值(脏检查基准,hydrate 时写入) */
const originalPolicy = ref({
  interval: DEFAULT_POLICY.checkpoint_interval,
  intervalBuiltin: DEFAULT_POLICY.checkpoint_interval_builtin as number | null,
  intervalCli: DEFAULT_POLICY.checkpoint_interval_cli as number | null,
  allowInterrupt: DEFAULT_POLICY.allow_interrupt,
  maxInterrupts: DEFAULT_POLICY.max_interrupts_per_round,
  allowVerify: DEFAULT_POLICY.allow_verify,
})

/** agent 策略是否有未保存改动 */
const policyDirty = computed(() => {
  return (
    policyInterval.value !== originalPolicy.value.interval ||
    policyIntervalBuiltin.value !== originalPolicy.value.intervalBuiltin ||
    policyIntervalCli.value !== originalPolicy.value.intervalCli ||
    policyAllowInterrupt.value !== originalPolicy.value.allowInterrupt ||
    policyMaxInterrupts.value !== originalPolicy.value.maxInterrupts ||
    policyAllowVerify.value !== originalPolicy.value.allowVerify
  )
})

/** 重置策略表单为系统默认值(不立即保存) */
function resetPolicyToDefault(): void {
  policyInterval.value = DEFAULT_POLICY.checkpoint_interval
  policyIntervalBuiltin.value = DEFAULT_POLICY.checkpoint_interval_builtin
  policyIntervalCli.value = DEFAULT_POLICY.checkpoint_interval_cli
  policyAllowInterrupt.value = DEFAULT_POLICY.allow_interrupt
  policyMaxInterrupts.value = DEFAULT_POLICY.max_interrupts_per_round
  policyAllowVerify.value = DEFAULT_POLICY.allow_verify
  policyAdvanced.value = false
}

// ============================================================
// 加载 / 保存
// ============================================================
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)
const updatedAt = ref<string | null>(null)

/** 顶部居中 toast */
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)

function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  setTimeout(() => {
    toast.value = null
  }, 5000)
}

async function loadPolicy(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const data = await getPreferences()
    updatedAt.value = data.updated_at ?? null
    const policy = data.agent_policy
    policyInterval.value = policy?.checkpoint_interval ?? DEFAULT_POLICY.checkpoint_interval
    policyIntervalBuiltin.value = policy?.checkpoint_interval_builtin ?? DEFAULT_POLICY.checkpoint_interval_builtin
    policyIntervalCli.value = policy?.checkpoint_interval_cli ?? DEFAULT_POLICY.checkpoint_interval_cli
    policyAllowInterrupt.value = policy?.allow_interrupt ?? DEFAULT_POLICY.allow_interrupt
    policyMaxInterrupts.value = policy?.max_interrupts_per_round ?? DEFAULT_POLICY.max_interrupts_per_round
    policyAllowVerify.value = policy?.allow_verify ?? DEFAULT_POLICY.allow_verify
    // 高级模式:仅当任一专用 K 值非 null 时展开
    policyAdvanced.value = policyIntervalBuiltin.value !== null || policyIntervalCli.value !== null
    // 同步原始值(脏检查基准)
    originalPolicy.value = {
      interval: policyInterval.value,
      intervalBuiltin: policyIntervalBuiltin.value,
      intervalCli: policyIntervalCli.value,
      allowInterrupt: policyAllowInterrupt.value,
      maxInterrupts: policyMaxInterrupts.value,
      allowVerify: policyAllowVerify.value,
    }
  } catch (err) {
    loadError.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}

async function handleSave(): Promise<void> {
  if (!policyDirty.value || saving.value) return
  saving.value = true
  try {
    const body: SaveAgentPolicyRequest = {
      checkpoint_interval: policyInterval.value,
      // 关闭高级模式时,专用 K 值强制为 null(用统一值)
      checkpoint_interval_builtin: policyAdvanced.value ? policyIntervalBuiltin.value : null,
      checkpoint_interval_cli: policyAdvanced.value ? policyIntervalCli.value : null,
      allow_interrupt: policyAllowInterrupt.value,
      max_interrupts_per_round: policyAllowInterrupt.value ? policyMaxInterrupts.value : 0,
      allow_verify: policyAllowVerify.value,
    }
    const data = await saveAgentPolicy(body)
    updatedAt.value = data.updated_at ?? null
    // 重新 hydrate(后端可能规范化字段)
    const policy = data.agent_policy
    if (policy) {
      policyInterval.value = policy.checkpoint_interval
      policyIntervalBuiltin.value = policy.checkpoint_interval_builtin
      policyIntervalCli.value = policy.checkpoint_interval_cli
      policyAllowInterrupt.value = policy.allow_interrupt
      policyMaxInterrupts.value = policy.max_interrupts_per_round
      policyAllowVerify.value = policy.allow_verify
      policyAdvanced.value = policyIntervalBuiltin.value !== null || policyIntervalCli.value !== null
    }
    originalPolicy.value = {
      interval: policyInterval.value,
      intervalBuiltin: policyIntervalBuiltin.value,
      intervalCli: policyIntervalCli.value,
      allowInterrupt: policyAllowInterrupt.value,
      maxInterrupts: policyMaxInterrupts.value,
      allowVerify: policyAllowVerify.value,
    }
    showToast('协作策略已保存', 'success')
  } catch (err) {
    showToast(extractErrorMessage(err), 'error')
  } finally {
    saving.value = false
  }
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '从未保存'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

onMounted(() => {
  loadPolicy()
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
        <!-- 页头 -->
        <div class="page-header">
          <div>
            <h1>协作策略</h1>
            <p class="page-subtitle">
              user_agent 检查点评估的用户级默认。任务创建时可单独覆盖。
            </p>
          </div>
          <div class="header-meta">
            <span class="meta-label">最后保存</span>
            <span class="meta-value">{{ loading ? '加载中…' : formatTime(updatedAt) }}</span>
          </div>
        </div>

        <!-- 加载态 -->
        <div v-if="loading" class="loading-box">
          <span class="status-spinner" aria-label="加载中" />
          <span>正在加载策略配置…</span>
        </div>

        <!-- 加载失败 -->
        <div v-else-if="loadError" class="alert alert-error" role="alert">
          <span>加载失败:{{ loadError }}</span>
          <button class="btn-link" @click="loadPolicy">重试</button>
        </div>

        <!-- 策略表单 -->
        <section v-else class="policy-card">
          <div class="policy-grid">
            <label class="policy-field">
              <span class="policy-label">评估频率 K</span>
              <input
                v-model.number="policyInterval"
                type="number" min="1" max="20"
                class="policy-input"
                :disabled="saving"
              />
              <span class="policy-hint">每 K 个迭代评估一次</span>
            </label>

            <label class="policy-field">
              <span class="policy-label">每轮最大打断</span>
              <input
                v-model.number="policyMaxInterrupts"
                type="number" min="0" max="10"
                class="policy-input"
                :disabled="saving || !policyAllowInterrupt"
              />
              <span class="policy-hint">防死锁上限</span>
            </label>
          </div>

          <label class="policy-toggle-row">
            <input v-model="policyAllowInterrupt" type="checkbox" :disabled="saving" />
            <span>允许 user_agent 打断 react_agent</span>
          </label>

          <label class="policy-toggle-row">
            <input v-model="policyAllowVerify" type="checkbox" :disabled="saving" />
            <span>允许 user_agent 自行验证 <span class="policy-experimental">(实验性)</span></span>
          </label>

          <label class="policy-toggle-row">
            <input v-model="policyAdvanced" type="checkbox" :disabled="saving" />
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
                  :disabled="saving"
                />
              </label>
              <label class="policy-field">
                <span class="policy-label">CLI agent K</span>
                <input
                  v-model.number="policyIntervalCli"
                  type="number" min="1" max="20"
                  class="policy-input"
                  placeholder="留空用统一值"
                  :disabled="saving"
                />
              </label>
            </div>
          </Transition>

          <!-- 操作区 -->
          <div class="policy-actions">
            <button
              type="button"
              class="btn btn-secondary"
              :disabled="saving || !policyDirty"
              @click="resetPolicyToDefault"
            >恢复默认</button>

            <button
              type="button"
              class="btn btn-primary"
              :disabled="saving || !policyDirty"
              @click="handleSave"
            >
              <span v-if="saving" class="btn-spinner" />
              {{ saving ? '保存中…' : '保存' }}
            </button>

            <span v-if="policyDirty" class="dirty-dot-hint">有未保存改动</span>
          </div>
        </section>

        <!-- 说明区 -->
        <section class="info-section">
          <h2 class="info-title">字段说明</h2>
          <dl class="info-list">
            <div class="info-item">
              <dt>评估频率 K</dt>
              <dd>user_agent 每 K 个 react_agent 迭代做一次轻量检查点评估,判断方向是否跑偏。K 越小评估越频繁(更早纠偏,但开销更大),K 越大开销越小(但跑偏更晚发现)。</dd>
            </div>
            <div class="info-item">
              <dt>打断权限</dt>
              <dd>开启后,user_agent 在检查点评估发现跑偏时,可通过中断队列向 react_agent 注入追问指令(软中断),不强行终止当前迭代。</dd>
            </div>
            <div class="info-item">
              <dt>每轮最大打断</dt>
              <dd>防死锁上限:单轮协作中 user_agent 最多打断 react_agent 的次数。超过此上限后即使发现跑偏也只观察不干预,把控制权交还 react_agent。</dd>
            </div>
            <div class="info-item">
              <dt>分别配置 K 值</dt>
              <dd>高级选项。内置 react_agent 和外部 CLI agent 的迭代节奏可能不同,可分别设置评估频率。留空则使用统一 K 值。</dd>
            </div>
            <div class="info-item">
              <dt>自行验证(实验性)</dt>
              <dd>开启后,user_agent 可自行调用工具验证 react_agent 的产出。目前为实验性开关,默认关闭。</dd>
            </div>
          </dl>
        </section>
      </main>
    </div>

    <!-- 浮动提示弹窗 -->
    <Teleport to="body">
      <Transition name="toast-slide">
        <div
          v-if="toast"
          :class="['toast-popup', toast.type === 'error' ? 'toast-error' : 'toast-success']"
          role="status"
          aria-live="polite"
        >
          <span class="toast-icon" aria-hidden="true">
            <svg
              v-if="toast.type === 'success'"
              width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
            >
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
              <polyline points="22 4 12 14.01 9 11.01" />
            </svg>
            <svg
              v-else
              width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
            >
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
          </span>
          <span class="toast-msg">{{ toast.msg }}</span>
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
  max-width: 760px;
  margin: 0 auto;
  overflow-y: auto;
  padding: var(--space-6) var(--space-5) var(--space-8);
}

/* ---- 页头 ---- */
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-5);
}

.page-header h1 {
  font-size: var(--fs-xl);
  margin: 0;
}

.page-subtitle {
  margin: var(--space-1) 0 0;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

.header-meta {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  flex-shrink: 0;
}

.meta-label {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.meta-value {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
}

/* ---- 加载/错误态 ---- */
.loading-box {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-5);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
}

.alert {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  margin-bottom: var(--space-4);
}

.alert-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border: 1px solid #fecaca;
}

.status-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: status-spin 0.8s linear infinite;
}

@keyframes status-spin {
  to { transform: rotate(360deg); }
}

/* ---- 策略卡片 ---- */
.policy-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  padding: var(--space-5);
  margin-bottom: var(--space-5);
}

.policy-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
  margin-bottom: var(--space-2);
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

.policy-input:disabled {
  color: var(--color-text-muted);
  background: var(--color-surface-alt);
  cursor: not-allowed;
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

.policy-toggle-row input:disabled {
  cursor: not-allowed;
}

.policy-experimental {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  font-style: italic;
}

/* ---- 操作区 ---- */
.policy-actions {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-4);
  padding-top: var(--space-4);
  border-top: 1px solid var(--color-border);
}

.btn {
  height: 36px;
  padding: 0 var(--space-4);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  border: 1px solid transparent;
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-primary {
  background: var(--color-primary);
  color: white;
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-secondary {
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-color: var(--color-border);
}

.btn-secondary:hover:not(:disabled) {
  border-color: var(--color-border-strong);
  color: var(--color-text);
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: btn-spin 0.8s linear infinite;
}

@keyframes btn-spin {
  to { transform: rotate(360deg); }
}

.dirty-dot-hint {
  font-size: var(--fs-xs);
  color: var(--color-primary);
  font-weight: var(--fw-medium);
}

.btn-link {
  background: none;
  border: none;
  padding: 2px 6px;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  cursor: pointer;
  border-radius: var(--radius-sm);
}

.btn-link:hover {
  text-decoration: underline;
}

/* ---- 说明区 ---- */
.info-section {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: var(--space-5);
}

.info-title {
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  margin: 0 0 var(--space-3);
  color: var(--color-text);
}

.info-list {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.info-item dt {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  margin-bottom: 2px;
}

.info-item dd {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  line-height: var(--lh-relaxed);
}

/* ---- collapse 过渡 ---- */
.collapse-enter-active,
.collapse-leave-active {
  transition: opacity var(--transition-fast), max-height var(--transition-fast);
  overflow: hidden;
}

.collapse-enter-from,
.collapse-leave-to {
  opacity: 0;
  max-height: 0;
}

.collapse-enter-to,
.collapse-leave-from {
  opacity: 1;
  max-height: 600px;
}

/* ---- toast ---- */
.toast-popup {
  position: fixed;
  top: var(--space-5);
  left: 50%;
  transform: translateX(-50%);
  z-index: 2000;
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  min-width: 280px;
  max-width: 420px;
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  box-shadow: var(--shadow-lg);
  border: 1px solid transparent;
}

.toast-success {
  background: var(--color-success-light);
  color: var(--color-success);
  border-color: #bbf7d0;
}

.toast-error {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border-color: #fecaca;
}

.toast-icon {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  margin-top: 1px;
}

.toast-msg {
  flex: 1;
  word-break: break-word;
  white-space: pre-wrap;
}

.toast-slide-enter-active,
.toast-slide-leave-active {
  transition: opacity var(--transition-base), transform var(--transition-base);
}

.toast-slide-enter-from,
.toast-slide-leave-to {
  opacity: 0;
  transform: translate(-50%, -12px);
}
</style>
