/**
 * buildToolSegments / buildToolSummary 单元测试
 *
 * 覆盖紧凑工具(读文件/搜索/列目录)的配对与单行摘要生成:
 * 1. 配对:紧凑 tool_call 与紧邻 tool_result 成对,其余保持原顺序进 plain 段
 * 2. 摘要:各工具的计数/路径提取、执行中、执行失败兜底
 */
import { describe, expect, it } from 'vitest'

import {
  buildToolSegments,
  buildToolSummary,
  toolNameOf,
  type ToolItem,
} from './toolSummary'

function call(id: string, intent: string): ToolItem {
  return { id, type: 'tool_call', content: intent }
}

function result(id: string, content: string): ToolItem {
  return { id, type: 'tool_result', content }
}

describe('toolNameOf', () => {
  it('从首行末尾提取 [tool_name] 标签', () => {
    expect(toolNameOf(call('1', '读取文件 src/main.py [read_file]\n{"a":1}')))
      .toBe('read_file')
  })

  it('无标签时返回空串', () => {
    expect(toolNameOf(call('1', '旧数据无标签'))).toBe('')
  })

  it('只匹配首行,忽略后续行的方括号', () => {
    expect(toolNameOf(call('1', '执行命令: ls [run_command]\n{"pattern":"[a-z]"}')))
      .toBe('run_command')
  })
})

describe('buildToolSummary', () => {
  const readCall = call('1', '读取文件 src/main.py [read_file]')

  it('read_file:路径 + 总行数', () => {
    const r = result('2', JSON.stringify({
      path: 'src/main.py', content: '...', total_lines: 356, truncated: false,
    }))
    expect(buildToolSummary(readCall, r)).toBe('📖 阅读了 src/main.py · 356 行')
  })

  it('read_file:截断时追加标记', () => {
    const r = result('2', JSON.stringify({
      path: 'big.py', content: '...', total_lines: 9999, truncated: true,
    }))
    expect(buildToolSummary(readCall, r)).toBe('📖 阅读了 big.py · 9999 行(截断)')
  })

  it('find_files:文件数 + pattern', () => {
    const c = call('1', '查找文件: **/*.py [find_files]')
    const r = result('2', JSON.stringify({
      pattern: '**/*.py', files: ['a.py', 'b.py'], total: 12, truncated: false, offset: 0,
    }))
    expect(buildToolSummary(c, r)).toBe('🔍 搜索到 12 个文件(**/*.py)')
  })

  it('search_code:匹配数', () => {
    const c = call('1', '搜索代码: TODO [search_code]')
    const r = result('2', JSON.stringify({
      matches: [{ file: 'a.py', line: 1, content: 'TODO' }],
      total_matches: 8, truncated: false,
    }))
    expect(buildToolSummary(c, r)).toBe('🔍 搜索到 8 处代码匹配')
  })

  it('list_files:目录 + 条目数', () => {
    const c = call('1', '查看目录结构: src [list_files]')
    const r = result('2', JSON.stringify({
      path: 'src/', entries: [{ name: 'main.py', type: 'file', size: 1 }],
      total: 45, truncated: false,
    }))
    expect(buildToolSummary(c, r)).toBe('📂 查看目录 src/ · 45 项')
  })

  it('result 未到达:显示意图 + 省略号', () => {
    expect(buildToolSummary(readCall, null)).toBe('读取文件 src/main.py ...')
  })

  it('执行失败(非 JSON):透出错误原因', () => {
    const r = result('2', '工具执行失败: 文件不存在: nope.py')
    expect(buildToolSummary(readCall, r)).toBe('❌ 读取文件 src/main.py · 文件不存在: nope.py')
  })

  it('未知工具名:回退显示 intent', () => {
    const c = call('1', '调用 custom_tool [custom_tool]')
    const r = result('2', JSON.stringify({ ok: true }))
    expect(buildToolSummary(c, r)).toBe('调用 custom_tool')
  })
})

describe('buildToolSegments', () => {
  it('紧凑 call+result 配对成 compact 段', () => {
    const items = [
      call('c1', '读取文件 a.py [read_file]'),
      result('r1', '{"path":"a.py","total_lines":1}'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(1)
    expect(segs[0]).toMatchObject({ kind: 'compact', call: { id: 'c1' }, result: { id: 'r1' } })
  })

  it('紧凑 call 无 result(执行中):result 为 null', () => {
    const segs = buildToolSegments([call('c1', '读取文件 a.py [read_file]')])
    expect(segs).toHaveLength(1)
    expect(segs[0]).toMatchObject({ kind: 'compact', result: null })
  })

  it('非紧凑工具归入 plain 段且保持原顺序', () => {
    const items = [
      call('c1', '写入文件 b.py [write_file]'),
      result('r1', 'ok'),
      call('c2', '执行命令: pytest [run_command]'),
      result('r2', 'passed'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(1)
    expect(segs[0].kind).toBe('plain')
    if (segs[0].kind === 'plain') {
      expect(segs[0].items.map((i) => i.id)).toEqual(['c1', 'r1', 'c2', 'r2'])
    }
  })

  it('混合场景:紧凑段与普通段交替,顺序不乱', () => {
    const items = [
      call('c1', '读取文件 a.py [read_file]'),
      result('r1', '{}'),
      call('c2', '写入文件 b.py [write_file]'),
      result('r2', 'ok'),
      call('c3', '搜索代码: TODO [search_code]'),
      result('r3', '{}'),
    ]
    const segs = buildToolSegments(items)
    expect(segs.map((s) => s.kind)).toEqual(['compact', 'plain', 'compact'])
    if (segs[1].kind === 'plain') {
      expect(segs[1].items.map((i) => i.id)).toEqual(['c2', 'r2'])
    }
  })

  it('落单的 tool_result 归入 plain 兜底', () => {
    const segs = buildToolSegments([result('r1', '孤儿结果')])
    expect(segs).toHaveLength(1)
    expect(segs[0].kind).toBe('plain')
  })
})
