<script setup lang="ts">
/**
 * 记忆管理页(文件列表 + Markdown 编辑器)
 *
 * 三类记忆以「文件」隐喻呈现,每份文件即一篇 Markdown:
 * - 用户偏好(1):custom_prompt Markdown(结构化 preferences JSONB 保留但不在此编辑)
 * - 全局记忆(1):content Markdown,注入 user_agent
 * - 项目记忆(N,按 repo_url 聚合):memory_content Markdown,注入 react_agent;任务完成自动归纳
 *
 * 左侧文件列表,右侧编辑器(编辑/预览切换)。
 * 预览用 marked 渲染 + DOMPurify 净化(项目记忆可能由 agent 归纳自不可信仓库内容,防存储型 XSS)。
 *
 * 入口:主导航「记忆管理」项(与模型设置/CLI 设置并列)。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import AppHeader from '@/components/AppHeader.vue'
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
import type { ProjectOut, UserPreferenceOut } from '@/types/memory'

// ============================================================
// 默认模板(后端初始为空,前端在内容为空时预填模板作为引导)
// 保存后才会真正写入 DB 并注入 agent;纯模板未改动不计为"未保存"
// ============================================================

/** 用户偏好默认模板(写入 custom_prompt,注入 user_agent) */
const PREF_TEMPLATE = `# 用户偏好

## 输出语言
中文

## 评判风格
严格,安全与最佳实践优先于简洁;对关键问题必须给出复现步骤与修复示例。

## 重点关注领域
- 安全:硬编码密钥、注入、权限提升、SSRF
- 性能与资源泄漏
- 可读性与职责划分

## 补充要求
- 
`

/** 全局长期记忆默认模板(写入 content,注入 user_agent) */
const GLOBAL_TEMPLATE = `# 全局长期记忆

## 通用约定
- 

## 常见踩坑
- 

## 复用经验
- 
`

/** 按 文件类型 取对应模板(项目记忆无模板) */
function templateFor(kind: FileKind): string {
  if (kind === 'pref') return PREF_TEMPLATE
  if (kind === 'global') return GLOBAL_TEMPLATE
  return ''
}

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
// 文件模型
// ============================================================
type FileKind = 'pref' | 'global' | 'project'

interface FileEntry {
  /** 文件 id:pref / global / project:<uuid> */
  id: string
  kind: FileKind
  /** 显示名(项目=alias 或 repo_url,其余=固定名) */
  label: string
  /** 副标题(项目=repo_url_raw) */
  subtitle?: string
  /** 元信息(项目=上次归纳时间) */
  meta?: string | null
}

/** 项目原始数据(列表) */
const projects = ref<ProjectOut[]>([])

/** 文件列表(计算) */
const fileList = computed<FileEntry[]>(() => {
  const list: FileEntry[] = [
    { id: 'pref', kind: 'pref', label: '用户偏好' },
    { id: 'global', kind: 'global', label: '全局记忆' },
  ]
  for (const p of projects.value) {
    list.push({
      id: `project:${p.id}`,
      kind: 'project',
      label: p.alias || p.repo_url_raw,
      subtitle: p.repo_url_raw,
      meta: p.last_summary_at,
    })
  }
  return list
})

/** 当前选中文件 id */
const activeId = ref<string>('pref')
const activeEntry = computed<FileEntry | undefined>(() =>
  fileList.value.find((f) => f.id === activeId.value),
)

// ============================================================
// 草稿与原始值(支持多文件切换不丢未保存编辑)
// ============================================================
/** 每份文件的 Markdown 草稿(键=文件 id) */
const drafts = reactive<Record<string, string>>({})
/** 每份文件的原始 Markdown(脏检查基准) */
const originals = reactive<Record<string, string>>({})
/** 项目 alias 草稿(键=project:<uuid>) */
const aliasDrafts = reactive<Record<string, string>>({})
/** 项目 alias 原始值 */
const originalAlias = reactive<Record<string, string>>({})
/** 该文件当前显示的是默认模板(DB 为空时预填,键=文件 id;仅 pref/global 会为 true) */
const isTemplateDefault = reactive<Record<string, boolean>>({})

