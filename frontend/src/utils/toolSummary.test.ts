/**
 * buildToolSegments / buildToolSummary 单元测试
 *
 * 覆盖紧凑工具(读文件/搜索/列目录)的配对与单行摘要生成:
 * 1. 配对:紧凑 tool_call 与紧邻 tool_result 成对,其余保持原顺序进 plain 段
 * 2. 摘要:各工具的计数/路径提取、执行中、执行失败兜底
 * 3. Bash/run_command 命令解析:只读命令白名单摘要、危险命令拒绝
 */
import { describe, expect, it } from 'vitest'

import {
  buildToolSegments,
  buildToolSummary,
  commandOf,
  shortenPath,
  summarizeCliToolCall,
  summarizeCommand,
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

describe('commandOf', () => {
  it('Bash:首行后全部是命令原文', () => {
    const c = call('1', "执行: cat /x/f.py [Bash]\n/bin/bash -lc 'cat /x/f.py'")
    expect(commandOf(c)).toBe("/bin/bash -lc 'cat /x/f.py'")
  })

  it('run_command:从参数 JSON 提取 command 字段', () => {
    const c = call('1', '执行命令: pytest [run_command]\n{"command": "pytest -x", "timeout": 60}')
    expect(commandOf(c)).toBe('pytest -x')
  })

  it('无 detail 时返回空串', () => {
    expect(commandOf(call('1', '执行: ls [Bash]'))).toBe('')
  })
})

describe('shortenPath', () => {
  it('剥掉 /repos/<仓库名>/ 前缀', () => {
    expect(shortenPath('/home/user/repos/openclaw-manager/services/app.py'))
      .toBe('services/app.py')
  })

  it('无仓库前缀时保持原样', () => {
    expect(shortenPath('src/main.py')).toBe('src/main.py')
  })
})

describe('summarizeCommand(日志真实命令形态)', () => {
  it('cat 单文件(bash -lc 包装)', () => {
    expect(summarizeCommand(
      "/bin/bash -lc 'cat /home/user/repos/openclaw-manager/services/manager-web/metadata_store.py'",
    )).toBe('📖 阅读了 services/manager-web/metadata_store.py')
  })

  it('cat | head -N → 前 N 行', () => {
    expect(summarizeCommand(
      "/bin/bash -lc 'cat /home/user/repos/proj/services/manager-web/app.py | head -200'",
    )).toBe('📖 阅读了 services/manager-web/app.py · 前 200 行')
  })

  it('cat 多文件', () => {
    expect(summarizeCommand('cat a.py b.py c.py')).toBe('📖 阅读了 3 个文件')
  })

  it('cat 带 || echo 回退分支', () => {
    expect(summarizeCommand(
      '/bin/bash -lc \'cat /home/user/.agent_memory/project_memory.md 2>/dev/null || echo "Memory file not found"\'',
    )).toBe('📖 阅读了 /home/user/.agent_memory/project_memory.md')
  })

  it('head -n N 直接读文件', () => {
    expect(summarizeCommand('head -n 50 src/main.py')).toBe('📖 阅读了 src/main.py · 前 50 行')
  })

  it('tail -20 读文件尾部', () => {
    expect(summarizeCommand('tail -20 app.log')).toBe('📖 阅读了 app.log · 后 20 行')
  })

  it('ls -la 列目录', () => {
    expect(summarizeCommand(
      "ls -la /home/user/repos/openclaw-manager/services/",
    )).toBe('📂 查看目录 services/')
  })

  it('tree 列目录树', () => {
    expect(summarizeCommand('tree src')).toBe('📂 查看目录树 src')
  })

  it('find -name 查找文件', () => {
    expect(summarizeCommand(
      '/bin/bash -lc \'find /home/user/repos/openclaw-manager -type f -name "*.py" | head -50\'',
    )).toBe('🔍 查找文件 *.py')
  })

  it('grep 搜索内容与路径', () => {
    expect(summarizeCommand('grep -rn "TODO" src/')).toBe('🔍 搜索 TODO · src/')
  })

  it('wc -l 统计行数', () => {
    expect(summarizeCommand('wc -l src/main.py')).toBe('📄 统计行数 src/main.py')
  })

  it('链式命令 cd + ls && cat | head', () => {
    expect(summarizeCommand(
      "/bin/bash -lc 'cd /home/user/repos/proj && ls -la *.md && cat README.md | head -100'",
    )).toBe('📂 查看目录 *.md、📖 阅读了 README.md · 前 100 行')
  })

  it('非只读命令拒绝:rm', () => {
    expect(summarizeCommand('rm -rf /tmp/x')).toBeNull()
  })

  it('写重定向拒绝:cat f > out', () => {
    expect(summarizeCommand('cat a.py > out.txt')).toBeNull()
  })

  it('命令替换拒绝:$( )', () => {
    expect(summarizeCommand('echo $(cat /etc/passwd)')).toBeNull()
  })

  it('xargs 拒绝(可执行任意命令)', () => {
    expect(summarizeCommand(
      "/bin/bash -lc 'find /home/user/repos/proj -name \"*.py\" -type f | xargs wc -l | tail -20'",
    )).toBeNull()
  })

  it('sudo 拒绝', () => {
    expect(summarizeCommand('sudo cat /etc/shadow')).toBeNull()
  })

  it('未知命令拒绝:pip install', () => {
    expect(summarizeCommand('pip install requests')).toBeNull()
  })
})

describe('CLI 原生 Read/Grep/Glob 紧凑化(Qoder 日志真实形态)', () => {
  it('Read:file_path 剥仓库前缀', () => {
    const c = call('1', '调用 Read [Read]\n{"file_path": "/home/user/repos/openclaw-manager/CONTEXT.md"}')
    expect(summarizeCliToolCall(c)).toBe('📖 阅读了 CONTEXT.md')
  })

  it('Read 带 limit:前 N 行', () => {
    const c = call('1', '调用 Read [Read]\n{"file_path": "/x/services/app.py", "limit": 400}')
    expect(summarizeCliToolCall(c)).toBe('📖 阅读了 /x/services/app.py · 前 400 行')
  })

  it('Read 带 offset+limit:行范围', () => {
    const c = call('1', '调用 Read [Read]\n{"file_path": "/x/app.py", "limit": 500, "offset": 1}')
    expect(summarizeCliToolCall(c)).toBe('📖 阅读了 /x/app.py · 前 500 行')
    const c2 = call('2', '调用 Read [Read]\n{"file_path": "/x/app.py", "limit": 100, "offset": 201}')
    expect(summarizeCliToolCall(c2)).toBe('📖 阅读了 /x/app.py · 第 201~301 行')
  })

  it('Grep:pattern + path', () => {
    // detail 由后端 json.dumps 生成:正则里的 \ 在 JSON 文本中是 \\(双反斜杠才是合法转义)
    const c = call('1', '调用 Grep [Grep]\n{"output_mode": "content", "path": "/home/user/repos/proj/services", "pattern": "subprocess\\\\.run"}')
    expect(summarizeCliToolCall(c)).toBe('🔍 搜索 subprocess\\.run · services')
  })

  it('Glob:pattern', () => {
    const c = call('1', '调用 Glob [Glob]\n{"path": "/home/user/repos/proj", "pattern": "tests/**/*"}')
    expect(summarizeCliToolCall(c)).toBe('🔍 查找文件 tests/**/*')
  })

  it('无 detail(旧数据)时返回 null 回退折叠', () => {
    expect(summarizeCliToolCall(call('1', '调用 Read [Read]'))).toBeNull()
  })

  it('配对成 compact 段并生成摘要', () => {
    const items = [
      call('c1', '调用 Read [Read]\n{"file_path": "/home/user/repos/proj/README.md"}'),
      result('r1', '# README 内容...'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(1)
    expect(segs[0].kind).toBe('compact')
    if (segs[0].kind === 'compact') {
      expect(buildToolSummary(segs[0].call, segs[0].result)).toBe('📖 阅读了 README.md')
    }
  })

  it('Read result 未到达:摘要 + 省略号', () => {
    const c = call('1', '调用 Read [Read]\n{"file_path": "a.py"}')
    expect(buildToolSummary(c, null)).toBe('📖 阅读了 a.py ...')
  })
})

describe('Bash/run_command 紧凑化集成', () => {
  function bashCall(cmd: string): ToolItem {
    return { id: 'b1', type: 'tool_call', content: `执行: ${cmd.slice(0, 40)} [Bash]\n${cmd}` }
  }

  it('只读 Bash 命令配对成 compact 段并生成摘要', () => {
    const items = [
      bashCall("/bin/bash -lc 'cat /home/user/repos/proj/app.py'"),
      result('r1', 'def main(): ...'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(1)
    expect(segs[0].kind).toBe('compact')
    if (segs[0].kind === 'compact') {
      expect(buildToolSummary(segs[0].call, segs[0].result)).toBe('📖 阅读了 app.py')
    }
  })

  it('危险 Bash 命令保持 plain', () => {
    const items = [
      bashCall('rm -rf build/'),
      result('r1', 'done'),
    ]
    const segs = buildToolSegments(items)
    expect(segs).toHaveLength(1)
    expect(segs[0].kind).toBe('plain')
  })

  it('Bash result 未到达:摘要 + 省略号', () => {
    const c = bashCall("/bin/bash -lc 'ls /home/user/repos/proj/src'")
    expect(buildToolSummary(c, null)).toBe('📂 查看目录 src ...')
  })

  it('run_command 只读命令也紧凑化', () => {
    const c = call('1', '执行命令: cat app.py [run_command]\n{"command": "cat app.py"}')
    expect(buildToolSummary(c, result('r', 'content'))).toBe('📖 阅读了 app.py')
  })
})
