<script setup lang="ts">
/**
 * 设置页(表格 + 弹窗)
 *
 * 账号相关配置合并为一张表格展示,点击行打开对应弹窗编辑:
 * - 登录密码:已设密码 → 修改(验证当前密码);未设密码 → 设置
 * - GitHub 账号:已绑定 → 查看 + 解绑;未绑定 → 跳转授权页绑定
 *
 * 与 ModelSettingsView 风格一致(表格 + 弹窗 + 顶部居中 toast)。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import AgentConfigDialog from '@/components/AgentConfigDialog.vue'
import DeleteAccountDialog from '@/components/DeleteAccountDialog.vue'
import GitHubDialog from '@/components/GitHubDialog.vue'
import PasswordDialog from '@/components/PasswordDialog.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import { changePassword, deleteAccount } from '@/api/auth'
import {
  deleteAgentConfig,
  getAgentConfig,
  getAgentConfigs,
  getAgentTypes,
  saveAgentConfig,
  testAgentConfig,
} from '@/api/agent_configs'
import { getGitHubBindURL, getGitHubStatus, syncEmail, unbindGitHub } from '@/api/github'
import type { GitHubStatus } from '@/types/github'
import type {
  AgentConfigDetailOut,
  AgentConfigOut,
  AgentTestResult,
  AgentTypeMeta,
  CredentialValue,
} from '@/types/agent_configs'
import { useAuthStore } from '@/stores/auth'
import { extractErrorMessage } from '@/utils/error'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

/** OAuth 用户未设密码时为 false */
const hasPassword = computed(() => authStore.user?.has_password ?? false)

// ============================================================
// Toast(顶部居中,5s 自动消失)
// ============================================================
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)

function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  setTimeout(() => {
    toast.value = null
  }, 5000)
}

// ============================================================
// 密码弹窗
// ============================================================
const pwdDialogOpen = ref(false)
const pwdLoading = ref(false)
const pwdError = ref('')

function openPasswordDialog(): void {
  pwdError.value = ''
  pwdDialogOpen.value = true
}

async function handlePasswordConfirm(payload: {
  current_password?: string
  new_password: string
}): Promise<void> {
  pwdError.value = ''
  pwdLoading.value = true
  try {
    await changePassword({
      current_password: payload.current_password,
      new_password: payload.new_password,
    })
    pwdDialogOpen.value = false
    showToast('密码已修改,将自动登出...', 'success')
    // 修改成功后登出并跳转登录页
    setTimeout(() => {
      authStore.logout()
      router.push('/login')
    }, 1500)
  } catch (err) {
    pwdError.value = extractErrorMessage(err)
  } finally {
    pwdLoading.value = false
  }
}

// ============================================================
// GitHub 弹窗
// ============================================================
const ghDialogOpen = ref(false)
const githubStatus = ref<GitHubStatus | null>(null)
const githubLoading = ref(false)
const githubAction = ref<'bind' | 'unbind' | ''>('')
const githubError = ref('')
const githubSuccess = ref('')

async function refreshGitHubStatus(): Promise<void> {
  githubLoading.value = true
  githubError.value = ''
  try {
    githubStatus.value = await getGitHubStatus()
  } catch (err) {
    // 静默失败,弹窗内会显示失败态
    console.warn('加载 GitHub 状态失败:', err)
  } finally {
    githubLoading.value = false
  }
}

function openGitHubDialog(): void {
  githubError.value = ''
  githubSuccess.value = ''
  ghDialogOpen.value = true
  if (!githubStatus.value) refreshGitHubStatus()
}

function handleBind(): void {
  // 跳到 GitHub 授权页,回调后由 OAuthCallbackView 完成绑定
  githubAction.value = 'bind'
  window.location.href = getGitHubBindURL()
}

async function handleUnbind(): Promise<void> {
  githubAction.value = 'unbind'
  githubError.value = ''
  githubSuccess.value = ''
  try {
    githubStatus.value = await unbindGitHub()
    githubSuccess.value = '已解绑 GitHub'
    showToast('已解绑 GitHub,任务执行将无法访问你的私有仓库', 'success')
    setTimeout(() => {
      githubSuccess.value = ''
    }, 5000)
  } catch (err) {
    githubError.value = extractErrorMessage(err)
  } finally {
    githubAction.value = ''
  }
}