// ---- 透传存储(不在 UI 编辑,但保存时原样回传,避免数据丢失) ----
/** 用户偏好的结构化 preferences JSONB(透传) */
const prefPreferencesPassthrough = ref<Record<string, unknown>>({})
/** 项目 note(透传,键=project:<uuid>) */
const projectNotes = reactive<Record<string, string | null>>({})
/** 文件 id → 项目 uuid */
const projectIds = reactive<Record<string, string>>({})

// ============================================================
// 编辑/预览模式
// ============================================================
const mode = ref<'edit' | 'preview'>('edit')

/** 预览 HTML(marked 渲染 + DOMPurify 净化) */
const previewHtml = computed(() => {
  const md = drafts[activeId.value] ?? ''
  if (!md.trim()) return ''
  // marked.parse 在 async:false 下同步返回 string
  const raw = marked.parse(md, { async: false }) as string
  return DOMPurify.sanitize(raw)
})

// ============================================================
// 加载状态
// ============================================================
const loading = ref(true)
const loadError = ref('')

// 保存/删除状态(作用于当前文件)
const saving = ref(false)
const deleting = ref(false)
const actionError = ref('')

// ============================================================
// 字符上限(与后端 schema 对齐)
// ============================================================
const LIMITS: Record<FileKind, number> = {
  pref: 8000,
  global: 20000,
  project: 20000,
}
const activeLimit = computed(() => (activeEntry.value ? LIMITS[activeEntry.value.kind] : 20000))
const activeDraft = computed(() => drafts[activeId.value] ?? '')
const activeOverLimit = computed(() => activeDraft.value.length > activeLimit.value)

/** 草稿与原始值是否有任何差异(含 alias;不区分模板,用于判定"可保存") */
function hasDiff(fileId: string): boolean {
  if ((drafts[fileId] ?? '') !== (originals[fileId] ?? '')) return true
  if (fileId.startsWith('project:') && (aliasDrafts[fileId] ?? '') !== (originalAlias[fileId] ?? '')) return true
  return false
}

/** 当前文件是否脏(有未保存改动;纯模板未改动不计脏,避免空文件初始就显示未保存,但仍可"采用"保存) */
function isDirty(fileId: string): boolean {
  if (!hasDiff(fileId)) return false
  const draft = drafts[fileId] ?? ''
  const orig = originals[fileId] ?? ''
  // 纯默认模板预填未改动:草稿===模板且 DB 原本为空 → 不算脏
  if (isTemplateDefault[fileId] && orig === '' && draft === templateFor(fileKind(fileId))) {
    return false
  }
  return true
}
/** 是否可保存:草稿与原始值有差异即可(含"采用默认模板":纯模板时 hasDiff 仍为 true) */
const activeCanSave = computed(() => hasDiff(activeId.value))

/** 当前文件是否处于"纯默认模板预填"状态(用于显示提示条) */
const activeIsTemplate = computed(() => {
  const entry = activeEntry.value
  if (!entry || entry.kind === 'project') return false
  return (
    isTemplateDefault[activeId.value] === true &&
    (drafts[activeId.value] ?? '') === templateFor(entry.kind)
  )
})

/** 按文件 id 取类型(fileList 反查) */
function fileKind(fileId: string): FileKind {
  if (fileId === 'pref') return 'pref'
  if (fileId === 'global') return 'global'
  return 'project'
}

/** 任一文件有未保存改动(用于切换/离开提示,此处仅展示) */
const hasAnyDirty = computed(() => fileList.value.some((f) => isDirty(f.id)))

// ============================================================
// 加载
// ============================================================
async function loadAll(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const [prefData, globalData, projectsData] = await Promise.all([
      getPreferences(),
      getGlobalMemory(),
      listProjects(),
    ])
    hydratePref(prefData)
    hydrateGlobal(globalData.content || '')
    hydrateProjects(projectsData.projects || [])
  } catch (err) {
    loadError.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}

function hydratePref(data: UserPreferenceOut): void {
  prefPreferencesPassthrough.value = data.preferences || {}
  const md = data.custom_prompt || ''
  if (md.trim() === '') {
    // DB 为空:预填默认模板作为引导(不算"已保存"内容,originals 仍为空)
    drafts['pref'] = PREF_TEMPLATE
    originals['pref'] = ''
    isTemplateDefault['pref'] = true
  } else {
    drafts['pref'] = md
    originals['pref'] = md
    isTemplateDefault['pref'] = false
  }
}

