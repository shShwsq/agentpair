<script setup lang="ts">
/**
 * 工作区侧栏组件
 *
 * 布局:左侧文件树 + 右侧文件内容查看面板
 *
 * 文件树用懒加载(参考 Claude Code LS):首次只加载根目录,
 * 点击文件夹时才加载其子目录。树扁平化为带 depth 的列表渲染,避免递归组件。
 *
 * 生命周期:
 * - 任务运行中:clone 完成后即可浏览;若尚未 clone,轮询检查可用性
 * - 任务完成后:session 保留 1 小时(后端 TTL),供用户回看
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'

import {
  getWorkspaceInfo,
  listWorkspaceFiles,
  readWorkspaceFile,
} from '@/api/workspace'
import { extractErrorMessage } from '@/utils/error'
import type { WorkspaceEntry } from '@/types/workspace'

const props = defineProps<{
  taskId: string
  /** 任务是否运行中(运行中轮询工作区可用性) */
  isRunning: boolean
}>()

// ---- 工作区可用性 ----
const available = ref(false)
const unavailableReason = ref('')
const checkingAvailable = ref(false)

// ---- 文件树 ----
interface TreeNode {
  name: string
  path: string // 相对仓库的路径(根节点为 "")
  type: 'dir' | 'file'
  expanded: boolean
  loaded: boolean
  loading: boolean
  children: TreeNode[]
}

const treeRoot = reactive<TreeNode>({
  name: '',
  path: '',
  type: 'dir',
  expanded: true,
  loaded: false,
  loading: false,
  children: [],
})

// ---- 当前选中文件 + 内容 ----
const selectedFilePath = ref<string | null>(null)
const fileContent = ref('')
const fileStartLine = ref(0)
const fileEndLine = ref(0)
const fileTotalLines = ref(0)
const fileTruncated = ref(false)
const loadingFile = ref(false)
const fileOffset = ref(1)

// ---- 错误提示 ----
const errorMsg = ref('')

// ============================================================
// 扁平化树:把嵌套的 TreeNode 展开成带 depth 的一维列表(仅已展开的目录)
// ============================================================

interface FlatNode {
  node: TreeNode
  depth: number
}

const flatTree = computed<FlatNode[]>(() => {
  const result: FlatNode[] = []
  const walk = (n: TreeNode, depth: number) => {
    if (depth >= 0) result.push({ node: n, depth })
    if (n.type === 'dir' && n.expanded && n.loaded) {
      for (const child of n.children) {
        walk(child, depth + 1)
      }
    }
  }
  walk(treeRoot, -1) // 根节点不显示,从其子节点开始
  return result
})

// ============================================================
// 初始化 + 轮询
// ============================================================

async function checkAvailable(): Promise<void> {
  checkingAvailable.value = true
  try {
    const info = await getWorkspaceInfo(props.taskId)
    available.value = info.available
    unavailableReason.value = info.reason ?? ''
    if (info.available && !treeRoot.loaded) {
      await loadDir(treeRoot)
    }
  } catch (e) {
    errorMsg.value = extractErrorMessage(e)
  } finally {
    checkingAvailable.value = false
  }
}

let pollTimer: ReturnType<typeof setInterval> | null = null

function startPolling(): void {
  stopPolling()
  pollTimer = setInterval(async () => {
    if (available.value || !props.isRunning) {
      stopPolling()
      return
    }
    await checkAvailable()
  }, 5000)
}

