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

// ============================================================
// Bash/run_command 命令解析:只读命令白名单 → 单行摘要
//
// 来源:logs/acp 真实命令分布统计(cat 59/find 20/ls 12/grep 5/wc/head/tree),
// CLI 智能体(如 codex)无 Read 工具,全靠 bash 命令读文件。
// 安全策略:整条命令(含管道/链式)全部命中白名单才紧凑化,
// 出现写操作/未知命令/命令替换时回退原折叠展示。
// ============================================================

const READ_FILE_CMDS = new Set(['cat', 'head', 'tail', 'bat', 'less', 'more'])
const LIST_DIR_CMDS = new Set(['ls', 'll', 'dir', 'tree'])
const SEARCH_CMDS = new Set(['find', 'grep', 'rg', 'egrep', 'fgrep'])
/** 只读辅助命令(管道尾/链式成员安全) */
const AUX_CMDS = new Set([
  'wc', 'sort', 'uniq', 'cut', 'tr', 'file', 'stat', 'pwd', 'echo', 'true', 'cd',
])
const READONLY_CMDS = new Set([
  ...READ_FILE_CMDS, ...LIST_DIR_CMDS, ...SEARCH_CMDS, ...AUX_CMDS,
])

/** 从 tool_call content 提取命令文本:Bash 的 detail 是命令原文,
 * run_command(react_agent)的 detail 是参数 JSON(含 command 字段) */
export function commandOf(call: ToolItem): string {
  const content = call.content || ''
  const idx = content.indexOf('\n')
  if (idx < 0) return ''
  const detail = content.slice(idx + 1).trim()
  if (detail.startsWith('{')) {
    try {
      const args = JSON.parse(detail)
      if (typeof args.command === 'string') return args.command
    } catch { /* 非 JSON 则按命令原文处理 */ }
  }
  return detail
}

/** 剥掉 /repos/<仓库名>/ 前缀,路径更短更易读 */
export function shortenPath(p: string): string {
  const m = p.match(/\/repos\/[^/]+\/(.+)$/)
  return m ? m[1] : p
}