// ============================================================
// 智能体 CLI 配置弹窗(动态多 agent)
// ============================================================
/** 所有已注册 agent 类型(后端动态返回,含凭据字段定义) */
const agentTypes = ref<AgentTypeMeta[]>([])
/** 当前用户已配置的 agent 列表(用于表格状态列) */
const agentConfigs = ref<AgentConfigOut[]>([])
/** agentTypes 是否仍在加载 */
const agentTypesLoading = ref(true)

const agentDialogOpen = ref(false)
/** 当前弹窗对应的 agent 类型元数据 */
const agentDialogMeta = ref<AgentTypeMeta | null>(null)
/** 当前弹窗对应的 agent 配置详情(null=未配置或加载中) */
const agentDialogDetail = ref<AgentConfigDetailOut | null>(null)
/** 弹窗加载详情中(用于禁用表单) */
const agentDialogLoading = ref(false)
const agentSaving = ref(false)
const agentError = ref('')
/** 测试连接中(禁用测试按钮 + spinner) */
const agentTesting = ref(false)
/** 测试结果(null=未测试) */
const agentTestResult = ref<AgentTestResult | null>(null)

/** 按类型查找已配置项(用于表格状态展示) */
function findConfig(agent_type: string): AgentConfigOut | null {
  return agentConfigs.value.find((c) => c.agent_type === agent_type) ?? null
}

/** 刷新用户已配置的 agent 列表 */
async function refreshAgentConfigs(): Promise<void> {
  try {
    const res = await getAgentConfigs()
    agentConfigs.value = res.configs
  } catch (err) {
    console.warn('加载 agent 配置列表失败:', err)
  }
}

async function openAgentDialog(meta: AgentTypeMeta): Promise<void> {
  agentDialogMeta.value = meta
  agentDialogDetail.value = null
  agentDialogLoading.value = true
  agentError.value = ''
  agentTestResult.value = null
  agentDialogOpen.value = true
  try {
    agentDialogDetail.value = await getAgentConfig(meta.agent_type)
  } catch (err) {
    // 未配置时后端可能 404,静默处理:detail 保持 null(按未配置渲染)
    console.warn('加载 agent 配置详情失败:', err)
  } finally {
    agentDialogLoading.value = false
  }
}

async function handleAgentSave(credentials: CredentialValue[], is_active: boolean): Promise<void> {
  const meta = agentDialogMeta.value
  if (!meta) return
  agentError.value = ''
  agentSaving.value = true
  try {
    agentDialogDetail.value = await saveAgentConfig(meta.agent_type, { credentials, is_active })
    await refreshAgentConfigs()
    // 保存成功后不自动关闭弹窗,让用户可继续点「测试连接」验证
    // 清空旧的测试结果(凭证可能已变)
    agentTestResult.value = null
    showToast(`${meta.display_name} 配置已保存`, 'success')
  } catch (err) {
    agentError.value = extractErrorMessage(err)
  } finally {
    agentSaving.value = false
  }
}

async function handleAgentClear(): Promise<void> {
  const meta = agentDialogMeta.value
  if (!meta) return
  agentError.value = ''
  agentSaving.value = true
  try {
    await deleteAgentConfig(meta.agent_type)
    await refreshAgentConfigs()
    agentDialogDetail.value = null
    agentTestResult.value = null
    agentDialogOpen.value = false
    showToast(`${meta.display_name} 配置已清除`, 'success')
  } catch (err) {
    agentError.value = extractErrorMessage(err)
  } finally {
    agentSaving.value = false
  }
}

async function handleAgentTest(): Promise<void> {
  const meta = agentDialogMeta.value
  if (!meta) return
  agentError.value = ''
  agentTesting.value = true
  agentTestResult.value = null
  try {
    agentTestResult.value = await testAgentConfig(meta.agent_type)
  } catch (err) {
    // 网络错误/后端 500 等:构造一个失败结果展示
    agentTestResult.value = {
      ok: false,
      message: extractErrorMessage(err),
    }
  } finally {
    agentTesting.value = false
  }
}

// ============================================================
// 表格行
// ============================================================
interface SettingRow {
  /** 行唯一 key:password/github/delete 固定;agent 行用 'agent:'+agent_type */
  key: string
  item: string
  desc: string
  status: string
  statusType: 'ok' | 'warn' | 'neutral' | 'danger'
  actionText: string
  loading?: boolean
  /** 关联的 agent 类型元数据(仅 agent 行有),用于打开弹窗 */
  agentMeta?: AgentTypeMeta
}

