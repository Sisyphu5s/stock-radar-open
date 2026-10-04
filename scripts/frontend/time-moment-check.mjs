#!/usr/bin/env node
/**
 * 周期语义状态机 resolveSignalMoment 边界断言脚本
 *
 * 环境说明：本仓使用 rolldown-vite，node_modules 内**无 esbuild**（任务卡原假设 esbuild 为项目依赖不成立）。
 * 实测替代：用项目自带的 tsc 将 time.ts + periods.ts 编译为 CommonJS 到 os.tmpdir() 临时目录，
 * require 加载后断言（time.js 内 require('./periods') 与编译产物同目录，可解析）。
 * 执行：node scripts/time-moment-check.mjs（脚本自管编译，无需前置命令）
 */
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'frontend')
const tscBin = path.join(frontendRoot, 'node_modules', '.bin', 'tsc')

// 编译 time.ts + periods.ts（--ignoreConfig：避免 TS5112「tsconfig 存在但命令行指定文件时不加载」）
const outDir = mkdtempSync(path.join(tmpdir(), 'sr-time-'))
execFileSync(tscBin, [
  'src/utils/time.ts', 'src/utils/periods.ts',
  '--ignoreConfig', '--outDir', outDir,
  '--module', 'commonjs', '--target', 'es2022', '--skipLibCheck',
], { cwd: frontendRoot, stdio: 'pipe' })

const require = createRequire(import.meta.url)
const { resolveSignalMoment, formatShanghaiFullTime } = require(path.join(outDir, 'time.js'))

// ===== 固定时钟（上海时区口径） =====
// 2026-08-12 是周三；naive 字符串按 UTC+8 解析 → Date.UTC(y, m, d, h-8, mi)
const NOW_1430 = Date.UTC(2026, 7, 12, 6, 30) // 上海 2026-08-12 14:30
const NOW_1530 = Date.UTC(2026, 7, 12, 7, 30) // 上海 2026-08-12 15:30（收盘后）
const NOW_1000 = Date.UTC(2026, 7, 12, 2, 0)  // 上海 2026-08-12 10:00
const NOW_1600 = Date.UTC(2026, 7, 12, 8, 0)  // 上海 2026-08-12 16:00（收盘后）

/** 断言用例：args=[period, t, asOf, nowMs]，expect 字段逐一对比（titleHas=子串包含） */
const cases = [
  {
    name: '1 分钟盘中（今日 HH:mm 无日期）',
    args: ['60', '2026-08-12T14:25:00', null, NOW_1430],
    expect: { kind: 'intraday', label: '14:25', hint: '盘中', intraday: true },
  },
  {
    name: '2 分钟今日收盘（15:30 后今日仍标今天）',
    args: ['5', '2026-08-12T10:30:00', null, NOW_1530],
    expect: { kind: 'today_closed', label: '10:30', hint: '今天', intraday: false },
  },
  {
    name: '3 分钟昨日（MM-DD HH:mm 带日期）',
    args: ['1', '2026-08-11T14:35:00', null, NOW_1530],
    expect: { kind: 'yesterday', label: '08-11 14:35', hint: '昨天' },
  },
  {
    name: '4 分钟历史（无 hint）',
    args: ['1', '2026-08-10T14:35:00', null, NOW_1530],
    expect: { kind: 'older', label: '08-10 14:35', hint: null },
  },
  {
    name: '5 分钟跨年（补 YYYY-）',
    args: ['30', '2025-12-30T14:35:00', null, NOW_1530],
    expect: { kind: 'older', label: '2025-12-30 14:35' },
  },
  {
    name: '6 日线盘中（as_of 盘中标记，tooltip 双时间）',
    args: ['daily', '2026-08-12T15:00:00', '2026-08-12T10:24:00', NOW_1430],
    expect: { kind: 'intraday', label: '08-12', hint: '盘中', intraday: true, titleHas: '· bar' },
  },
  {
    name: '7 日线盘中（无 as_of 未来 bar 标签 + 早盘）',
    args: ['daily', '2026-08-12T15:00:00', null, NOW_1000],
    expect: { kind: 'intraday', label: '08-12', hint: '盘中' },
  },
  {
    // 规格 future 路径：tMs > nowMs+5min 且上海当前 ≥15:05。15:00 bar 在 16:00 时钟下已非未来
    // （15:00 < 16:05），故用例 8 用明日 bar 标签构造 future（任务卡原数据与规格逻辑矛盾，已修正）
    name: '8 日线未来异常（收盘后未来 bar 标签 → future）',
    args: ['daily', '2026-08-13T15:00:00', null, NOW_1600],
    expect: { kind: 'future', label: '08-13', hint: null, intraday: false },
  },
  {
    name: '9 日线今日收盘（今天收盘 hint）',
    args: ['daily', '2026-08-12T15:00:00', null, NOW_1530],
    expect: { kind: 'today_closed', label: '08-12', hint: '今天收盘' },
  },
  {
    name: '10 日线昨日',
    args: ['daily', '2026-08-11T15:00:00', null, NOW_1530],
    expect: { kind: 'yesterday', label: '08-11', hint: '昨天' },
  },
  {
    // 08-11 虽是昨天，但周线锚定交易日不产「昨天」词 → older；08-11(周二) 与 08-12(周三) 同周 → 本周
    name: '11 周线本周（昨日但周月不产昨天词 → older + 本周）',
    args: ['weekly', '2026-08-11T15:00:00', null, NOW_1430],
    expect: { kind: 'older', label: '2026-08-11', hint: '本周' },
  },
  {
    name: '12 周线上周（hint null）',
    args: ['weekly', '2026-08-07T15:00:00', null, NOW_1430],
    expect: { kind: 'older', label: '2026-08-07', hint: null },
  },
  {
    name: '13 月线本月',
    args: ['monthly', '2026-08-03T15:00:00', null, NOW_1430],
    expect: { kind: 'older', label: '2026-08-03', hint: '本月' },
  },
  {
    name: '14 月线上月（hint null）',
    args: ['monthly', '2026-07-31T15:00:00', null, NOW_1430],
    expect: { kind: 'older', label: '2026-07-31', hint: null },
  },
  {
    name: '15 兜底（空输入）',
    args: ['daily', null, null, NOW_1430],
    expect: { kind: 'older', label: '—', title: '', sortEpoch: 0, hint: null, intraday: false },
  },
  {
    // 上海 14:35 时钟（Date.UTC(2026,7,12,6,35)）比 14:25 bar 晚 10 分钟 → title 追加「10 分钟前」
    name: '16 title 相对词（10 分钟后 → 10 分钟前）',
    args: ['1', '2026-08-12T14:25:00', null, Date.UTC(2026, 7, 12, 6, 35)],
    expect: { titleHas: '10 分钟前' },
  },
  // ===== 跨年窗口（上海 2026-01-01 01:00 = Date.UTC(2025,11,31,17,0)） =====
  // 覆盖：上海年 vs 浏览器本地年差异（UTC 时区下浏览器年仍为 2025）——补年判断必须用上海年
  {
    name: '17 跨年窗口·昨日分钟（上海 1/1 01:00，bar 12/31 → 补年）',
    args: ['1', '2025-12-31T14:35:00', null, Date.UTC(2025, 11, 31, 17, 0)],
    expect: { kind: 'yesterday', label: '2025-12-31 14:35', hint: '昨天' },
  },
  {
    name: '18 跨年窗口·昨日日线（补年）',
    args: ['daily', '2025-12-31T15:00:00', null, Date.UTC(2025, 11, 31, 17, 0)],
    expect: { kind: 'yesterday', label: '2025-12-31', hint: '昨天' },
  },
  {
    name: '19 跨年窗口·今日盘中（不补年）',
    args: ['daily', '2026-01-01T15:00:00', null, Date.UTC(2025, 11, 31, 17, 0)],
    expect: { kind: 'intraday', label: '01-01', hint: '盘中' },
  },
]