function hydrateGlobal(content: string): void {
  if (content.trim() === '') {
    drafts['global'] = GLOBAL_TEMPLATE
    originals['global'] = ''
    isTemplateDefault['global'] = true
  } else {
    drafts['global'] = content
    originals['global'] = content
    isTemplateDefault['global'] = false
  }
}

function hydrateProjects(list: ProjectOut[]): void {
  projects.value = list
  for (const p of list) {
    const fid = `project:${p.id}`
    drafts[fid] = p.memory_content || ''
    originals[fid] = p.memory_content || ''
    aliasDrafts[fid] = p.alias || ''
    originalAlias[fid] = p.alias || ''
    projectNotes[fid] = p.note ?? null
    projectIds[fid] = p.id
  }
}

// ============================================================
// 选择文件 / 切换模式
// ============================================================
function selectFile(id: string): void {
  activeId.value = id
  mode.value = 'edit'
  actionError.value = ''
}

// ============================================================
// 保存当前文件
// ============================================================
async function handleSave(): Promise<void> {
  const entry = activeEntry.value
  if (!entry || !activeCanSave.value || activeOverLimit.value || saving.value) return
  saving.value = true
  actionError.value = ''
  try {
    if (entry.kind === 'pref') {
      const data = await savePreferences({
        preferences: prefPreferencesPassthrough.value,
        custom_prompt: drafts['pref'],
      })
      hydratePref(data)
    } else if (entry.kind === 'global') {
      const data = await saveGlobalMemory({ content: drafts['global'] })
      hydrateGlobal(data.content || '')
    } else {
      const fid = entry.id
      const pid = projectIds[fid]
      const alias = (aliasDrafts[fid] || '').trim()
      const data = await saveProject(pid, {
        alias: alias || null,
        note: projectNotes[fid] ?? null,
        memory_content: drafts[fid],
      })
      // 就地更新项目列表 + 重水化草稿
      const idx = projects.value.findIndex((p) => p.id === pid)
      if (idx >= 0) projects.value[idx] = data
      drafts[fid] = data.memory_content || ''
      originals[fid] = data.memory_content || ''
      aliasDrafts[fid] = data.alias || ''
      originalAlias[fid] = data.alias || ''
      projectNotes[fid] = data.note ?? null
    }
    showToast('已保存', 'success')
  } catch (err) {
    actionError.value = extractErrorMessage(err)
  } finally {
    saving.value = false
  }
}

// ============================================================
// 删除当前项目文件
// ============================================================
async function handleDelete(): Promise<void> {
  const entry = activeEntry.value
  if (!entry || entry.kind !== 'project' || deleting.value) return
  const fid = entry.id
  const pid = projectIds[fid]
  if (!pid) return
  if (!window.confirm('确定删除此项目记忆?该仓库的记忆将被清空,无法恢复。')) return
  deleting.value = true
  actionError.value = ''
  try {
    const data = await deleteProject(pid)
    projects.value = data.projects || []
    // 清理草稿
    delete drafts[fid]
    delete originals[fid]
    delete aliasDrafts[fid]
    delete originalAlias[fid]
    delete projectNotes[fid]
    delete projectIds[fid]
    activeId.value = 'pref'
    mode.value = 'edit'
    showToast('项目记忆已删除', 'success')
  } catch (err) {
    actionError.value = extractErrorMessage(err)
  } finally {
    deleting.value = false
  }
}

// ============================================================
// 工具
// ============================================================
function formatTime(iso: string | null | undefined): string {
  if (!iso) return '从未归纳'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    return d.toLocaleString('zh-CN', { hour12: false })
  } catch {
    return iso
  }
}

