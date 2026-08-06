<script setup lang="ts">
/**
 * 记忆管理页(三段卡片 + 弹窗)
 *
 * 用户可编辑三类记忆:
 * - 用户偏好(1:1):结构化字段 + 自由文本,影响 user_agent 评判标准与 checklist 生成
 * - 全局长期记忆(1:1):跨项目通用经验,注入 user_agent
 * - 分项目记忆(1:N):按 repo_url 聚合,注入 react_agent;任务完成时自动归纳
 *
 * 入口:header 齿轮图标下拉 → /memory(账号类入口只由齿轮进入,不入主导航)。
 *
 * 与 SettingsView 风格一致:卡片 + 弹窗 + 顶部居中 toast。
 */
import { computed, onMounted, reactive, ref } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import ProjectMemoryDialog from '@/components/ProjectMemoryDialog.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import {
  deleteProject,
  getGlobalMemory,
  getPreferences,
  listProjects,
  saveGlobalMemory,
  savePreferences,
  saveProject,
} from '@/api/memory'
import { extractErrorMessage } from '@/utils/error'
import type {
  ProjectOut,
  SaveProjectRequest,
  UserPreferenceOut,
} from '@/types/memory'

/** 历史任务侧栏是否折叠(默认折叠) */
const workspaceCollapsed = ref(true)

function toggleWorkspace(): void {
  workspaceCollapsed.value = !workspaceCollapsed.value
}

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
// 用户偏好(1:1)
// ============================================================
/** 结构化偏好草稿(与后端 preferences JSONB 字段约定一致) */
const prefDraft = reactive({
  output_language: 'auto' as 'zh' | 'en' | 'auto',
  focus_areas_text: '', // 逗号分隔文本,保存时转数组
  style: 'concise' as 'concise' | 'strict' | 'detailed',
})
const prefCustomPrompt = ref('')
const prefLoading = ref(true)
const prefSaving = ref(false)
const prefError = ref('')

const MAX_CUSTOM_PROMPT = 8000
const prefOverLimit = computed(() => prefCustomPrompt.value.length > MAX_CUSTOM_PROMPT)

/** 语言/风格选项 */
const LANG_OPTIONS = [
  { value: 'auto', label: '跟随输入' },
  { value: 'zh', label: '中文' },
  { value: 'en', label: 'English' },
]
const STYLE_OPTIONS = [
  { value: 'concise', label: '简洁' },
  { value: 'strict', label: '严格' },
  { value: 'detailed', label: '详细' },
]

async function loadPreferences(): Promise<void> {
  prefLoading.value = true
  try {
    const data: UserPreferenceOut = await getPreferences()
    hydratePrefDraft(data)
  } catch (err) {
    // 未配置返回空默认值,理论不会失败;失败时静默,草稿保持默认
    console.warn('加载用户偏好失败:', err)
  } finally {
    prefLoading.value = false
  }
}

function hydratePrefDraft(data: UserPreferenceOut): void {
  const p = data.preferences || {}
  const lang = p.output_language
  prefDraft.output_language =
    lang === 'zh' || lang === 'en' ? lang : 'auto'
  const style = p.style
  prefDraft.style =
    style === 'concise' || style === 'strict' || style === 'detailed'
      ? style
      : 'concise'
  const areas = p.focus_areas
  prefDraft.focus_areas_text = Array.isArray(areas)
    ? areas.map(String).join(', ')
    : ''
  prefCustomPrompt.value = data.custom_prompt || ''
}

async function handleSavePreferences(): Promise<void> {
  if (prefOverLimit.value) return
  prefError.value = ''
  prefSaving.value = true
  try {
    const focusAreas = prefDraft.focus_areas_text
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
    const data = await savePreferences({
      preferences: {
        output_language: prefDraft.output_language,
        focus_areas: focusAreas,
        style: prefDraft.style,
      },
      custom_prompt: prefCustomPrompt.value,
    })
    hydratePrefDraft(data)
    showToast('用户偏好已保存', 'success')
  } catch (err) {
    prefError.value = extractErrorMessage(err)
  } finally {
    prefSaving.value = false
  }
}

