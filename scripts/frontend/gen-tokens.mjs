#!/usr/bin/env node
/**
 * gen-tokens.mjs — 从 src/theme/tokens.ts(唯一事实源)生成 src/theme/tokens.css
 * ------------------------------------------------------------
 * 产物三块:
 *   :root                      亮色语义 + 静态(间距/圆角/动效/尺寸/字号/布局/阴影)
 *   body[data-theme='dark']    暗色语义覆盖(仅重声明与亮色不同的键 + 主题依赖派生)
 *   body[data-density='compact'] 紧凑密度覆盖(DESIGN §7:pad 整表缩放 / radius 降档 / 字号/控件/图表高)
 * 键名与既有 --sr-* 完全一致(历史消费方零改动)。
 * 附带 WCAG AA 对比度断言(正文 4.5:1、大号 3:1),不达标仅警告不阻断。
 *
 * 用法:cd frontend && node ../scripts/frontend/gen-tokens.mjs
 * 依赖:node >= 23.6(原生 TS 剥离,直接 import .ts)
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import * as tokens from '../../frontend/src/theme/tokens.ts'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'frontend')
const OUT = join(ROOT, 'src/theme/tokens.css')

/* ---------------- 语义键 → CSS 变量名 ---------------- */
const SEMANTIC_CSS = {
  bg: '--sr-bg', cardBg: '--sr-card-bg', blockBg: '--sr-block-bg', siderBg: '--sr-sider-bg',
  text1: '--sr-text-1', text2: '--sr-text-2', text3: '--sr-text-3',
  border: '--sr-border', accent: '--sr-accent', accentDeep: '--sr-accent-deep',
  onAccent: '--sr-on-accent', warning: '--sr-warning', success: '--sr-success',
  error: '--sr-error', up: '--sr-up', down: '--sr-down', star: '--sr-star',
  hoverBg: '--sr-hover-bg', chartGrid: '--sr-chart-grid',
  overlay: '--sr-overlay', overlaySoft: '--sr-overlay-soft',
  surface0: '--sr-surface-0', surface1: '--sr-surface-1',
  surface2: '--sr-surface-2', surface3: '--sr-surface-3',
}

const lines = []
const push = (s = '') => lines.push(s)