onMounted(() => {
  loadAll()
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

      <main class="memory-main">
        <!-- ============ 左侧文件列表 ============ -->
        <aside class="file-sidebar">
          <div class="sidebar-header">
            <span>记忆文件</span>
            <button
              v-if="!loading && !loadError"
              class="btn-icon"
              title="刷新"
              @click="loadAll"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="23 4 23 10 17 10" />
                <polyline points="1 20 1 14 7 14" />
                <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
              </svg>
            </button>
          </div>

          <!-- 加载中 -->
          <div v-if="loading" class="sidebar-placeholder">
            <span class="status-spinner" /> 加载中...
          </div>
          <!-- 加载失败 -->
          <div v-else-if="loadError" class="sidebar-placeholder error-text">
            加载失败: {{ loadError }}
            <button class="btn-link" @click="loadAll">重试</button>
          </div>
          <!-- 文件列表 -->
          <ul v-else class="file-list">
            <li>
              <button
                class="file-item"
                :class="{ active: activeId === 'pref' }"
                @click="selectFile('pref')"
              >
                <svg class="file-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                  <polyline points="14 2 14 8 20 8" />
                </svg>
                <span class="file-label">用户偏好</span>
                <span v-if="isDirty('pref')" class="dirty-dot" title="有未保存改动" />
              </button>
            </li>
            <li>
              <button
                class="file-item"
                :class="{ active: activeId === 'global' }"
                @click="selectFile('global')"
              >
                <svg class="file-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                  <polyline points="14 2 14 8 20 8" />
                </svg>
                <span class="file-label">全局记忆</span>
                <span v-if="isDirty('global')" class="dirty-dot" title="有未保存改动" />
              </button>
            </li>

            <li class="group-label">项目记忆</li>
            <li v-if="projects.length === 0" class="group-empty">
              暂无项目记忆,任务完成后自动归纳生成
            </li>
            <li v-for="p in projects" :key="p.id">
              <button
                class="file-item project"
                :class="{ active: activeId === `project:${p.id}` }"
                @click="selectFile(`project:${p.id}`)"
              >
                <svg class="file-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M3 3h18v18H3z" />
                  <path d="M3 9h18M9 21V9" />
                </svg>
                <span class="file-label" :title="p.alias || p.repo_url_raw">
                  {{ p.alias || p.repo_url_raw }}
                </span>
                <span v-if="isDirty(`project:${p.id}`)" class="dirty-dot" title="有未保存改动" />
              </button>
            </li>
          </ul>

          <div v-if="hasAnyDirty" class="sidebar-footer-hint">
            <span class="dirty-dot" /> 有未保存改动
          </div>
        </aside>

        <!-- ============ 右侧编辑器 ============ -->
        <section class="editor-panel">
          <template v-if="activeEntry">
            <!-- 工具栏 -->
            <header class="editor-toolbar">
              <div class="toolbar-left">
                <!-- 项目:alias 可编辑标题 -->
                <input
                  v-if="activeEntry.kind === 'project'"
                  v-model="aliasDrafts[activeEntry.id]"
                  class="title-input"
                  type="text"
                  placeholder="项目别名(可选)"
                  :disabled="saving || deleting"
                  :maxlength="255"
                />
                <h2 v-else class="title-static">{{ activeEntry.label }}</h2>

                <div v-if="activeEntry.kind === 'project'" class="subtitle-row">
                  <span class="subtitle mono" :title="activeEntry.subtitle">{{ activeEntry.subtitle }}</span>
                  <span class="meta-sep">·</span>
                  <span class="subtitle">上次归纳: {{ formatTime(activeEntry.meta) }}</span>
                </div>
                <p v-if="actionError" class="action-error">{{ actionError }}</p>
              </div>

              <div class="toolbar-right">
                <span class="char-count" :class="{ over: activeOverLimit }">
                  {{ activeDraft.length }} / {{ activeLimit }}
                </span>
                <div class="mode-switch" role="group" aria-label="编辑/预览">
                  <button
                    :class="['mode-btn', { active: mode === 'edit' }]"
                    :disabled="saving || deleting"
                    @click="mode = 'edit'"
                  >编辑</button>
                  <button
                    :class="['mode-btn', { active: mode === 'preview' }]"
                    :disabled="saving || deleting"
                    @click="mode = 'preview'"
                  >预览</button>
                </div>
                <button
                  v-if="activeEntry.kind === 'project'"
                  class="btn btn-danger"
                  :disabled="saving || deleting"
                  @click="handleDelete"
                >
                  <span v-if="deleting" class="btn-spinner danger" />
                  {{ deleting ? '删除中...' : '删除' }}
                </button>
                <button
                  class="btn btn-primary"
                  :disabled="!activeCanSave || saving || deleting || activeOverLimit"
                  @click="handleSave"
                >
                  <span v-if="saving" class="btn-spinner" />
                  {{ saving ? '保存中...' : (activeCanSave ? '保存' : '已保存') }}
                </button>
              </div>
            </header>

            <!-- 模板提示条(当前显示默认模板、未改动时) -->
            <div v-if="activeIsTemplate" class="template-hint">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10" />
                <line x1="12" y1="16" x2="12" y2="12" />
                <line x1="12" y1="8" x2="12.01" y2="8" />
              </svg>
              <span>当前显示默认模板 — 保存后才会写入并注入 agent。可按需编辑,或直接点「保存」采用此模板。</span>
            </div>

            <!-- 内容区 -->
            <div class="editor-content">
              <!-- 加载占位 -->
              <div v-if="loading" class="content-placeholder">
                <span class="status-spinner" /> 加载中...
              </div>
              <!-- 编辑模式 -->
              <textarea
                v-else-if="mode === 'edit'"
                v-model="drafts[activeEntry.id]"
                class="md-editor"
                :class="{ invalid: activeOverLimit }"
                placeholder="用 Markdown 编写。支持标题、列表、代码块等。"
                :disabled="saving || deleting"
                spellcheck="false"
              />
              <!-- 预览模式 -->
              <div v-else class="md-preview">
                <div v-if="previewHtml" class="markdown-body" v-html="previewHtml" />
                <div v-else class="preview-empty">暂无内容</div>
              </div>
            </div>

          </template>

          <!-- 无选中文件兜底 -->
          <div v-else class="editor-empty">
            <p>请从左侧选择一份记忆文件</p>
          </div>
        </section>
      </main>
    </div>

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

.memory-main {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: stretch;
  overflow: hidden;
}

/* ============ 左侧文件列表 ============ */
.file-sidebar {
  width: 248px;
  flex-shrink: 0;
  background: var(--color-surface);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--color-text-secondary);
  letter-spacing: 0.02em;
}

.btn-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--color-text-muted);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.btn-icon:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.sidebar-placeholder {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-4);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.error-text {
  color: var(--color-danger);
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
}