const rows = computed<SettingRow[]>(() => {
  const fixed: SettingRow[] = [
    {
      key: 'password',
      item: '登录密码',
      desc: hasPassword.value ? '修改账号登录密码' : 'OAuth 账号设置密码',
      status: hasPassword.value ? '已设置' : '未设置',
      statusType: hasPassword.value ? 'ok' : 'warn',
      actionText: hasPassword.value ? '修改' : '设置',
      loading: false,
    },
    {
      key: 'github',
      item: 'GitHub 账号',
      desc: '绑定后可访问私有仓库',
      status: githubStatus.value?.bound
        ? `@${githubStatus.value.github_login || 'unknown'}`
        : githubStatus.value === null
          ? ''  // 加载中,由 template 渲染 spinner
          : '未绑定',
      statusType: githubStatus.value?.bound
        ? 'ok'
        : githubStatus.value === null
          ? 'neutral'
          : 'warn',
      actionText: githubStatus.value?.bound ? '管理' : '绑定',
      loading: githubStatus.value === null,
    },
  ]

  // 动态 agent 行:遍历后端注册的 agent 类型,每种一行
  const agentRows: SettingRow[] = agentTypes.value.map((meta) => {
    const cfg = findConfig(meta.agent_type)
    const configured = !!cfg && cfg.has_credentials
    // agentTypes 未加载完时显示加载态
    const loading = agentTypesLoading.value
    return {
      key: `agent:${meta.agent_type}`,
      item: meta.display_name,
      desc: meta.description,
      status: loading
        ? ''
        : configured
          ? '已设置'
          : '未设置',
      statusType: loading
        ? 'neutral'
        : configured
          ? 'ok'
          : 'warn',
      actionText: configured ? '修改' : '设置',
      loading,
      agentMeta: meta,
    }
  })

  const tail: SettingRow[] = [
    {
      key: 'delete',
      item: '删除账号',
      desc: '永久删除账号及所有数据',
      status: '不可恢复',
      statusType: 'danger',
      actionText: '删除',
    },
  ]

  return [...fixed, ...agentRows, ...tail]
})

function openRow(row: SettingRow): void {
  if (row.key === 'password') openPasswordDialog()
  else if (row.key === 'github') openGitHubDialog()
  else if (row.key === 'delete') openDeleteDialog()
  else if (row.agentMeta) openAgentDialog(row.agentMeta)
}

// ============================================================
// 删除账号弹窗
// ============================================================
const deleteDialogOpen = ref(false)
const deleteLoading = ref(false)
const deleteError = ref('')
const currentEmail = computed(() => authStore.user?.email ?? '')

function openDeleteDialog(): void {
  deleteError.value = ''
  deleteDialogOpen.value = true
}

async function handleDeleteConfirm(email: string): Promise<void> {
  deleteError.value = ''
  deleteLoading.value = true
  try {
    await deleteAccount(email)
    deleteDialogOpen.value = false
    // 删除成功 → 登出并跳转登录页
    authStore.logout()
    await router.push('/login')
    showToast('账号已删除', 'success')
  } catch (err) {
    deleteError.value = extractErrorMessage(err)
  } finally {
    deleteLoading.value = false
  }
}

// ============================================================
// 邮箱同步弹窗(绑定后 GitHub 邮箱与账号邮箱不一致时触发)
// ============================================================
const syncDialogOpen = ref(false)
const syncGithubEmail = ref('')
const syncCurrentEmail = ref('')
const syncLoading = ref(false)
const syncError = ref('')

async function handleSyncConfirm(): Promise<void> {
  syncError.value = ''
  syncLoading.value = true
  try {
    await syncEmail()
    // 重新拉取用户信息,更新本地 email
    await authStore.fetchMe()
    syncDialogOpen.value = false
    showToast('邮箱已同步为 GitHub 邮箱', 'success')
    // 同步后刷新 GitHub 状态(绑定状态不变,但邮箱已更新)
    refreshGitHubStatus()
  } catch (err) {
    syncError.value = extractErrorMessage(err)
  } finally {
    syncLoading.value = false
  }
}

onMounted(() => {
  // 预加载 GitHub 状态(用于表格状态列展示)
  refreshGitHubStatus()
  // 并行加载 agent 类型列表 + 用户已配置列表
  loadAgentTypesAndConfigs()

  // 检测 OAuthCallbackView 带来的邮箱不一致 query → 弹窗询问
  if (route.query.email_mismatch === '1') {
    syncGithubEmail.value = (route.query.github_email as string) || ''
    syncCurrentEmail.value = (route.query.current_email as string) || ''
    syncError.value = ''
    syncDialogOpen.value = true
    // 清除 query,避免刷新重复弹窗
    router.replace({ path: '/settings' })
  }
})