// ============================================================
// 全局长期记忆(1:1)
// ============================================================
const globalContent = ref('')
const globalLoading = ref(true)
const globalSaving = ref(false)
const globalError = ref('')

const MAX_GLOBAL = 20000
const globalOverLimit = computed(() => globalContent.value.length > MAX_GLOBAL)

async function loadGlobalMemory(): Promise<void> {
  globalLoading.value = true
  try {
    const data = await getGlobalMemory()
    globalContent.value = data.content || ''
  } catch (err) {
    console.warn('加载全局长期记忆失败:', err)
  } finally {
    globalLoading.value = false
  }
}

async function handleSaveGlobal(): Promise<void> {
  if (globalOverLimit.value) return
  globalError.value = ''
  globalSaving.value = true
  try {
    const data = await saveGlobalMemory({ content: globalContent.value })
    globalContent.value = data.content || ''
    showToast('全局长期记忆已保存', 'success')
  } catch (err) {
    globalError.value = extractErrorMessage(err)
  } finally {
    globalSaving.value = false
  }
}

// ============================================================
// 分项目记忆(1:N)
// ============================================================
const projects = ref<ProjectOut[]>([])
const projectsLoading = ref(true)
const projectsError = ref('')

/** 当前打开弹窗的项目 id(空串=关闭) */
const activeProjectId = ref<string>('')
/** 弹窗内操作的 loading / deleting / error(按项目 id 隔离) */
const projectDialog = reactive({
  loading: false,
  deleting: false,
  error: '',
})

const MAX_PROJECT_PREVIEW = 120

async function loadProjects(): Promise<void> {
  projectsLoading.value = true
  projectsError.value = ''
  try {
    const data = await listProjects()
    projects.value = data.projects || []
  } catch (err) {
    projectsError.value = extractErrorMessage(err)
  } finally {
    projectsLoading.value = false
  }
}

const activeProject = computed<ProjectOut | null>(
  () => projects.value.find((p) => p.id === activeProjectId.value) ?? null,
)

function openProjectDialog(p: ProjectOut): void {
  activeProjectId.value = p.id
  projectDialog.loading = false
  projectDialog.deleting = false
  projectDialog.error = ''
}

function closeProjectDialog(): void {
  if (projectDialog.loading || projectDialog.deleting) return
  activeProjectId.value = ''
}

async function handleSaveProject(payload: SaveProjectRequest): Promise<void> {
  const id = activeProjectId.value
  if (!id) return
  projectDialog.loading = true
  projectDialog.error = ''
  try {
    const updated = await saveProject(id, payload)
    // 就地替换列表项
    const idx = projects.value.findIndex((p) => p.id === id)
    if (idx >= 0) projects.value[idx] = updated
    activeProjectId.value = ''
    showToast('项目记忆已保存', 'success')
  } catch (err) {
    projectDialog.error = extractErrorMessage(err)
  } finally {
    projectDialog.loading = false
  }
}

async function handleDeleteProject(): Promise<void> {
  const id = activeProjectId.value
  if (!id) return
  // 二次确认
  if (!window.confirm('确定删除此项目记忆?该仓库的记忆将被清空,无法恢复。')) return
  projectDialog.deleting = true
  projectDialog.error = ''
  try {
    const data = await deleteProject(id)
    projects.value = data.projects || []
    activeProjectId.value = ''
    showToast('项目记忆已删除', 'success')
  } catch (err) {
    projectDialog.error = extractErrorMessage(err)
  } finally {
    projectDialog.deleting = false
  }
}

function previewContent(content: string): string {
  const c = content || ''
  if (c.length <= MAX_PROJECT_PREVIEW) return c
  return c.slice(0, MAX_PROJECT_PREVIEW) + '…'
}