let failed = 0
for (const c of cases) {
  const got = resolveSignalMoment(...c.args)
  const problems = []
  for (const [k, v] of Object.entries(c.expect)) {
    if (k === 'titleHas') {
      if (!got.title.includes(v)) problems.push(`title 不含「${v}」,实际「${got.title}」`)
      continue
    }
    if (got[k] !== v) problems.push(`${k}: 期望 ${JSON.stringify(v)},实际 ${JSON.stringify(got[k])}`)
  }
  if (problems.length) {
    failed++
    console.error(`✗ ${c.name}\n    ${problems.join('\n    ')}`)
  }
}
rmSync(outDir, { recursive: true, force: true })
if (failed) {
  console.error(`time-moment-check: ${cases.length - failed}/${cases.length} 通过，${failed} 失败`)
  process.exit(1)
}
console.log(`time-moment-check: ${cases.length} 用例通过`)

// ===== formatShanghaiFullTime（epoch ms → 上海 naive 串，P2-63 时区收敛） =====
// 固定 Asia/Shanghai：UTC 值 +8h 取 UTC 字段（不依赖浏览器本地时区）
const shCases = [
  { name: '上海 14:30（Date.UTC 06:30）', ms: Date.UTC(2026, 7, 12, 6, 30), opts: undefined, expect: '2026-08-12 14:30:00' },
  { name: 'withYear=false', ms: Date.UTC(2026, 7, 12, 6, 30), opts: { withYear: false }, expect: '08-12 14:30:00' },
  { name: 'withSeconds=false', ms: Date.UTC(2026, 7, 12, 6, 30), opts: { withSeconds: false }, expect: '2026-08-12 14:30' },
  { name: '跨年窗口（上海 2026-01-01 01:00 = UTC 2025-12-31 17:00）', ms: Date.UTC(2025, 11, 31, 17, 0), opts: undefined, expect: '2026-01-01 01:00:00' },
  { name: '午夜前（上海 23:59）', ms: Date.UTC(2026, 7, 11, 15, 59), opts: { withYear: false, withSeconds: false }, expect: '08-11 23:59' },
]
let shFailed = 0
for (const c of shCases) {
  const got = formatShanghaiFullTime(c.ms, c.opts)
  if (got !== c.expect) {
    shFailed++
    console.error(`✗ formatShanghaiFullTime ${c.name}\n    期望 ${JSON.stringify(c.expect)},实际 ${JSON.stringify(got)}`)
  }
}
if (shFailed) {
  console.error(`time-moment-check: formatShanghaiFullTime ${shCases.length - shFailed}/${shCases.length} 失败`)
  process.exit(1)
}
console.log(`time-moment-check: formatShanghaiFullTime ${shCases.length} 用例通过`)