function stopPolling(): void {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

let initialized = false
async function ensureInitialized(): Promise<void> {
  if (initialized) return
  initialized = true
  await checkAvailable()
  if (!available.value && props.isRunning) {
    startPolling()
  }
}

// 任务从运行变为完成时,再检查一次(确保拿到最终状态)
watch(
  () => props.isRunning,
  (running, wasRunning) => {
    if (wasRunning && !running && !available.value) {
      checkAvailable()
    }
  },
)

// 组件仅在展开时挂载,挂载即初始化
onMounted(ensureInitialized)
onUnmounted(stopPolling)

// ============================================================
// 文件树操作
// ============================================================

async function loadDir(node: TreeNode): Promise<void> {
  if (node.loaded || node.loading) return
  node.loading = true
  errorMsg.value = ''
  try {
    const res = await listWorkspaceFiles(props.taskId, node.path)
    node.children = res.entries.map((e: WorkspaceEntry) => ({
      name: e.name,
      path: node.path ? `${node.path}/${e.name}` : e.name,
      type: e.type,
      expanded: false,
      loaded: false,
      loading: false,
      children: [],
    }))
    node.loaded = true
  } catch (e) {
    errorMsg.value = extractErrorMessage(e)
  } finally {
    node.loading = false
  }
}

async function toggleDir(node: TreeNode): Promise<void> {
  if (node.type !== 'dir') return
  if (!node.loaded) {
    await loadDir(node)
  }
  node.expanded = !node.expanded
}

async function selectFile(node: TreeNode): Promise<void> {
  if (node.type !== 'file') return
  selectedFilePath.value = node.path
  fileOffset.value = 1
  await loadFileContent()
}

async function loadFileContent(): Promise<void> {
  if (!selectedFilePath.value) return
  loadingFile.value = true
  errorMsg.value = ''
  try {
    const res = await readWorkspaceFile(
      props.taskId,
      selectedFilePath.value,
      fileOffset.value,
      500,
    )
    fileContent.value = res.content
    fileStartLine.value = res.start_line
    fileEndLine.value = res.end_line
    fileTotalLines.value = res.total_lines
    fileTruncated.value = res.truncated
  } catch (e) {
    errorMsg.value = extractErrorMessage(e)
    fileContent.value = ''
  } finally {
    loadingFile.value = false
  }
}

async function loadNextPage(): Promise<void> {
  if (!fileTruncated.value || loadingFile.value) return
  fileOffset.value = fileEndLine.value + 1
  await loadFileContent()
}

async function loadPrevPage(): Promise<void> {
  if (fileOffset.value <= 1 || loadingFile.value) return
  fileOffset.value = Math.max(1, fileOffset.value - 500)
  await loadFileContent()
}

async function refreshTree(): Promise<void> {
  errorMsg.value = ''
  await refreshNode(treeRoot)
}

async function refreshNode(node: TreeNode): Promise<void> {
  if (node.type === 'dir' && node.loaded) {
    node.loaded = false
    node.children = []
    await loadDir(node)
    for (const child of node.children) {
      if (child.expanded) {
        await refreshNode(child)
      }
    }
  }
}

// ============================================================
// 计算属性
// ============================================================

const selectedFileName = computed(() => {
  if (!selectedFilePath.value) return ''
  const parts = selectedFilePath.value.split('/')
  return parts[parts.length - 1]
})

const hasPrevPage = computed(() => fileOffset.value > 1)
const hasNextPage = computed(() => fileTruncated.value)
const showFilePanel = computed(() => selectedFilePath.value !== null)
</script>

<template>
  <div class="workspace-container">
    <!-- 侧栏(组件仅在展开时挂载) -->
    <aside class="workspace-sidebar">
      <div class="sidebar-header">
        <span class="sidebar-title">📁 工作区</span>
        <div class="sidebar-actions">
          <button
            class="icon-btn"
            title="刷新文件树"
            :disabled="!available"
            @click="refreshTree"
          >↻</button>
        </div>
      </div>

      <!-- 检查中 -->
      <div v-if="checkingAvailable && !available" class="sidebar-status">
        <span class="spinner-sm" /> 检查工作区...
      </div>

      <!-- 不可用 -->
      <div v-else-if="!available" class="sidebar-status sidebar-status-muted">
        <p>{{ unavailableReason || '工作区不可用' }}</p>
        <p v-if="isRunning" class="status-hint">等待 react_agent clone 仓库...</p>
      </div>

      <!-- 文件树(扁平化渲染) -->
      <div v-else class="file-tree">
        <div
          v-for="item in flatTree"
          :key="item.node.path"
          class="tree-node"
          :class="[
            `tree-${item.node.type}`,
            { 'tree-selected': selectedFilePath === item.node.path },
          ]"
          :style="{ paddingLeft: `${item.depth * 14 + 8}px` }"
          @click="item.node.type === 'dir' ? toggleDir(item.node) : selectFile(item.node)"
        >
          <span class="tree-icon">
            {{ item.node.type === 'dir'
              ? (item.node.expanded ? '📂' : '📁')
              : '📄' }}
          </span>
          <span class="tree-name">{{ item.node.name }}</span>
          <span v-if="item.node.loading" class="tree-loading">...</span>
        </div>
        <div v-if="treeRoot.loaded && treeRoot.children.length === 0" class="empty-tree">
          (空目录)
        </div>
      </div>

      <!-- 错误提示 -->
      <div v-if="errorMsg" class="sidebar-error">{{ errorMsg }}</div>
    </aside>

    <!-- 文件查看面板(右侧,仅展开侧栏且选中文件时显示) -->
    <section v-if="showFilePanel" class="file-panel">
      <div class="file-panel-header">
        <span class="file-path" :title="selectedFilePath ?? undefined">{{ selectedFileName }}</span>
        <div class="file-pagination">
          <button class="page-btn" :disabled="!hasPrevPage || loadingFile" @click="loadPrevPage">
            ↑
          </button>
          <span class="page-info">
            {{ fileStartLine }}-{{ fileEndLine }} / {{ fileTotalLines }}
          </span>
          <button class="page-btn" :disabled="!hasNextPage || loadingFile" @click="loadNextPage">
            ↓
          </button>
        </div>
      </div>
      <div class="file-content">
        <div v-if="loadingFile" class="file-loading">
          <span class="spinner-sm" /> 加载中...
        </div>
        <pre v-else><code>{{ fileContent || '(空文件)' }}</code></pre>
      </div>
    </section>
  </div>
