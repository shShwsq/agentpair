<script setup lang="ts">
/**
 * 技能管理页(上传 zip / 列表 / 右侧栏文件浏览与编辑 / 删除)
 *
 * 入口:主导航「技能管理」项(与记忆管理/协作策略并列)。
 *
 * 布局:
 * - 中栏宽度与 CLI 设置页对齐(max-width 680px 居中):上传区 + 技能列表
 * - 上传区为常规文件上传样式:左侧选择框点击弹出文件对话框并回显文件名,右侧上传按钮
 * - 点击列表项打开右侧栏:左边技能文件列表,右边文件编辑器(与记忆管理同款)
 *   · .md 文件支持「编辑 / 预览」切换,预览用 marked 渲染 + DOMPurify 净化
 *   · 自己的 skill 的 SKILL.md 可编辑保存(后端 upsert 热刷新);其余只读
 *
 * 安全:zip 由后端校验(zip-slip / 大小 / frontmatter / 扩展名白名单);
 * 文件读取接口后端做了路径穿越防护与 UTF-8 校验。
 */
import { computed, onMounted, ref } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

import AppHeader from '@/components/AppHeader.vue'
import CodeMirrorEditor from '@/components/CodeMirrorEditor.vue'
import WorkspaceSidebar from '@/components/WorkspaceSidebar.vue'
import WorkspaceToggleButton from '@/components/WorkspaceToggleButton.vue'
import {
  deleteSkill,
  getSkillFileContent,
  getSkillFiles,
  getSkills,
  updateSkill,
  uploadSkill,
  type SkillFileEntry,
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
const fileInputRef = ref<HTMLInputElement | null>(null)
const selectedFile = ref<File | null>(null)
/** 同名冲突时强制覆盖(需后端 409 提示后勾选) */
const forceOverwrite = ref(false)
const uploading = ref(false)

function triggerPick(): void {
  fileInputRef.value?.click()
}

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
    // 上传成功后直接打开新技能的文件浏览
    openSkill({
      name: result.skill.name,
      description: result.skill.description,
      scenario_id: result.skill.scenario_id,
      owned: result.skill.owned,
    })
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
// 右侧栏:文件列表 + 文件编辑器
// ============================================================
const activeSkill = ref<SkillSummary | null>(null)
const detailFiles = ref<SkillFileEntry[]>([])
const filesLoading = ref(false)
const filesError = ref('')

const activePath = ref('')
const draft = ref('')
const original = ref('')
const fileLoading = ref(false)
const fileError = ref('')
const saving = ref(false)
const mode = ref<'edit' | 'preview'>('edit')

/** 防止快速切换 skill/文件时旧请求回写新状态 */
let reqSeq = 0

function isMd(path: string): boolean {
  return path.toLowerCase().endsWith('.md')
}

const activeFile = computed<SkillFileEntry | undefined>(() =>
  detailFiles.value.find((f) => f.path === activePath.value),
)

/** 仅自己上传的 skill 的 SKILL.md 可编辑保存 */
const canEdit = computed(
  () => !!activeSkill.value?.owned && activePath.value === 'SKILL.md',
)

const isDirty = computed(() => draft.value !== original.value)
const canSave = computed(() => canEdit.value && isDirty.value && !saving.value)

/** 预览 HTML(marked 渲染 + DOMPurify 净化,防存储型 XSS) */
const previewHtml = computed(() => {
  if (!isMd(activePath.value) || !draft.value.trim()) return ''
  const raw = marked.parse(draft.value, { async: false }) as string
  return DOMPurify.sanitize(raw)
})

async function loadFiles(s: SkillSummary): Promise<void> {
  const seq = ++reqSeq
  filesLoading.value = true
  filesError.value = ''
  try {
    const files = await getSkillFiles(s.scenario_id, s.name)
    if (seq !== reqSeq) return
    detailFiles.value = files
    // 默认选中 SKILL.md(后端已置顶),否则选第一个文件
    const first = files.find((f) => f.path === 'SKILL.md') ?? files[0]
    if (first) {
      await selectFile(first.path)
    } else {
      activePath.value = ''
      draft.value = ''
      original.value = ''
    }
  } catch (e) {
    if (seq !== reqSeq) return
    filesError.value = extractErrorMessage(e)
  } finally {
    if (seq === reqSeq) filesLoading.value = false
  }
}

async function selectFile(path: string): Promise<void> {
  const s = activeSkill.value
  if (!s) return
  const seq = ++reqSeq
  activePath.value = path
  fileLoading.value = true
  fileError.value = ''
  draft.value = ''
  original.value = ''
  // md 文件:只读时默认预览,可编辑时默认编辑;其余文件默认源码视图
  const editable = s.owned && path === 'SKILL.md'
  mode.value = isMd(path) && !editable ? 'preview' : 'edit'
  try {
    const data = await getSkillFileContent(s.scenario_id, s.name, path)
    if (seq !== reqSeq) return
    draft.value = data.content
    original.value = data.content
  } catch (e) {
    if (seq !== reqSeq) return
    fileError.value = extractErrorMessage(e)
  } finally {
    if (seq === reqSeq) fileLoading.value = false
  }
}

function openSkill(s: SkillSummary): void {
  activeSkill.value = s
  detailFiles.value = []
  activePath.value = ''
  draft.value = ''
  original.value = ''
  filesError.value = ''
  fileError.value = ''
  loadFiles(s)
}

function closeSkill(): void {
  reqSeq++
  activeSkill.value = null
  detailFiles.value = []
  activePath.value = ''
  draft.value = ''
  original.value = ''
}

/** 保存 SKILL.md(直写全文,含 frontmatter;后端热刷新注册表) */
async function handleSave(): Promise<void> {
  const s = activeSkill.value
  if (!s || !canSave.value) return
  saving.value = true
  try {
    const result = await updateSkill(s.scenario_id, s.name, draft.value)
    original.value = draft.value
    showToast(`技能「${result.name}」已保存`, 'success')
    // frontmatter 改名/改描述时同步选中项与列表
    activeSkill.value = {
      name: result.name,
      description: result.description,
      scenario_id: result.scenario_id,
      owned: result.owned,
    }
    await loadSkills()
  } catch (e) {
    showToast(extractErrorMessage(e), 'error')
    await loadSkills()
  } finally {
    saving.value = false
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
    if (
      activeSkill.value &&
      activeSkill.value.scenario_id === s.scenario_id &&
      activeSkill.value.name === s.name
    ) {
      closeSkill()
    }
    await loadSkills()
  } catch (e) {
    showToast(extractErrorMessage(e), 'error')
  }
}

// ============================================================
// 工具
// ============================================================
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

/** 文件名(相对路径的最后一段,显示用) */
function fileName(path: string): string {
  const parts = path.split('/')
  return parts[parts.length - 1] || path
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

      <!-- ============ 中栏(宽度与 CLI 设置页对齐) ============ -->
      <div class="skill-center">
        <main class="main-col">
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
              <!-- 左侧选择框:点击弹出文件对话框,选中后回显文件名 -->
              <input
                ref="fileInputRef"
                type="file"
                accept=".zip,application/zip"
                class="file-input-hidden"
                @change="onPickFile"
              />
              <button
                type="button"
                class="file-picker"
                :class="{ 'has-file': !!selectedFile }"
                @click="triggerPick"
              >
                <svg class="picker-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                  <polyline points="14 2 14 8 20 8" />
                </svg>
                <span class="picker-text" :title="selectedFile?.name ?? ''">
                  {{ selectedFile ? selectedFile.name : '点击选择 .zip 文件' }}
                </span>
              </button>
              <label class="force-check">
                <input v-model="forceOverwrite" type="checkbox" :disabled="uploading" />
                覆盖同名技能
              </label>
              <!-- 右侧上传按钮 -->
              <button
                class="btn-upload"
                :disabled="uploading || !selectedFile"
                @click="handleUpload"
              >
                <span v-if="uploading" class="btn-spinner" />
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
              <li
                v-for="s in skills"
                :key="`${s.scenario_id}/${s.name}`"
                class="skill-item"
                :class="{ active: activeSkill && activeSkill.scenario_id === s.scenario_id && activeSkill.name === s.name }"
              >
                <button class="skill-main-btn" @click="openSkill(s)">
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

      <!-- ============ 右侧栏:文件列表 + 文件编辑器 ============ -->
      <aside v-if="activeSkill" class="detail-panel">
        <header class="panel-header">
          <div class="panel-title-col">
            <div class="panel-title-row">
              <h2 class="panel-title">{{ activeSkill.name }}</h2>
              <span class="skill-tag" :class="isBuiltin(activeSkill) ? 'tag-builtin' : 'tag-mine'">
                {{ isBuiltin(activeSkill) ? '系统内置' : '我的' }}
              </span>
            </div>
            <p class="panel-desc">{{ activeSkill.description }}</p>
          </div>
          <button class="panel-close" aria-label="关闭" title="关闭" @click="closeSkill">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="18" x2="6" y1="6" y2="18" /><line x1="6" x2="18" y1="6" y2="18" />
            </svg>
          </button>
        </header>

        <div class="panel-body">
          <!-- 文件列表 -->
          <div class="file-sidebar">
            <div v-if="filesLoading" class="sidebar-placeholder">
              <span class="status-spinner" /> 加载中...
            </div>
            <div v-else-if="filesError" class="sidebar-placeholder error-text">
              加载失败
              <button class="btn-link" @click="loadFiles(activeSkill)">重试</button>
            </div>
            <ul v-else class="file-list">
              <li v-for="f in detailFiles" :key="f.path">
                <button
                  class="file-item"
                  :class="{ active: activePath === f.path }"
                  :title="f.path"
                  @click="selectFile(f.path)"
                >
                  <svg class="file-icon" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <polyline points="14 2 14 8 20 8" />
                  </svg>
                  <span class="file-label">{{ fileName(f.path) }}</span>
                </button>
              </li>
            </ul>
          </div>

          <!-- 文件编辑器 -->
          <section class="editor-panel">
            <template v-if="activePath">
              <header class="editor-toolbar">
                <div class="toolbar-left">
                  <span class="editor-file-name" :title="activePath">{{ activePath }}</span>
                  <span v-if="activeFile" class="editor-file-size">{{ formatSize(activeFile.size) }}</span>
                  <span v-if="!canEdit" class="readonly-tag">只读</span>
                  <span v-else-if="isDirty" class="dirty-tag">未保存</span>
                </div>
                <div class="toolbar-right">
                  <!-- md 文件:编辑/预览切换 -->
                  <div v-if="isMd(activePath)" class="mode-switch" role="group" aria-label="编辑/预览">
                    <button
                      :class="['mode-btn', { active: mode === 'edit' }]"
                      :disabled="fileLoading"
                      @click="mode = 'edit'"
                    >编辑</button>
                    <button
                      :class="['mode-btn', { active: mode === 'preview' }]"
                      :disabled="fileLoading"
                      @click="mode = 'preview'"
                    >预览</button>
                  </div>
                  <button
                    v-if="canEdit"
                    class="btn-save"
                    :disabled="!canSave"
                    @click="handleSave"
                  >
                    <span v-if="saving" class="btn-spinner" />
                    {{ saving ? '保存中...' : (isDirty ? '保存' : '已保存') }}
                  </button>
                </div>
              </header>

              <div class="editor-content">
                <div v-if="fileLoading" class="content-placeholder">
                  <span class="status-spinner" /> 加载中...
                </div>
                <div v-else-if="fileError" class="content-placeholder error-text">
                  {{ fileError }}
                  <button class="btn-link" @click="selectFile(activePath)">重试</button>
                </div>
                <!-- 编辑模式:CodeMirror 编辑器(:key 切换文件时重建,清空撤销栈) -->
                <CodeMirrorEditor
                  v-else-if="mode === 'edit'"
                  :key="`${activeSkill.scenario_id}/${activeSkill.name}/${activePath}`"
                  v-model="draft"
                  class="code-area"
                  placeholder="暂无内容"
                  :disabled="!canEdit || saving"
                />
                <!-- 预览模式:Markdown 渲染 -->
                <div v-else class="md-preview">
                  <div v-if="previewHtml" class="markdown-body" v-html="previewHtml" />
                  <div v-else class="preview-empty">暂无内容</div>
                </div>
              </div>
            </template>

            <div v-else class="editor-empty">
              <p v-if="filesLoading"><span class="status-spinner" /> 加载中...</p>
              <p v-else>请从左侧选择一个文件</p>
            </div>
          </section>
        </div>
      </aside>
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

/* ============ 中栏(宽度与 CLI 设置页一致) ============ */
.skill-center {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
}

.main-col {
  max-width: 680px;
  margin: 0 auto;
  padding: var(--space-6) var(--space-5) var(--space-8);
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
}

.file-input-hidden {
  display: none;
}

/* 左侧选择框:点击选择文件,回显文件名 */
.file-picker {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  height: 40px;
  padding: 0 var(--space-3);
  font-size: var(--fs-sm);
  color: var(--color-text-muted);
  background: var(--color-surface-alt);
  border: 1px dashed var(--color-border-strong, var(--color-border));
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: border-color var(--transition-fast), color var(--transition-fast);
}

.file-picker:hover {
  border-color: var(--color-primary);
  color: var(--color-text-secondary);
}

.file-picker.has-file {
  border-style: solid;
  border-color: var(--color-border);
  color: var(--color-text);
}

.picker-icon {
  flex-shrink: 0;
  color: var(--color-text-muted);
}

.file-picker.has-file .picker-icon {
  color: var(--color-primary);
}

.picker-text {
  flex: 1;
  min-width: 0;
  text-align: left;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.force-check {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  cursor: pointer;
  white-space: nowrap;
}

/* 右侧上传按钮 */
.btn-upload {
  flex-shrink: 0;
  height: 40px;
  padding: 0 var(--space-5);
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text-inverse);
  background: var(--color-primary);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.btn-upload:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-upload:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, var(--color-text-inverse) 30%, transparent);
  border-top-color: var(--color-text-inverse);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
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

.skill-item.active {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
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

/* ============ 右侧栏 ============ */
.detail-panel {
  width: clamp(520px, 48vw, 700px);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
  border-left: 1px solid var(--color-border);
  overflow: hidden;
}

.panel-header {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.panel-title-col {
  flex: 1;
  min-width: 0;
}

.panel-title-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.panel-title {
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.panel-desc {
  margin: 2px 0 0;
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.panel-close {
  flex-shrink: 0;
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

.panel-close:hover {
  color: var(--color-text);
  background: var(--color-surface-alt);
}

.panel-body {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: stretch;
}

/* ---- 文件列表 ---- */
.file-sidebar {
  width: 190px;
  flex-shrink: 0;
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-bg);
}

.sidebar-placeholder {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
  padding: var(--space-4);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.file-list {
  list-style: none;
  margin: 0;
  padding: var(--space-2);
  flex: 1;
  overflow-y: auto;
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
}

.file-item:hover {
  background: var(--color-surface-alt);
}

.file-item.active {
  background: var(--color-primary-light);
  color: var(--color-primary-hover);
  font-weight: var(--fw-semibold);
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

/* ---- 文件编辑器 ---- */
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
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
  flex-shrink: 0;
}

.toolbar-left {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.editor-file-name {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.editor-file-size {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
}

.readonly-tag,
.dirty-tag {
  flex-shrink: 0;
  padding: 1px 6px;
  font-size: 11px;
  border-radius: var(--radius-full);
}

.readonly-tag {
  color: var(--color-text-muted);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-border);
}

.dirty-tag {
  color: var(--color-warning, #f59e0b);
  background: var(--color-surface-alt);
  border: 1px solid var(--color-warning, #f59e0b);
}

.toolbar-right {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

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

.btn-save {
  height: 30px;
  padding: 0 var(--space-3);
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-medium);
  color: var(--color-text-inverse);
  background: var(--color-primary);
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background var(--transition-fast);
}

.btn-save:hover:not(:disabled) {
  background: var(--color-primary-hover);
}

.btn-save:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.editor-content {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.content-placeholder {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-4);
  color: var(--color-text-secondary);
  font-size: var(--fs-sm);
}

.code-area {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  background: var(--color-surface);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: var(--lh-relaxed);
}

.md-preview {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-4) var(--space-5);
  background: var(--color-surface);
}

.preview-empty {
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
  text-align: center;
  padding: var(--space-8) 0;
}

.editor-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-muted);
  font-size: var(--fs-sm);
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