function stripQuotes(s: string): string {
  return s.replace(/^(['"])(.*)\1$/, '$2')
}

/** 取段的非 flag 参数(跳过 -n 等选项的值) */
function filesOf(args: string[]): string[] {
  const files: string[] = []
  for (let i = 0; i < args.length; i++) {
    const a = args[i]
    if (a.startsWith('-')) {
      if (a === '-n' || a === '-c') i++ // 跳过选项值
      continue
    }
    files.push(a)
  }
  return files
}

/** 提取 head/tail 的行数:-200 / -n 200 / -n200 */
function headTailLines(args: string[]): string | null {
  for (let i = 0; i < args.length; i++) {
    const a = args[i]
    if (/^-\d+$/.test(a)) return a.slice(1)
    if (a === '-n' && /^\d+$/.test(args[i + 1] || '')) return args[i + 1]
    const m = a.match(/^-n(\d+)$/)
    if (m) return m[1]
  }
  return null
}

interface CmdSeg { cmd: string; args: string[] }

/**
 * 解析命令为链式管道结构;含非只读内容时返回 null。
 * 处理:/bin/bash -lc 包装、cd 前缀、2>/dev/null 重定向、|| 回退分支(取首支)。
 */
function parseReadonlyChains(cmdRaw: string): CmdSeg[][] | null {
  let c = cmdRaw.trim()
  if (!c) return null
  // 危险特征直接拒绝:命令替换、sudo、xargs(可执行任意命令)
  if (/`|\$\(|\bsudo\b|\bxargs\b/.test(c)) return null
  // 剥掉 /bin/bash -lc '...' 包装(bash/zsh/sh,支持 -lc -c 等组合 flag)
  c = c.replace(/^(?:\/usr)?\/bin\/(?:ba|da|z)?sh\s+(?:-\w+\s+)+['"]?/, '')
  c = c.replace(/['"]$/, '')

  const chains = c.split(/\s*(?:&&|;)\s*/).filter(Boolean)
  if (!chains.length) return null

  const result: CmdSeg[][] = []
  for (const chain of chains) {
    // || 回退分支(如 || echo "not found")只看首支
    const main = chain.split(/\s*\|\|\s*/)[0]
    const pipes = main.split('|')
    const segs: CmdSeg[] = []
    for (const p of pipes) {
      // 剥 /dev/null 重定向与 fd 复制;剩余 > 视为写文件 → 拒绝
      const cleaned = p
        .replace(/\d*\s*>>?\s*\/dev\/null/g, '')
        .replace(/\d*>\s*&\d+/g, '')
      if (cleaned.includes('>')) return null
      const tokens = cleaned.trim().split(/\s+/).filter(Boolean).map(stripQuotes)
      // 跳过 env VAR=x 前缀
      let i = 0
      while (i < tokens.length && /^[\w]+=.*/.test(tokens[i])) i++
      const cmdToken = tokens[i]
      if (!cmdToken) return null
      const name = cmdToken.split('/').pop() || ''
      if (!READONLY_CMDS.has(name)) return null
      segs.push({ cmd: name, args: tokens.slice(i + 1) })
    }
    result.push(segs)
  }
  return result
}

/** 单个链式分支的摘要(无可展示内容如 cd/echo 时返回 null) */
function summarizeChain(segs: CmdSeg[]): string | null {
  const head = segs[0]
  const tail = segs.length > 1 ? segs[segs.length - 1] : null
  // 管道尾修饰:cat f | head -200 → 前 200 行;tail → 后 N 行
  let mod = ''
  if (tail && (tail.cmd === 'head' || tail.cmd === 'tail')) {
    const n = headTailLines(tail.args)
    if (n) mod = ` · ${tail.cmd === 'head' ? '前' : '后'} ${n} 行`
  }

  if (READ_FILE_CMDS.has(head.cmd)) {
    const files = filesOf(head.args)
    if (!files.length) return null
    if (head.cmd === 'head' || head.cmd === 'tail') {
      // head/tail 自身就是读文件:行数从自身参数取
      const n = headTailLines(head.args)
      const m = n ? ` · ${head.cmd === 'head' ? '前' : '后'} ${n} 行` : ''
      return files.length === 1
        ? `📖 阅读了 ${shortenPath(files[0])}${m}`
        : `📖 阅读了 ${files.length} 个文件${m}`
    }
    return files.length === 1
      ? `📖 阅读了 ${shortenPath(files[0])}${mod}`
      : `📖 阅读了 ${files.length} 个文件${mod}`
  }
  if (head.cmd === 'tree') {
    const dir = filesOf(head.args)[0]
    return `📂 查看目录树 ${dir ? shortenPath(dir) : '.'}`
  }
  if (LIST_DIR_CMDS.has(head.cmd)) {
    const dir = filesOf(head.args)[0]
    return `📂 查看目录 ${dir ? shortenPath(dir) : '.'}`
  }
  if (head.cmd === 'find') {
    const args = head.args
    const dir = args.find((a) => !a.startsWith('-'))
    const nameIdx = args.findIndex((a) => a === '-name' || a === '-iname')
    const pattern = nameIdx >= 0 ? stripQuotes(args[nameIdx + 1] || '') : ''
    return pattern
      ? `🔍 查找文件 ${pattern}`
      : `🔍 查找文件${dir ? `(${shortenPath(dir)})` : ''}`
  }
  if (SEARCH_CMDS.has(head.cmd)) {
    const nonFlag = filesOf(head.args)
    const pattern = (nonFlag[0] || '').slice(0, 40)
    const path = nonFlag[1]
    if (!pattern) return null
    return `🔍 搜索 ${pattern}${path ? ` · ${shortenPath(path)}` : ''}`
  }
  if (head.cmd === 'wc') {
    const files = filesOf(head.args)
    return files.length
      ? `📄 统计行数 ${shortenPath(files[0])}`
      : '📄 统计行数'
  }
  // cd/echo/pwd/sort 等辅助命令无独立展示价值
  return null
}

/**
 * 命令摘要:整条命令全部只读时返回单行文案,否则 null(保持原折叠展示)。
 * 多个链式分支(如 ls x && cat y)摘要用顿号连接,最多 3 段。
 */
export function summarizeCommand(cmdRaw: string): string | null {
  const chains = parseReadonlyChains(cmdRaw)
  if (!chains) return null
  const parts = chains
    .map(summarizeChain)
    .filter((s): s is string => !!s)
  if (!parts.length) return null
  if (parts.length > 3) return `${parts.slice(0, 3).join('、')} 等`
  return parts.join('、')
}

/** CLI 智能体原生浏览工具(Qoder Read/Grep/Glob):从调用参数 JSON 摘要,
 * 不依赖结果(结果是原始输出文本) */
const CLI_COMPACT_TOOLS = new Set(['Read', 'Grep', 'Glob'])

/** tool_call content 首行后的 detail 解析为 JSON(失败返回 null) */
function detailJsonOf(call: ToolItem): Record<string, unknown> | null {
  const content = call.content || ''
  const idx = content.indexOf('\n')
  if (idx < 0) return null
  const detail = content.slice(idx + 1).trim()
  if (!detail.startsWith('{')) return null
  try {
    const v = JSON.parse(detail)
    return v && typeof v === 'object' ? (v as Record<string, unknown>) : null
  } catch {
    return null
  }
}

/**
 * Read/Grep/Glob 单行摘要:从调用参数 JSON 提取路径/模式。
 * 无有效参数(如旧数据只有 intent 没有 detail)时返回 null,回退原折叠展示。
 */
export function summarizeCliToolCall(call: ToolItem): string | null {
  const name = toolNameOf(call)
  const data = detailJsonOf(call)
  if (!data) return null
  if (name === 'Read') {
    const fp = typeof data.file_path === 'string' ? data.file_path : ''
    if (!fp) return null
    // limit/offset:读指定行范围时标注
    let mod = ''
    const limit = typeof data.limit === 'number' ? data.limit : null
    const offset = typeof data.offset === 'number' ? data.offset : 1
    if (limit !== null) {
      mod = offset > 1 ? ` · 第 ${offset}~${offset + limit} 行` : ` · 前 ${limit} 行`
    }
    return `📖 阅读了 ${shortenPath(fp)}${mod}`
  }
  if (name === 'Grep') {
    const pattern = typeof data.pattern === 'string' ? data.pattern.slice(0, 40) : ''
    if (!pattern) return null
    const path = typeof data.path === 'string' ? data.path : ''
    return `🔍 搜索 ${pattern}${path ? ` · ${shortenPath(path)}` : ''}`
  }
  if (name === 'Glob') {
    const pattern = typeof data.pattern === 'string' ? data.pattern : ''
    if (!pattern) return null
    return `🔍 查找文件 ${pattern}`
  }
  return null
}

/** 判断 tool_call 是否可紧凑化:内置浏览型工具、只读命令的 Bash/run_command,
 * 或参数完整的 CLI 原生 Read/Grep/Glob */
function isCompactCall(it: ToolItem): boolean {
  const name = toolNameOf(it)
  if (COMPACT_TOOLS.has(name)) return true
  if (CLI_COMPACT_TOOLS.has(name)) return !!summarizeCliToolCall(it)
  if (name === 'Bash' || name === 'run_command') {
    return !!summarizeCommand(commandOf(it))
  }
  return false
}

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

  // Bash/run_command:按命令内容摘要(结果是原始输出文本,非 JSON)
  if (name === 'Bash' || name === 'run_command') {
    const s = summarizeCommand(commandOf(call))
    if (!s) return intent
    if (!result || !result.content) return `${s} ...`
    return s
  }

  // CLI 原生 Read/Grep/Glob:按调用参数摘要(结果同样是原始输出文本)
  if (CLI_COMPACT_TOOLS.has(name)) {
    const s = summarizeCliToolCall(call)
    if (!s) return intent
    if (!result || !result.content) return `${s} ...`
    return s
  }

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
    if (it.type === 'tool_call' && isCompactCall(it)) {
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