</template>

<style scoped>
.workspace-container {
  display: flex;
  flex-shrink: 0;
  height: 100%;
  overflow: hidden;
}

/* ---- 侧栏 ---- */
.workspace-sidebar {
  flex-shrink: 0;
  width: 280px;
  border-right: 1px solid var(--color-border);
  background: var(--color-surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.sidebar-title {
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
}

.sidebar-actions {
  display: flex;
  gap: 2px;
}

.icon-btn {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  border-radius: var(--radius-sm);
  font-size: 13px;
  transition: all var(--transition-fast);
}

.icon-btn:hover:not(:disabled) {
  background: var(--color-surface-alt);
  color: var(--color-text);
}

.icon-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* ---- 侧栏状态 ---- */
.sidebar-status {
  padding: var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-secondary);
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.sidebar-status-muted {
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-1);
}

.status-hint {
  color: var(--color-text-muted);
  font-size: 10px;
}

/* ---- 文件树 ---- */
.file-tree {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-1) 0;
}

.empty-tree {
  padding: var(--space-3);
  font-size: var(--fs-xs);
  color: var(--color-text-muted);
  text-align: center;
}

.tree-node {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 3px var(--space-2);
  cursor: pointer;
  font-size: var(--fs-xs);
  color: var(--color-text);
  transition: background var(--transition-fast);
  white-space: nowrap;
  overflow: hidden;
  line-height: 1.6;
}

.tree-node:hover {
  background: var(--color-surface-alt);
}

.tree-selected {
  background: var(--color-primary-light) !important;
  color: var(--color-primary);
}

.tree-icon {
  flex-shrink: 0;
  font-size: 12px;
}

.tree-name {
  overflow: hidden;
  text-overflow: ellipsis;
}

.tree-loading {
  color: var(--color-text-muted);
  font-size: 10px;
}

.tree-dir .tree-name {
  font-weight: var(--fw-medium);
}

/* ---- 文件查看面板 ---- */
.file-panel {
  width: 480px;
  flex-shrink: 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-bg);
  border-right: 1px solid var(--color-border);
}

.file-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
  flex-shrink: 0;
}

.file-path {
  font-size: var(--fs-xs);
  font-family: var(--font-mono);
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}

.file-pagination {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-shrink: 0;
}

.page-btn {
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
}

.page-btn:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.page-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.page-info {
  font-size: 10px;
  color: var(--color-text-muted);
  font-family: var(--font-mono);
  white-space: nowrap;
}

.file-content {
  flex: 1;
  overflow: auto;
  padding: var(--space-2) 0;
}

.file-content pre {
  margin: 0;
  padding: 0 var(--space-4);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text);
  white-space: pre;
}

.file-loading {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-4);
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}

.spinner-sm {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.sidebar-error {
  padding: var(--space-2) var(--space-3);
  font-size: 10px;
  color: var(--color-danger);
  background: var(--color-danger-light);
  border-top: 1px solid #fecaca;
  flex-shrink: 0;
}
</style>