.file-list {
  list-style: none;
  margin: 0;
  padding: var(--space-2);
  flex: 1;
  overflow-y: auto;
}

.file-list > li {
  margin: 0;
}

.group-label {
  padding: var(--space-3) var(--space-3) var(--space-1);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--color-text-muted);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.group-empty {
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  line-height: var(--lh-relaxed);
}

.file-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  padding: var(--space-2) var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text);
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  text-align: left;
  transition: all var(--transition-fast);
  position: relative;
}

.file-item:hover {
  background: var(--color-surface-alt);
}

.file-item.active {
  background: var(--color-primary-light);
  color: var(--color-primary-hover);
  font-weight: var(--fw-semibold);
}

.file-item.project {
  padding-left: var(--space-5);
}

.file-icon {
  color: var(--color-text-muted);
  flex-shrink: 0;
}

.file-item.active .file-icon {
  color: var(--color-primary);
}

.file-label {
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.dirty-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-warning, #f59e0b);
  flex-shrink: 0;
}

.sidebar-footer-hint {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-4);
  border-top: 1px solid var(--color-border);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

/* ============ 右侧编辑器 ============ */
.editor-panel {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-bg);
}

.editor-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-3) var(--space-5);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
}

/* 模板提示条:当前显示默认模板(未改动)时出现 */
.template-hint {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-5);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  background: var(--color-surface-alt);
  border-bottom: 1px solid var(--color-border);
  line-height: var(--lh-relaxed);
}

.template-hint svg {
  color: var(--color-primary);
  flex-shrink: 0;
  margin-top: 2px;
}