function formatTime(iso: string | null): string {
  if (!iso) return '从未归纳'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

function projectTitle(p: ProjectOut): string {
  return p.alias || p.repo_url_raw
}

// ============================================================
// 加载
// ============================================================
onMounted(() => {
  // 三段并行加载,互不阻塞
  loadPreferences()
  loadGlobalMemory()
  loadProjects()
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
        <RouterLink to="/cli">CLI 设置</RouterLink>
      </template>
    </AppHeader>

    <div class="page-body">
      <WorkspaceSidebar v-if="!workspaceCollapsed" />

      <main class="main">
        <!-- 页头 -->
        <div class="page-header">
          <div>
            <h1>记忆管理</h1>
            <p class="page-subtitle">
              用户偏好与全局记忆影响评估代理(user_agent);分项目记忆影响执行代理(react_agent)的审计方向。
              分项目记忆在任务完成时也会自动归纳。
            </p>
          </div>
        </div>

        <!-- ============ 用户偏好 ============ -->
        <section class="card">
          <header class="card-header">
            <div>
              <h2>用户偏好</h2>
              <p class="card-desc">影响 user_agent 的评判标准与 checklist 生成(注入 system prompt)</p>
            </div>
            <span v-if="prefLoading" class="status-spinner" aria-label="加载中" />
          </header>

          <div class="card-body">
            <div class="grid-2">
              <div class="field">
                <label for="pref-lang">输出语言</label>
                <select
                  id="pref-lang"
                  v-model="prefDraft.output_language"
                  :disabled="prefLoading || prefSaving"
                >
                  <option v-for="o in LANG_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
                </select>
              </div>

              <div class="field">
                <label for="pref-style">评判风格</label>
                <select
                  id="pref-style"
                  v-model="prefDraft.style"
                  :disabled="prefLoading || prefSaving"
                >
                  <option v-for="o in STYLE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
                </select>
              </div>
            </div>

            <div class="field">
              <label for="pref-areas">重点关注领域</label>
              <input
                id="pref-areas"
                v-model="prefDraft.focus_areas_text"
                type="text"
                placeholder="逗号分隔,如:security, performance, 可读性"
                :disabled="prefLoading || prefSaving"
              />
              <span class="field-hint">用逗号分隔多个领域,会引导评估代理重点关注这些方面</span>
            </div>

            <div class="field">
              <label for="pref-prompt">
                自定义补充偏好
                <span class="char-count" :class="{ over: prefOverLimit }">
                  {{ prefCustomPrompt.length }} / {{ MAX_CUSTOM_PROMPT }}
                </span>
              </label>
              <textarea
                id="pref-prompt"
                v-model="prefCustomPrompt"
                rows="5"
                placeholder="大段自定义偏好/评判标准补充(可选)。会作为兜底注入 user_agent。"
                :class="{ invalid: prefOverLimit }"
                :disabled="prefLoading || prefSaving"
              />
              <span v-if="prefOverLimit" class="field-error">不能超过 {{ MAX_CUSTOM_PROMPT }} 字符</span>
            </div>

            <div class="card-footer">
              <span v-if="prefError" class="validation-error">{{ prefError }}</span>
              <span v-else></span>
              <button
                class="btn btn-primary"
                :disabled="prefLoading || prefSaving || prefOverLimit"
                @click="handleSavePreferences"
              >
                <span v-if="prefSaving" class="btn-spinner" />
                {{ prefSaving ? '保存中...' : '保存偏好' }}
              </button>
            </div>
          </div>
        </section>

        <!-- ============ 全局长期记忆 ============ -->
        <section class="card">
          <header class="card-header">
            <div>
              <h2>全局长期记忆</h2>
              <p class="card-desc">跨项目通用经验/约定,注入 user_agent(注入时截断 2000 字符)</p>
            </div>
            <span v-if="globalLoading" class="status-spinner" aria-label="加载中" />
          </header>

          <div class="card-body">
            <div class="field">
              <label for="global-mem">
                记忆内容
                <span class="char-count" :class="{ over: globalOverLimit }">
                  {{ globalContent.length }} / {{ MAX_GLOBAL }}
                </span>
              </label>
              <textarea
                id="global-mem"
                v-model="globalContent"
                rows="10"
                placeholder="记录跨任务通用的经验、约定、偏好。任务完成时也会自动归纳合并新发现。"
                :class="{ invalid: globalOverLimit }"
                :disabled="globalLoading || globalSaving"
              />
              <span v-if="globalOverLimit" class="field-error">不能超过 {{ MAX_GLOBAL }} 字符</span>
            </div>

            <div class="card-footer">
              <span v-if="globalError" class="validation-error">{{ globalError }}</span>
              <span v-else></span>
              <button
                class="btn btn-primary"
                :disabled="globalLoading || globalSaving || globalOverLimit"
                @click="handleSaveGlobal"
              >
                <span v-if="globalSaving" class="btn-spinner" />
                {{ globalSaving ? '保存中...' : '保存全局记忆' }}
              </button>
            </div>
          </div>
        </section>

        <!-- ============ 分项目记忆 ============ -->
        <section class="card">
          <header class="card-header">
            <div>
              <h2>分项目记忆</h2>
              <p class="card-desc">
                按 Git 仓库聚合,注入 react_agent 影响审计方向。任务完成后会自动归纳创建/更新。
              </p>
            </div>
            <button
              v-if="!projectsLoading && projects.length > 0"
              class="btn-link"
              @click="loadProjects"
            >刷新</button>
          </header>

          <div class="card-body">
            <!-- 加载中 -->
            <div v-if="projectsLoading" class="placeholder">
              <span class="status-spinner" aria-label="加载中" /> 加载中...
            </div>

            <!-- 加载失败 -->
            <div v-else-if="projectsError" class="placeholder error-text">
              加载失败: {{ projectsError }}
              <button class="btn-link" @click="loadProjects">重试</button>
            </div>

            <!-- 空状态 -->
            <div v-else-if="projects.length === 0" class="empty-projects">
              <p class="empty-title">暂无项目记忆</p>
              <p class="empty-desc">
                对某仓库跑一次代码审计任务,完成后会自动归纳生成项目记忆;
                之后可在此手动编辑补充。
              </p>
            </div>

            <!-- 项目列表 -->
            <ul v-else class="project-list">
              <li
                v-for="p in projects"
                :key="p.id"
                class="project-row"
                @click="openProjectDialog(p)"
              >
                <div class="project-main">
                  <div class="project-title-row">
                    <span class="project-title">{{ projectTitle(p) }}</span>
                    <span v-if="p.alias" class="project-url mono" :title="p.repo_url_raw">{{ p.repo_url_raw }}</span>
                  </div>
                  <p v-if="p.memory_content" class="project-preview">{{ previewContent(p.memory_content) }}</p>
                  <p v-else class="project-preview muted">(暂无记忆内容)</p>
                  <div class="project-meta">
                    <span class="meta-item">上次归纳: {{ formatTime(p.last_summary_at) }}</span>
                    <span class="meta-item">更新: {{ formatTime(p.updated_at) }}</span>
                  </div>
                </div>
                <button class="btn-link" @click.stop="openProjectDialog(p)">编辑</button>
              </li>
            </ul>
          </div>
        </section>
      </main>
    </div>

    <!-- ============ 项目记忆编辑弹窗 ============ -->
    <ProjectMemoryDialog
      :open="activeProjectId !== ''"
      :project="activeProject"
      :loading="projectDialog.loading"
      :deleting="projectDialog.deleting"
      :error="projectDialog.error"
      @save="handleSaveProject"
      @delete="handleDeleteProject"
      @cancel="closeProjectDialog"
    />

    <!-- ============ 浮动提示弹窗 ============ -->
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
  max-width: 820px;
  margin: 0 auto;
  overflow-y: auto;
  padding: var(--space-6) var(--space-5) var(--space-8);
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

/* ---- 页头 ---- */
.page-header {
  margin-bottom: var(--space-1);
}

.page-header h1 {
  font-size: var(--fs-xl);
  margin: 0 0 var(--space-2);
}

.page-subtitle {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  margin: 0;
  line-height: var(--lh-relaxed);
}

/* ---- 卡片 ---- */
.card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-sm);
  overflow: hidden;
}

.card-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-border);
}

