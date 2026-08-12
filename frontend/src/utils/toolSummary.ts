/**
 * 工具调用紧凑展示:把浏览型工具(读文件/搜索文件/列目录)的
 * tool_call + tool_result 配对压缩成一行摘要,替代原来的多层折叠卡片。
 *
 * 数据来源(后端已有,无需改动):
 * - tool_call content 首行:人类可读 intent,末尾带 [tool_name] 标签
 * - tool_result content:工具返回的结构化 JSON(read_file/find_files/search_code/list_files)
 */

/** 工具项最小结构(TaskDetailView 的 DisplayItem 满足此接口) */
export interface ToolItem {
  id: string
  type?: string
  content?: string
}

/** 紧凑化的工具集合:浏览型只读操作,一行摘要即可表达全部信息 */
export const COMPACT_TOOLS = new Set([
  'read_file',
  'find_files',
  'search_code',
  'list_files',
])

/** 配对后的渲染段:紧凑工具对(单行摘要)或普通工具项(保持原折叠渲染)。
 * 泛型 T 保留调用方传入的具体项类型(如 TaskDetailView 的 DisplayItem) */
export type ToolSegment<T extends ToolItem = ToolItem> =
  | { kind: 'compact'; call: T; result: T | null }
  | { kind: 'plain'; items: T[] }

/** 安全解析 JSON,失败返回 null(tool_result 可能是错误消息文本) */
function tryParseJson(content: string): Record<string, unknown> | null {
  try {
    const v = JSON.parse(content)
    return v && typeof v === 'object' ? (v as Record<string, unknown>) : null
  } catch {
    return null
  }
}

/** 从 tool_call content 提取工具名(匹配首行末尾的 [tool_name] 标签) */
export function toolNameOf(item: ToolItem): string {
  const firstLine = (item.content || '').split('\n', 1)[0]
  const m = firstLine.match(/\[(\w+)\]$/)
  return m ? m[1] : ''
}

/** intent 首行(剥掉末尾 [tool_name] 标签) */
function intentOf(item: ToolItem): string {
  const firstLine = (item.content || '').split('\n', 1)[0]
  return firstLine.replace(/\s*\[\w+\]$/, '') || toolNameOf(item) || '工具调用'
}

/**
 * 生成紧凑工具的单行摘要文案。
 *
 * - result 为 null:执行中(react_agent 落库 tool_call 后、结果返回前的短暂窗口)
 * - result 非 JSON:执行失败,直接透出错误原因
 * - 正常:按工具类型提取计数/路径,如「阅读了 src/main.py · 356 行」
 */
export function buildToolSummary(call: ToolItem, result: ToolItem | null): string {
  const name = toolNameOf(call)
  const intent = intentOf(call)

  if (!result || !result.content) return `${intent} ...`

  const data = tryParseJson(result.content)
  if (!data) {
    const msg = result.content.replace(/^工具执行失败:\s*/, '')
    return `❌ ${intent} · ${msg.split('\n', 1)[0]}`
  }

  switch (name) {
    case 'read_file': {
      const path = (data.path as string) || '?'
      const lines = typeof data.total_lines === 'number' ? data.total_lines : null
      const suffix = data.truncated ? '(截断)' : ''
      const linesTxt = lines !== null ? ` · ${lines} 行` : ''
      return `📖 阅读了 ${path}${linesTxt}${suffix}`
    }
    case 'find_files': {
      const pattern = (data.pattern as string) || ''
      const n = typeof data.total === 'number' ? data.total : ((data.files as unknown[]) ?? []).length
      return `🔍 搜索到 ${n} 个文件${pattern ? `(${pattern})` : ''}`
    }
    case 'search_code': {
      const n = typeof data.total_matches === 'number'
        ? data.total_matches
        : ((data.matches as unknown[]) ?? []).length
      return `🔍 搜索到 ${n} 处代码匹配`
    }
    case 'list_files': {
      const path = (data.path as string) || '根目录'
      const n = typeof data.total === 'number' ? data.total : ((data.entries as unknown[]) ?? []).length
      return `📂 查看目录 ${path} · ${n} 项`
    }
    default:
      return intent
  }
}

/**
 * 把迭代内的 tool_call/tool_result 列表转成渲染段序列:
 * 紧凑工具与其紧邻的 result 配对成 compact 段,其余按原顺序合并进 plain 段。
 * 流式项(type 未定)与落单的 tool_result 一律归入 plain,保持原渲染兜底。
 */
export function buildToolSegments<T extends ToolItem>(items: T[]): ToolSegment<T>[] {
  const segments: ToolSegment<T>[] = []
  let plain: T[] = []
  const flush = () => {
    if (plain.length) {
      segments.push({ kind: 'plain', items: plain })
      plain = []
    }
  }
  for (let i = 0; i < items.length; i++) {
    const it = items[i]
    if (it.type === 'tool_call' && COMPACT_TOOLS.has(toolNameOf(it))) {
      flush()
      // react_agent 执行循环中 result 紧跟 call 落库;尚未到达时 result 为 null
      const next = items[i + 1]
      if (next && next.type === 'tool_result') {
        segments.push({ kind: 'compact', call: it, result: next })
        i++
      } else {
        segments.push({ kind: 'compact', call: it, result: null })
      }
    } else {
      plain.push(it)
    }
  }
  flush()
  return segments
}