/* ---------------- 亮色 :root ---------------- */
push('/* ============================================================')
push('   tokens.css — 由 scripts/frontend/gen-tokens.mjs 自动生成,禁止手改')
push('   唯一事实源:src/theme/tokens.ts;改值后重新运行生成脚本')
push('   结构:亮色 :root + body[data-theme="dark"] + body[data-density="compact"]')
push('   ============================================================ */')
push('')
push(':root {')
push('  /* 语义色(亮色) */')
for (const key of Object.keys(SEMANTIC_CSS)) {
  push(`  ${SEMANTIC_CSS[key]}: ${tokens.semantic[key].light};`)
}
push('')
push('  /* 投影(亮色) */')
push(`  --sr-card-shadow: ${tokens.shadow.card.light};`)
push(`  --sr-card-hover-shadow: ${tokens.shadow.cardHover.light};`)
push(`  --sr-float-shadow: ${tokens.shadow.float.light};`)
push('')
push('  /* 圆角 token(--sr-radius-card 为主键,--sr-card-radius 为兼容别名) */')
push(`  --sr-radius-card: ${tokens.radius.card};`)
push(`  --sr-radius-ctl: ${tokens.radius.ctl};`)
push(`  --sr-radius-tag: ${tokens.radius.tag};`)
push(`  --sr-card-radius: ${tokens.derived.radiusCard};`)
push('')
push('  /* 动效时长令牌:全站 transition 收敛两档 */')
push(`  --sr-dur-fast: ${tokens.dur.fast};`)
push(`  --sr-dur-norm: ${tokens.dur.norm};`)
push('')
push('  /* 控件与字号(固定档位,不随容器缩放) */')
push(`  --sr-ctl-h: ${tokens.ctlH.comfort};`)
push(`  --sr-font-page: ${tokens.fontScale.page}; --sr-font-head: ${tokens.fontScale.head}; --sr-font-title: ${tokens.fontScale.title};`)
push(`  --sr-font-sm: ${tokens.fontScale.sm}; --sr-font-xs: ${tokens.fontScale.xs}; --sr-font-meta: ${tokens.fontScale.meta};`)
push(`  --sr-font-stat: ${tokens.fontScale.stat}; --sr-font-display: ${tokens.fontScale.display};`)
push('')
push('  /* 间距 token(语义) */')
push(`  --sr-gap-page: ${tokens.gap.page};   /* 页面级间距 */`)
push(`  --sr-gap-card: ${tokens.gap.card};   /* 卡片内/卡片间间距 */`)
push(`  --sr-gap-row: ${tokens.gap.row};     /* 行级间距 */`)
push(`  --sr-gap-ctl: ${tokens.gap.ctl};     /* 控件间间距 */`)
push('')
push('  /* 间距刻度(设计令牌,单一事实源):所有 UI 内边距/间距一律引用 --sr-pad-* */')
push(`  --sr-pad-xs: ${tokens.pad.xs};`)
push(`  --sr-pad-sm: ${tokens.pad.sm};`)
push(`  --sr-pad-md: ${tokens.pad.md};`)
push(`  --sr-pad-lg: ${tokens.pad.lg};`)
push(`  --sr-pad-xl: ${tokens.pad.xl};`)
push(`  --sr-pad-2xl: ${tokens.pad['2xl']};`)
push(`  --sr-pad-3xl: ${tokens.pad['3xl']};`)
push('')
push('  /* 卡片体内边距别名:指向间距刻度 */')
push(`  --sr-card-pad: ${tokens.derived.cardPad};`)
push('')
push('  /* 图表标准高度 */')
push(`  --sr-chart-h-sm: ${tokens.chartH.sm}; --sr-chart-h-md: ${tokens.chartH.md}; --sr-chart-h-lg: ${tokens.chartH.lg};`)
push('')
push('  /* 布局:顶栏高 / 内容区 padding / 满高页可用高度 */')
push(`  --sr-header-h: ${tokens.layout.headerH};`)
push(`  --sr-content-pad: ${tokens.layout.contentPad};`)
push(`  --sr-sider-w: ${tokens.layout.siderW};          /* 侧栏展开宽 */`)
push(`  --sr-nav-drawer-w: ${tokens.layout.navDrawerW}; /* 导航抽屉宽 */`)
push(`  --sr-msgrid-w: ${tokens.layout.msgridW};        /* 多选下拉网格宽 */`)
push(`  --sr-bottombar-h: ${tokens.layout.bottombarH};`)
push(`  --sr-page-fill-h: ${tokens.derived.pageFillH};`)
push('')
push('  /* 安全区 */')
push(`  --sr-safe-top: ${tokens.safeArea.top};`)
push(`  --sr-safe-bottom: ${tokens.safeArea.bottom};`)
push(`  --sr-safe-left: ${tokens.safeArea.left};`)
push(`  --sr-safe-right: ${tokens.safeArea.right};`)
push('')
push('  /* 斑马纹底色(长表格偶数行):跟随 --sr-block-bg,var() 在声明处解析,故暗色需重声明 */')
push(`  --sr-zebra-bg: ${tokens.derived.zebraBg};`)
push('}')
push('')

/* ---------------- 暗色覆盖 ---------------- */
push("body[data-theme='dark'] {")
push('  /* 语义色(暗色) */')
for (const key of Object.keys(SEMANTIC_CSS)) {
  push(`  ${SEMANTIC_CSS[key]}: ${tokens.semantic[key].dark};`)
}
push('')
push('  /* 投影(暗色:仅 hover 阴影与亮色不同) */')
push(`  --sr-card-hover-shadow: ${tokens.shadow.cardHover.dark};`)
push('')
push('  /* 暗色斑马纹重声明(var() 在声明处解析,随主题域重声明才取到暗色 --sr-block-bg) */')
push(`  --sr-zebra-bg: ${tokens.derived.zebraBg};`)
push('}')
push('')

/* ---------------- 紧凑密度覆盖 ---------------- */
push("body[data-density='compact'] {")
push('  /* 间距刻度整表缩放(2/4/6/8/10/12/16) */')
push(`  --sr-pad-xs: ${tokens.padCompact.xs}; --sr-pad-sm: ${tokens.padCompact.sm}; --sr-pad-md: ${tokens.padCompact.md};`)
push(`  --sr-pad-lg: ${tokens.padCompact.lg}; --sr-pad-xl: ${tokens.padCompact.xl}; --sr-pad-2xl: ${tokens.padCompact['2xl']}; --sr-pad-3xl: ${tokens.padCompact['3xl']};`)
push('  /* 圆角降档(6/4/4) */')
push(`  --sr-radius-card: ${tokens.radiusCompact.card}; --sr-radius-ctl: ${tokens.radiusCompact.ctl}; --sr-radius-tag: ${tokens.radiusCompact.tag};`)
push('  /* 语义 gap 收紧 */')
push(`  --sr-gap-page: ${tokens.gapCompact.page}; --sr-gap-card: ${tokens.gapCompact.card}; --sr-gap-row: ${tokens.gapCompact.row}; --sr-gap-ctl: ${tokens.gapCompact.ctl};`)
  push(`  --sr-ctl-h: ${tokens.ctlH.compact};`)
  push(`  --sr-font-page: ${tokens.fontScaleCompact.page}; --sr-font-head: ${tokens.fontScaleCompact.head}; --sr-font-title: ${tokens.fontScaleCompact.title};`)
  push(`  --sr-font-sm: ${tokens.fontScaleCompact.sm}; --sr-font-xs: ${tokens.fontScaleCompact.xs}; --sr-font-meta: ${tokens.fontScaleCompact.meta};`)
  push(`  --sr-font-stat: ${tokens.fontScaleCompact.stat}; --sr-font-display: ${tokens.fontScaleCompact.display};`)