.card-header h2 {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  margin: 0 0 var(--space-1);
}

.card-desc {
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  margin: 0;
  line-height: var(--lh-relaxed);
}

.card-body {
  padding: var(--space-5);
}

/* ---- 表单字段 ---- */
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-4);
  margin-bottom: var(--space-4);
}

@media (max-width: 560px) {
  .grid-2 {
    grid-template-columns: 1fr;
  }
}

.field {
  margin-bottom: var(--space-4);
}

.field:last-child {
  margin-bottom: 0;
}

.field label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  margin-bottom: var(--space-2);
  color: var(--color-text);
}

.char-count {
  font-size: var(--fs-xs);
  font-weight: var(--fw-regular);
  color: var(--color-text-muted);
}

.char-count.over {
  color: var(--color-danger);
  font-weight: var(--fw-medium);
}

.field input,
.field select,
.field textarea {
  width: 100%;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-base);
  font-family: inherit;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.field textarea {
  resize: vertical;
  line-height: var(--lh-relaxed);
  min-height: 80px;
}

.field input:focus,
.field select:focus,
.field textarea:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.field input:disabled,
.field select:disabled,
.field textarea:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.field input.invalid,
.field textarea.invalid {
  border-color: var(--color-danger);
}

.field-hint {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.field-error {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--fs-xs);
  color: var(--color-danger);
}

