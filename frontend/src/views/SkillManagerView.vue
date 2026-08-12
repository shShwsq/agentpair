<script setup lang="ts">
/**
 * 技能管理页(上传 zip / 列表 / 详情 / 删除)
 *
 * 入口:主导航「技能管理」项(与记忆管理/协作策略并列)。
 *
 * 能力:
 * - 上传 zip 格式 skill(支持 <skill_name>/SKILL.md 或根目录 SKILL.md),
 *   上传后立即生效(后端热刷新注册表),react_agent 的 list_skills/skill 工具即可使用
 * - 列表区分「系统内置」(全局共享,只读)与「我的」(仅自己可见,可删)
 * - 查看详情(body 指令预览)、删除自己的 skill
 * - 同名冲突:与自己的 skill 重名时勾选「覆盖同名技能」重传即可
 *
 * 安全:zip 由后端校验(zip-slip / 大小 / frontmatter / 扩展名白名单)。
 */
import { onMounted, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import {
  deleteSkill,
  getSkillDetail,
  getSkills,
  uploadSkill,
  type SkillDetail,
  type SkillSummary,
} from '@/api/skill'
import { extractErrorMessage } from '@/utils/error'

/** 历史任务侧栏是否折叠 */
const workspaceCollapsed = ref(true)
function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

// ============================================================
// Toast
// ============================================================
const toast = ref<{ msg: string; type: 'success' | 'error' } | null>(null)
function showToast(msg: string, type: 'success' | 'error'): void {
  toast.value = { msg, type }
  setTimeout(() => {
    toast.value = null
  }, 4000)
}

// ============================================================
// 技能列表
// ============================================================
const skills = ref<SkillSummary[]>([])
const loading = ref(true)
const loadError = ref('')

async function loadSkills(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    skills.value = await getSkills()
  } catch (e) {
    loadError.value = extractErrorMessage(e)
  } finally {
    loading.value = false
  }
}

/** 是否为内置 skill(场景目录非 user_ 前缀) */
function isBuiltin(s: SkillSummary): boolean {
  return !s.scenario_id.startsWith('user_')
}

// ============================================================
// 上传
// ============================================================
const selectedFile = ref<File | null>(null)
/** 同名冲突时强制覆盖(需后端 409 提示后勾选) */
const forceOverwrite = ref(false)
const uploading = ref(false)

function onPickFile(e: Event): void {
  const input = e.target as HTMLInputElement
  selectedFile.value = input.files?.[0] ?? null
  // 清空 input,允许重复选择同一文件
  input.value = ''
}

async function handleUpload(): Promise<void> {
  if (!selectedFile.value) {
    showToast('请先选择 .zip 文件', 'error')
    return
  }
  uploading.value = true
  try {
    const result = await uploadSkill(selectedFile.value, forceOverwrite.value)
    showToast(
      result.replaced
        ? `已覆盖技能「${result.skill.name}」`
        : `已上传技能「${result.skill.name}」,react_agent 立即可用`,
      'success',
    )
    selectedFile.value = null
    forceOverwrite.value = false
    await loadSkills()
  } catch (e) {
    const msg = extractErrorMessage(e)
    // 409 同名冲突:提示勾选覆盖后重试
    if (msg.includes('已存在') || msg.includes('冲突')) {
      forceOverwrite.value = true
      showToast(`${msg}——已自动勾选「覆盖同名技能」,再次点击上传即可`, 'error')
    } else {
      showToast(msg, 'error')
    }
  } finally {
    uploading.value = false
  }
}

// ============================================================
// 详情弹窗
// ============================================================
const detailOpen = ref(false)
const detail = ref<SkillDetail | null>(null)
const detailLoading = ref(false)

async function openDetail(s: SkillSummary): Promise<void> {
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await getSkillDetail(s.scenario_id, s.name)
  } catch (e) {
    showToast(extractErrorMessage(e), 'error')
    detailOpen.value = false
  } finally {
    detailLoading.value = false
  }
}

// ============================================================
// 删除
// ============================================================
async function handleDelete(s: SkillSummary): Promise<void> {
  if (!window.confirm(`确定删除技能「${s.name}」吗?删除后不可恢复。`)) return
  try {
    await deleteSkill(s.scenario_id, s.name)
    showToast(`已删除技能「${s.name}」`, 'success')
    await loadSkills()
  } catch (e) {
    showToast(extractErrorMessage(e), 'error')
  }
}

