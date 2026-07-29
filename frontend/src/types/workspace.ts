/**
 * 工作区浏览相关类型
 *
 * 对应后端 app/routers/workspace.py 的端点
 */

/** 目录条目(文件或子目录) */
export interface WorkspaceEntry {
  name: string
  type: 'file' | 'dir'
  size: number
}

/** 列出目录的响应 */
export interface WorkspaceFilesResponse {
  path: string
  entries: WorkspaceEntry[]
  total: number
  truncated: boolean
}

/** 工作区信息 */
export interface WorkspaceInfo {
  available: boolean
  reason: string | null
  repo_path: string
  completed: boolean
  mode: string
}

/** 读取文件的响应 */
export interface WorkspaceFileResponse {
  path: string
  content: string
  start_line: number
  end_line: number
  total_lines: number
  truncated: boolean
}