/* ---- 卡片底部 ---- */
.card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.validation-error {
  font-size: var(--fs-sm);
  color: var(--color-danger);
  flex: 1;
  word-break: break-word;
}

/* ---- 按钮 ---- */
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
  flex-shrink: 0;
}

.btn-link:hover {
  background: var(--color-primary-light);
  color: var(--color-primary-hover);
}

/* ---- 占位/空状态 ---- */
.placeholder {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
  padding: var(--space-4) 0;
}

.error-text {
  color: var(--color-danger);
}

.empty-projects {
  padding: var(--space-6) var(--space-4);
  text-align: center;
}

.empty-title {
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  margin: 0 0 var(--space-2);
}

.empty-desc {
  font-size: var(--fs-sm);
  color: var(--color-text-muted);
  margin: 0;
  line-height: var(--lh-relaxed);
}

/* ---- 项目列表 ---- */
.project-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.project-row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background var(--transition-fast), border-color var(--transition-fast);
}

.project-row:hover {
  background: var(--color-surface-alt);
  border-color: var(--color-border-strong);
}

.project-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.project-title-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  min-width: 0;
}

.project-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  word-break: break-all;
}

.project-url {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.project-preview {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  margin: 0;
  line-height: var(--lh-relaxed);
  white-space: pre-wrap;
  word-break: break-word;
}

.project-preview.muted {
  color: var(--color-text-muted);
  font-style: italic;
}

.project-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  margin-top: var(--space-1);
}

.meta-item {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.mono {
  font-family: var(--font-mono);
}

/* ---- spinner ---- */
.status-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: status-spin 0.8s linear infinite;
  flex-shrink: 0;
}

@keyframes status-spin {
  to { transform: rotate(360deg); }
}

/* ---- 浮动提示弹窗 ---- */
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
