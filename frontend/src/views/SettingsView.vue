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
import { useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import GitHubDialog from '@/components/GitHubDialog.vue'
import PasswordDialog from '@/components/PasswordDialog.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import { changePassword } from '@/api/auth'
import { getGitHubBindURL, getGitHubStatus, unbindGitHub } from '@/api/github'
import type { GitHubStatus } from '@/types/github'
import { useAuthStore } from '@/stores/auth'
import { extractErrorMessage } from '@/utils/error'

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
// 表格行
// ============================================================
interface SettingRow {
  key: 'password' | 'github'
  item: string
  desc: string
  status: string
  statusType: 'ok' | 'warn' | 'neutral'
}

const rows = computed<SettingRow[]>(() => [
  {
    key: 'password',
    item: '登录密码',
    desc: hasPassword.value ? '修改账号登录密码' : 'OAuth 账号设置密码',
    status: hasPassword.value ? '已设置' : '未设置',
    statusType: hasPassword.value ? 'ok' : 'warn',
  },
  {
    key: 'github',
    item: 'GitHub 账号',
    desc: '绑定后可访问私有仓库',
    status: githubStatus.value?.bound
      ? `@${githubStatus.value.github_login || 'unknown'}`
      : '未绑定',
    statusType: githubStatus.value?.bound ? 'ok' : 'warn',
  },
])

function openRow(row: SettingRow): void {
  if (row.key === 'password') openPasswordDialog()
  else openGitHubDialog()
}

onMounted(() => {
  // 预加载 GitHub 状态(用于表格状态列展示)
  refreshGitHubStatus()
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
                  class="data-row"
                  @click="openRow(row)"
                >
                  <td class="col-item">
                    <span class="cell-title">{{ row.item }}</span>
                  </td>
                  <td class="col-desc">
                    <span class="cell-desc">{{ row.desc }}</span>
                  </td>
                  <td class="col-status">
                    <span :class="['badge', `badge-${row.statusType}`]">{{ row.status }}</span>
                  </td>
                  <td class="col-actions" @click.stop>
                    <button class="btn-icon" :title="row.key === 'password' ? '修改密码' : '管理 GitHub'" @click="openRow(row)">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                        <path d="M12 20h9" />
                        <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
                      </svg>
                    </button>
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
.col-status { width: 120px; }
.col-actions { width: 72px; text-align: right; }

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

/* ---- 单元格内容 ---- */
.cell-title {
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  display: block;
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

/* ---- 操作按钮 ---- */
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
  color: var(--color-text-secondary);
  transition: all var(--transition-fast);
}

.btn-icon:hover {
  background: var(--color-surface);
  color: var(--color-text);
}
</style>