push(`  --sr-card-pad: ${tokens.derived.cardPadCompact};`)
push(`  --sr-chart-h-sm: ${tokens.chartHCompact.sm}; --sr-chart-h-md: ${tokens.chartHCompact.md}; --sr-chart-h-lg: ${tokens.chartHCompact.lg};`)
push('}')
push('')

writeFileSync(OUT, lines.join('\n'))
console.log(`[gen-tokens] wrote ${OUT}`)

/* ================= WCAG AA 对比度断言(警告不阻断) ================= */
const hex = (n) => {
  let h = n.slice(1)
  if (h.length === 3) h = [...h].map((c) => c + c).join('')
  if (h.length !== 6) return null
  return { r: parseInt(h.slice(0, 2), 16), g: parseInt(h.slice(2, 4), 16), b: parseInt(h.slice(4, 6), 16), a: 1 }
}
const parseColor = (c) => {
  c = String(c).trim()
  if (c.startsWith('#')) return hex(c)
  const m = /^rgba?\(([^)]+)\)$/.exec(c)
  if (m) {
    const p = m[1].split(',').map((s) => s.trim())
    const r = parseFloat(p[0]); const g = parseFloat(p[1]); const b = parseFloat(p[2])
    const a = p[3] !== undefined ? parseFloat(p[3]) : 1
    if ([r, g, b, a].some(Number.isNaN)) return null
    return { r, g, b, a }
  }
  return null
}
const lum = (c) => {
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b)
}
const contrast = (fg, bg) => {
  const l1 = lum(fg); const l2 = lum(bg)
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
}

/** 断言对:前景语义键 × 背景语义键;阈值 4.5(正文) / 3(大号/图形)
 *  注意:accentDeep 实底的实际消费方(ChatPanel)亮色配 onAccent、暗色配 text1,
 *  故 accentDeep 按主题分别断言,不混入通用集合。 */
const PAIRS = [
  { fg: ['text1', 'text2', 'text3'], bg: ['bg', 'cardBg', 'blockBg', 'siderBg'], min: 4.5, note: '正文' },
  { fg: ['onAccent'], bg: ['accent', 'up', 'down', 'warning', 'success', 'error'], min: 4.5, note: '实底前景' },
  { fg: ['onAccent'], bg: ['accentDeep'], theme: 'light', min: 4.5, note: 'accentDeep 实底(亮色配 onAccent)' },
  { fg: ['text1'], bg: ['accentDeep'], theme: 'dark', min: 4.5, note: 'accentDeep 实底(暗色配 text1)' },
  { fg: ['accent', 'warning', 'success', 'error', 'up', 'down', 'star'], bg: ['bg', 'cardBg'], min: 3, note: '文字/图形' },
]

const issues = []
for (const theme of ['light', 'dark']) {
  for (const pair of PAIRS) {
    if (pair.theme && pair.theme !== theme) continue
    for (const fk of pair.fg) {
      const fgC = parseColor(tokens.semantic[fk][theme])
      if (!fgC || fgC.a < 1) continue // 半透明前景不参与(无法与不透明底精确计算)
      for (const bk of pair.bg) {
        const bgC = parseColor(tokens.semantic[bk][theme])
        if (!bgC || bgC.a < 1) continue
        const ratio = contrast(fgC, bgC)
        if (ratio < pair.min) {
          issues.push({ theme, fk, bk, ratio, min: pair.min, note: pair.note })
        }
      }
    }
  }
}

if (issues.length === 0) {
  console.log('[gen-tokens] 对比度断言:全部语义色对达标(WCAG AA)')
} else {
  console.warn(`[gen-tokens] 对比度断言:${issues.length} 对不达标(警告,不阻断):`)
  for (const i of issues) {
    console.warn(`  [${i.theme}] --sr-${i.fk} vs --sr-${i.bk} = ${i.ratio.toFixed(2)}:1 < ${i.min}:1 (${i.note})`)
  }
}
