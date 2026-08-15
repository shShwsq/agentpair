/**
 * 工作区浏览 API
 *
 * 对应后端 app/routers/workspace.py:
 * - GET /tasks/{id}/workspace          工作区信息
 * - GET /tasks/{id}/workspace/files    列出目录
 * - GET /tasks/{id}/workspace/file     读取文件
 */
import client from './client'
import type {
  WorkspaceFileResponse,
  WorkspaceFilesResponse,
  WorkspaceInfo,
  WorkspaceTreeResponse,
} from '@/types/workspace'

/** 获取工作区信息(是否可浏览) */
export function getWorkspaceInfo(taskId: string): Promise<WorkspaceInfo> {
  return client.get(`/tasks/${taskId}/workspace`).then((r) => r.data)
}

/** 获取整树快照(首屏一次拉取,替代逐级懒加载) */
export function getWorkspaceTree(
  taskId: string,
  refresh: boolean = false,
): Promise<WorkspaceTreeResponse> {
  return client
    .get(`/tasks/${taskId}/workspace/tree`, { params: { refresh } })
    .then((r) => r.data)
}

/** 列出工作区某目录下的文件(单层,懒加载树) */
export function listWorkspaceFiles(
  taskId: string,
  subdir: string = '',
): Promise<WorkspaceFilesResponse> {
  return client
    .get(`/tasks/${taskId}/workspace/files`, { params: { subdir } })
    .then((r) => r.data)
}

/** 读取工作区内文件内容(原始文本 + 分页,前端自行渲染行号) */
export function readWorkspaceFile(
  taskId: string,
  path: string,
  offset: number = 1,
  maxLines: number = 500,
): Promise<WorkspaceFileResponse> {
  return client
    .get(`/tasks/${taskId}/workspace/file`, {
      params: { path, offset, max_lines: maxLines },
    })
    .then((r) => r.data)
}