.toolbar-left {
  min-width: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.title-static {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  margin: 0;
  color: var(--color-text);
}

.title-input {
  width: 100%;
  max-width: 480px;
  padding: var(--space-1) var(--space-2);
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.title-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.title-input:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.subtitle-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.subtitle {
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.subtitle.mono {
  font-family: var(--font-mono);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 360px;
}

.meta-sep {
  color: var(--color-text-muted);
}

.toolbar-right {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: nowrap;
}

/* 模式切换 */
.mode-switch {
  display: inline-flex;
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  padding: 2px;
  gap: 2px;
}

.mode-btn {
  padding: var(--space-1) var(--space-3);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.mode-btn:hover:not(:disabled):not(.active) {
  color: var(--color-text);
}

.mode-btn.active {
  background: var(--color-surface);
  color: var(--color-text);
  box-shadow: var(--shadow-sm);
}

.mode-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 内容区 */
.editor-content {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: var(--space-4) var(--space-5);
}

.content-placeholder {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
}

.md-editor {
  flex: 1;
  width: 100%;
  padding: var(--space-4);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  resize: none;
  outline: none;
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.md-editor:focus {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.md-editor:disabled {
  background: var(--color-surface-alt);
  cursor: not-allowed;
}

.md-editor.invalid {
  border-color: var(--color-danger);
}

.md-editor::placeholder {
  color: var(--color-text-muted);
}

/* 预览 */
.md-preview {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-4);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.preview-empty {
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
  text-align: center;
  padding: var(--space-8) 0;
}

/* Markdown 渲染样式(作用于 v-html 容器) */
.markdown-body {
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
  color: var(--color-text);
  word-break: break-word;
}

.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3) {
  margin: var(--space-4) 0 var(--space-2);
  font-weight: var(--fw-semibold);
  line-height: var(--lh-tight);
}

.markdown-body :deep(h1) { font-size: var(--fs-xl); }
.markdown-body :deep(h2) { font-size: var(--fs-lg); }
.markdown-body :deep(h3) { font-size: var(--fs-base); }

.markdown-body :deep(p) {
  margin: 0 0 var(--space-3);
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 0 0 var(--space-3);
  padding-left: var(--space-5);
}

.markdown-body :deep(li) {
  margin: var(--space-1) 0;
}

.markdown-body :deep(code) {
  font-family: var(--font-mono);
  font-size: 0.875em;
  padding: 2px 6px;
  background: var(--color-surface-alt);
  border-radius: var(--radius-sm);
}

.markdown-body :deep(pre) {
  margin: 0 0 var(--space-3);
  padding: var(--space-3);
  background: var(--color-surface-alt);
  border-radius: var(--radius-md);
  overflow-x: auto;
}

.markdown-body :deep(pre code) {
  padding: 0;
  background: transparent;
}

.markdown-body :deep(blockquote) {
  margin: 0 0 var(--space-3);
  padding: var(--space-2) var(--space-4);
  border-left: 3px solid var(--color-primary);
  background: var(--color-surface-alt);
  color: var(--color-text-secondary);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}

.markdown-body :deep(a) {
  color: var(--color-primary);
  text-decoration: none;
}

.markdown-body :deep(a:hover) {
  text-decoration: underline;
}

.markdown-body :deep(table) {
  width: 100%;
  border-collapse: collapse;
  margin: 0 0 var(--space-3);
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  text-align: left;
}

.markdown-body :deep(hr) {
  border: none;
  border-top: 1px solid var(--color-border);
  margin: var(--space-4) 0;
}

.char-count {
  font-size: var(--fs-xs);
  font-family: var(--font-mono);
  color: var(--color-text-muted);
  white-space: nowrap;
}

.char-count.over {
  color: var(--color-danger);
  font-weight: var(--fw-medium);
}

.action-error {
  font-size: var(--fs-xs);
  color: var(--color-danger);
  margin: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.editor-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
}

/* ============ 按钮 ============ */
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

.btn-danger {
  background: var(--color-surface);
  color: var(--color-danger);
  border-color: var(--color-border);
}

.btn-danger:hover:not(:disabled) {
  background: var(--color-danger-light);
  border-color: var(--color-danger);
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: white;
  border-radius: 50%;
  animation: btn-spin 0.8s linear infinite;
}

.btn-spinner.danger {
  border-color: rgba(239, 68, 68, 0.3);
  border-top-color: var(--color-danger);
}

@keyframes btn-spin {
  to { transform: rotate(360deg); }
}

.btn-link {
  background: none;
  border: none;
  padding: 4px 8px;
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-primary);
  cursor: pointer;
  border-radius: var(--radius-sm);
}

.btn-link:hover {
  text-decoration: underline;
}

/* spinner */
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

/* ============ Toast ============ */
.toast-popup {
  position: fixed;
  top: var(--space-5);
  left: 50%;
  transform: translateX(-50%);
  z-index: 2000;
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  min-width: 240px;
  max-width: 380px;
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