/** 并行加载 agent 类型与已配置列表(表格行状态依赖) */
async function loadAgentTypesAndConfigs(): Promise<void> {
  agentTypesLoading.value = true
  try {
    const [types, configs] = await Promise.all([
      getAgentTypes(),
      getAgentConfigs().catch(() => null), // 未登录或失败时静默,列表为空
    ])
    agentTypes.value = types
    if (configs) agentConfigs.value = configs.configs
  } catch (err) {
    console.warn('加载 agent 类型失败:', err)
  } finally {
    agentTypesLoading.value = false
  }
}
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
        <RouterLink to="/tasks/new">提交任务</RouterLink>
        <RouterLink to="/models">模型设置</RouterLink>
      </template>
    </AppHeader>

    <div class="page-body">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <main class="main">
        <!-- 页头 -->
        <div class="page-header">
          <div>
            <h1>设置</h1>
          </div>
        </div>

        <!-- ============ 统一表格 ============ -->
        <section class="table-section">
          <div class="table-wrap">
            <table class="config-table">
              <thead>
                <tr>
                  <th class="col-item">项目</th>
                  <th class="col-desc">说明</th>
                  <th class="col-status">状态</th>
                  <th class="col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in rows"
                  :key="row.key"
                  :class="['data-row', { 'row-danger': row.statusType === 'danger' }]"
                  @click="openRow(row)"
                >
                  <td class="col-item">
                    <span :class="['cell-title', { 'text-danger': row.statusType === 'danger' }]">{{ row.item }}</span>
                  </td>
                  <td class="col-desc">
                    <span class="cell-desc">{{ row.desc }}</span>
                  </td>
                  <td class="col-status">
                    <span v-if="row.loading" class="status-spinner" aria-label="加载中" />
                    <span v-else :class="['badge', `badge-${row.statusType}`]">{{ row.status }}</span>
                  </td>
                  <td class="col-actions" @click.stop>
                    <button
                      class="btn-link"
                      :class="{ 'link-danger': row.statusType === 'danger' }"
                      :disabled="row.loading"
                      @click="openRow(row)"
                    >{{ row.actionText }}</button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <!-- ============ 弹窗 ============ -->
        <PasswordDialog
          :open="pwdDialogOpen"
          :has-password="hasPassword"
          :loading="pwdLoading"
          :error="pwdError"
          @confirm="handlePasswordConfirm"
          @cancel="pwdDialogOpen = false"
        />

        <GitHubDialog
          :open="ghDialogOpen"
          :status="githubStatus"
          :loading="githubLoading"
          :action="githubAction"
          :error="githubError"
          :success="githubSuccess"
          @bind="handleBind"
          @unbind="handleUnbind"
          @cancel="ghDialogOpen = false"
        />

        <AgentConfigDialog
          :open="agentDialogOpen"
          :meta="agentDialogMeta"
          :detail="agentDialogDetail"
          :saving="agentSaving || agentDialogLoading"
          :error="agentError"
          :testing="agentTesting"
          :test-result="agentTestResult"
          @save="handleAgentSave"
          @clear="handleAgentClear"
          @test="handleAgentTest"
          @cancel="agentDialogOpen = false"
        />

        <DeleteAccountDialog
          :open="deleteDialogOpen"
          :current-email="currentEmail"
          :loading="deleteLoading"
          :error="deleteError"
          @confirm="handleDeleteConfirm"
          @cancel="deleteDialogOpen = false"
        />

        <!-- ============ 邮箱同步确认弹窗 ============ -->
        <Teleport to="body">
          <Transition name="dialog-fade">
            <div v-if="syncDialogOpen" class="dialog-mask" @click.self="syncDialogOpen = false">
              <div class="dialog-card" role="dialog" aria-modal="true">
                <header class="dialog-header">
                  <h3>邮箱不一致</h3>
                  <button
                    class="dialog-close"
                    :disabled="syncLoading"
                    aria-label="关闭"
                    @click="syncDialogOpen = false"
                  >×</button>
                </header>

                <div class="dialog-body">
                  <p class="sync-tip">
                    检测到 GitHub 邮箱与当前账号邮箱不一致,是否将账号邮箱更新为 GitHub 邮箱?
                  </p>
                  <div class="email-compare">
                    <div class="email-row">
                      <span class="email-label">当前账号</span>
                      <span class="email-value">{{ syncCurrentEmail || '—' }}</span>
                    </div>
                    <div class="email-row">
                      <span class="email-label">GitHub</span>
                      <span class="email-value">{{ syncGithubEmail || '—' }}</span>
                    </div>
                  </div>
                  <p class="sync-note">
                    更新后此邮箱将成为登录邮箱;GitHub verified primary email 视为已验证。
                  </p>
                </div>

                <footer class="dialog-footer">
                  <span v-if="syncError" class="validation-error">{{ syncError }}</span>
                  <span v-else></span>
                  <div class="footer-actions">
                    <button
                      class="btn btn-secondary"
                      :disabled="syncLoading"
                      @click="syncDialogOpen = false"
                    >保持原邮箱</button>
                    <button
                      class="btn btn-primary"
                      :disabled="syncLoading"
                      @click="handleSyncConfirm"
                    >
                      <span v-if="syncLoading" class="btn-spinner" />
                      {{ syncLoading ? '同步中...' : '更新为 GitHub 邮箱' }}
                    </button>
                  </div>
                </footer>
              </div>
            </div>
          </Transition>
        </Teleport>
      </main>
    </div>

    <!-- ============ 浮动提示弹窗(Teleport 到 body,顶部居中,5s 自动消失) ============ -->
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