onMounted(loadSkills)
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

      <main class="skill-main">
        <!-- ============ 上传区 ============ -->
        <section class="upload-card">
          <div class="upload-header">
            <h2 class="section-title">上传技能</h2>
            <p class="section-desc">
              上传 zip 格式的 skill(内含 SKILL.md,支持
              <code>&lt;skill_name&gt;/SKILL.md</code> 或根目录直接放
              <code>SKILL.md</code>),上传后 react_agent 的 list_skills / skill
              工具立即可用。技能仅自己可见,他人无法查看或使用。
            </p>
          </div>
          <div class="upload-row">
            <input
              type="file"
              accept=".zip,application/zip"
              class="file-input"
              @change="onPickFile"
            />
            <span class="file-name" :title="selectedFile?.name ?? ''">
              {{ selectedFile ? selectedFile.name : '未选择文件' }}
            </span>
            <label class="force-check">
              <input v-model="forceOverwrite" type="checkbox" :disabled="uploading" />
              覆盖同名技能
            </label>
            <button
              class="btn-primary"
              :disabled="uploading || !selectedFile"
              @click="handleUpload"
            >
              {{ uploading ? '上传中...' : '上传' }}
            </button>
          </div>
        </section>

        <!-- ============ 技能列表 ============ -->
        <section class="list-card">
          <div class="list-header">
            <h2 class="section-title">技能列表</h2>
            <span class="list-count">{{ skills.length }} 个</span>
          </div>

          <div v-if="loading" class="list-placeholder">
            <span class="status-spinner" /> 加载中...
          </div>
          <div v-else-if="loadError" class="list-placeholder error-text">
            加载失败: {{ loadError }}
            <button class="btn-link" @click="loadSkills">重试</button>
          </div>
          <div v-else-if="skills.length === 0" class="list-placeholder">
            暂无技能,上传一个 zip 开始
          </div>

          <ul v-else class="skill-list">
            <li v-for="s in skills" :key="`${s.scenario_id}/${s.name}`" class="skill-item">
              <button class="skill-main-btn" @click="openDetail(s)">
                <span class="skill-name">{{ s.name }}</span>
                <span class="skill-desc">{{ s.description }}</span>
              </button>
              <span class="skill-tag" :class="isBuiltin(s) ? 'tag-builtin' : 'tag-mine'">
                {{ isBuiltin(s) ? '系统内置' : '我的' }}
              </span>
              <button
                v-if="s.owned"
                class="btn-delete"
                title="删除技能"
                aria-label="删除技能"
                @click="handleDelete(s)"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6h14" />
                </svg>
              </button>
            </li>
          </ul>
        </section>
      </main>
    </div>

    <!-- ============ 详情弹窗 ============ -->
    <div v-if="detailOpen" class="dialog-mask" @click.self="detailOpen = false">
      <div class="dialog">
        <div class="dialog-header">
          <h3 class="dialog-title">{{ detail?.name ?? '加载中...' }}</h3>
          <button class="dialog-close" aria-label="关闭" @click="detailOpen = false">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="18" x2="6" y1="6" y2="18" /><line x1="6" x2="18" y1="6" y2="18" />
            </svg>
          </button>
        </div>
        <p v-if="detail" class="dialog-desc">{{ detail.description }}</p>
        <div v-if="detailLoading" class="dialog-loading">
          <span class="status-spinner" /> 加载中...
        </div>
        <pre v-else-if="detail" class="dialog-body">{{ detail.body }}</pre>
      </div>
    </div>

    <!-- Toast -->
    <div v-if="toast" class="toast" :class="`toast-${toast.type}`">
      {{ toast.msg }}
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

.skill-main {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: var(--space-6);
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

/* ============ 卡片 ============ */
.upload-card,
.list-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-5) var(--space-6);
}

.section-title {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
}

.section-desc {
  margin-top: var(--space-1);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.section-desc code {
  font-family: var(--font-mono, monospace);
  font-size: 0.9em;
  background: var(--color-surface-alt);
  padding: 1px 5px;
  border-radius: var(--radius-sm);
}

/* ============ 上传区 ============ */
.upload-row {
  margin-top: var(--space-4);
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.file-input {
  max-width: 260px;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.file-name {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.force-check {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  cursor: pointer;
}

.btn-primary {
  margin-left: auto;
  padding: var(--space-2) var(--space-5);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: #fff;
  background: var(--color-primary);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: opacity var(--transition-fast);
}

.btn-primary:hover:not(:disabled) {
  opacity: 0.9;
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ============ 列表 ============ */
.list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.list-count {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.list-placeholder {
  padding: var(--space-8) 0;
  text-align: center;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.error-text {
  color: var(--color-danger);
}

.btn-link {
  color: var(--color-primary);
  background: none;
  border: none;
  cursor: pointer;
  font-size: var(--fs-sm);
}

.skill-list {
  margin-top: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.skill-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface-alt);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast);
}

.skill-item:hover {
  border-color: var(--color-border);
}

.skill-main-btn {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  text-align: left;
  background: none;
  border: none;
  cursor: pointer;
  padding: 0;
}

.skill-name {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
}

.skill-desc {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}

.skill-tag {
  flex-shrink: 0;
  padding: 2px 8px;
  font-size: 12px;
  border-radius: var(--radius-full);
}

.tag-builtin {
  color: var(--color-text-secondary);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
}

.tag-mine {
  color: var(--color-primary);
  background: var(--color-primary-light, rgba(99, 102, 241, 0.12));
}

.btn-delete {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-delete:hover {
  color: var(--color-danger);
  border-color: var(--color-danger);
  background: var(--color-danger-light);
}

/* ============ 详情弹窗 ============ */
.dialog-mask {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.45);
  padding: var(--space-6);
}

.dialog {
  width: min(680px, 100%);
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.dialog-title {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
}

.dialog-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
}

.dialog-close:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.dialog-desc {
  padding: var(--space-3) var(--space-5) 0;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.dialog-loading {
  padding: var(--space-8);
  text-align: center;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.dialog-body {
  flex: 1;
  overflow-y: auto;
  margin: var(--space-4) var(--space-5) var(--space-5);
  padding: var(--space-4);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-word;
}

/* ============ Toast ============ */
.toast {
  position: fixed;
  top: var(--space-5);
  left: 50%;
  transform: translateX(-50%);
  z-index: 60;
  padding: var(--space-3) var(--space-5);
  font-size: var(--fs-sm);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-lg);
  max-width: 80vw;
}

.toast-success {
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-primary);
}

.toast-error {
  color: var(--color-danger);
  background: var(--color-danger-light);
  border: 1px solid var(--color-danger);
}

.status-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  vertical-align: -2px;
  margin-right: var(--space-2);
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