/* ---- 浮动提示弹窗(顶部居中,5s 自动消失) ---- */
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

/* ---- 表格区(复用 ModelSettingsView 风格) ---- */
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

.col-item { width: 120px; }
.col-desc { }
.col-status { width: 120px; text-align: center; }
.config-table thead th.col-status { text-align: center; }
.config-table tbody td.col-status { text-align: center; }
.col-actions { width: 96px; text-align: center; }
.config-table thead th.col-actions { text-align: center; }
.config-table tbody td.col-actions { text-align: center; }

.config-table tbody td {
  padding: var(--space-4);
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

/* 危险行(删除账号):hover 用危险色浅底 */
.data-row.row-danger:hover {
  background: var(--color-danger-light);
}

/* ---- 单元格内容 ---- */
.cell-title {
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  display: block;
}

.text-danger {
  color: var(--color-danger);
}

.cell-desc {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

/* ---- 徽标 ---- */
.badge {
  font-size: var(--fs-xs);
  padding: 3px 8px;
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

.badge-neutral {
  background: var(--color-surface-alt);
  color: var(--color-text-muted);
}

.badge-danger {
  background: var(--color-danger-light);
  color: var(--color-danger);
}

/* 状态列加载动画 */
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

/* ---- 操作文字按钮 ---- */
.btn-link {
  background: none;
  border: none;
  padding: 4px 8px;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: all var(--transition-fast);
}

.btn-link:hover {
  background: var(--color-primary-light);
  color: var(--color-primary-hover);
}

.btn-link.link-danger {
  color: var(--color-danger);
}

.btn-link.link-danger:hover {
  background: var(--color-danger-light);
  color: var(--color-danger);
}

/* ============================================================ */
/* 邮箱同步确认弹窗(复用 dialog 视觉语言)                       */
/* ============================================================ */
.dialog-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: var(--space-4);
}

.dialog-card {
  background: var(--color-surface);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl);
  width: 100%;
  max-width: 460px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.dialog-header h3 {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  margin: 0;
  color: var(--color-text);
}

.dialog-close {
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

.dialog-close:hover:not(:disabled) {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.dialog-close:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.dialog-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-5);
}

.sync-tip {
  font-size: var(--fs-sm);
  color: var(--color-text);
  margin: 0 0 var(--space-4);
}

.email-compare {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  margin-bottom: var(--space-3);
}

.email-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  font-size: var(--fs-sm);
}

.email-label {
  flex-shrink: 0;
  width: 64px;
  color: var(--color-text-secondary);
  font-size: var(--fs-xs);
}

.email-value {
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  color: var(--color-text);
  word-break: break-all;
}

.sync-note {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  margin: 0;
  line-height: var(--lh-relaxed);
}

.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-5);
  border-top: 1px solid var(--color-border);
}

.validation-error {
  font-size: var(--fs-sm);
  color: var(--color-danger);
  flex: 1;
}

.footer-actions {
  display: flex;
  gap: var(--space-2);
}

.btn {
  height: 38px;
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

.dialog-fade-enter-active,
.dialog-fade-leave-active {
  transition: opacity 0.2s ease;
}

.dialog-fade-enter-from,
.dialog-fade-leave-to {
  opacity: 0;
}
</style>
