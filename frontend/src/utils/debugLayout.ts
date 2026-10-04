/**
 * Debug 布局检测（薄封装）：模式状态、事件广播与 visual 模式启动入口。
 * 几何/检测纯逻辑在 utils/debug/debugCore.ts（rectsIntersect / isFlushable / runChecks 等），
 * 本文件只保留：localStorage 状态（新键 sr-debug-mode，旧键 sr-debug 兼容迁移）、
 * URL ?sr_debug=1 临时点亮、事件广播（sr-debug-mode / sr-debug-changed / sr-debug-count）与
 * visual 模式样式注入 + MutationObserver 贴边标记。
 *
 * 入口：MainLayout 在 debug 模式下启动（Ctrl+Shift+D 或 URL ?sr_debug=1 / localStorage sr-debug-mode）。
 * headless 模式不注入事件监听/不渲染，只跑 runHeadlessChecks（由 fe-dbg-mount 决定调用时机）。
 */
import { FLUSH_CLS, isFlushable, runChecks } from './debug/debugCore'
import type { DebugMode, DebugReport } from './debug/debugTypes'

const MODE_KEY = 'sr-debug-mode'
const LEGACY_KEY = 'sr-debug'
const STYLE_ID = 'sr-debug-style'

/** 从字符串中移除 sr_debug 参数（hash 或 search），其余参数保留 */
function stripDebugParam(s: string): string {
  const qi = s.indexOf('?')
  if (qi < 0) return s
  const params = s.slice(qi + 1).split('&').filter((p) => p !== 'sr_debug=1' && !p.startsWith('sr_debug='))
  const base = s.slice(0, qi)
  return params.length ? `${base}?${params.join('&')}` : base
}

/** 当前 Debug 模式：URL ?sr_debug=1 优先（临时点亮，不持久化）；
 *  旧键 sr-debug=1 首次读取时迁移为 visual 并广播（一次性副作用）。 */
export function getDebugMode(): DebugMode {
  if (typeof location !== 'undefined') {
    const query = location.search || location.hash
    if (/(?:^|[?&])sr_debug=1(?:$|&)/.test(query)) return 'visual'
  }
  try {
    if (typeof localStorage !== 'undefined') {
      const v = localStorage.getItem(MODE_KEY)
      if (v === 'visual' || v === 'headless') return v
      // 旧键迁移：sr-debug=1 → visual，写回新键并清旧键后广播
      if (localStorage.getItem(LEGACY_KEY) === '1') {
        localStorage.setItem(MODE_KEY, 'visual')
        localStorage.setItem(LEGACY_KEY, '0')
        if (typeof window !== 'undefined') {
          window.dispatchEvent(new CustomEvent('sr-debug-mode', { detail: { mode: 'visual' } }))
          window.dispatchEvent(new CustomEvent('sr-debug-changed', { detail: { on: true } }))
        }
        return 'visual'
      }
    }
  } catch { /* localStorage 不可用时忽略 */ }
  return 'off'
}

/** 设置 Debug 模式（持久化 + 广播 sr-debug-mode；兼容广播 sr-debug-changed 供旧面板刷新） */
export function setDebugMode(m: DebugMode): void {
  try { localStorage.setItem(MODE_KEY, m) } catch { /* ignore */ }
  // 关闭时同时移除 URL 中的 sr_debug=1：isDebugMode 为 OR 语义，
  // 若不清理 URL，面板会立即被 URL 参数重新点亮
  if (m === 'off' && typeof history !== 'undefined' && typeof location !== 'undefined') {
    const target = location.hash || location.search
    if (/(?:^|[?&])sr_debug=1(?:$|&)/.test(target)) {
      try {
        history.replaceState(null, '', stripDebugParam(location.hash))
      } catch { /* ignore */ }
    }
  }
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('sr-debug-mode', { detail: { mode: m } }))
    window.dispatchEvent(new CustomEvent('sr-debug-changed', { detail: { on: m !== 'off' } }))
  }
}

/** 是否为 debug 模式（off 之外均视为开启） */
export function isDebugMode(): boolean {
  return getDebugMode() !== 'off'
}

/** 切换 debug 模式（off ↔ visual，保持与旧 Ctrl+Shift+D 语义一致） */
export function toggleDebug(): void {
  const next: DebugMode = getDebugMode() === 'off' ? 'visual' : 'off'
  setDebugMode(next)
}

/**
 * 启动调试布局检测（visual 模式）：注入 outline 样式 + MutationObserver 对 body 子树做边缘检测。
 * headless 模式拒绝启动（返回空清理函数，不注入任何副作用）。
 * 返回清理函数（移除样式、断开 observer）。
 */
export function startDebugLayout(): () => void {
  if (typeof document === 'undefined' || typeof MutationObserver === 'undefined') return () => {}
  if (getDebugMode() === 'headless') return () => {}

  // 1. 注入 outline 样式（全局 1px 弱 outline + 贴边元素 2px 红色强调）
  let styleEl = document.getElementById(STYLE_ID) as HTMLStyleElement | null
  if (!styleEl) {
    styleEl = document.createElement('style')
    styleEl.id = STYLE_ID
    styleEl.textContent = [
      '* { outline: 1px solid rgba(255,80,80,0.35) !important; outline-offset: -1px; }',
      `.${FLUSH_CLS} { outline: 2px solid #ff3b30 !important; background: rgba(255,59,48,0.06) !important; }`,
    ].join('\n')
    document.head.appendChild(styleEl)
  }

  let count = 0
  const dispatchCount = () => {
    // 权威计数：以 DOM 为准（运行中增量可能因异步批量而漂移）
    const n = document.querySelectorAll(`.${FLUSH_CLS}`).length
    count = n
    window.dispatchEvent(new CustomEvent('sr-debug-count', { detail: { count: n } }))
  }
  const mark = (el: HTMLElement): boolean => {
    if (!isFlushable(el)) return false
    el.classList.add(FLUSH_CLS)
    count++
    return true
  }
  const unmark = (el: Element) => {
    if (el.classList.contains(FLUSH_CLS)) {
      el.classList.remove(FLUSH_CLS)
      count = Math.max(0, count - 1)
    }
  }

  // 2. MutationObserver：新增/变化/移除节点边缘检测（不跳过任何子树）
  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (m.type === 'childList') {
        for (const node of m.addedNodes) {
          if (node instanceof HTMLElement) {
            mark(node)
            node.querySelectorAll<HTMLElement>('*').forEach(mark)
          }
        }
        for (const node of m.removedNodes) {
          if (node instanceof Element) {
            unmark(node)
            node.querySelectorAll<HTMLElement>(`.${FLUSH_CLS}`).forEach(unmark)
          }
        }
      } else if (m.type === 'attributes' && m.target instanceof HTMLElement) {
        mark(m.target)
      }
    }
    dispatchCount()
  })
  observer.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['class', 'style'],
  })

  // 3. 初始全量扫描
  document.body.querySelectorAll<HTMLElement>('*').forEach((el) => { mark(el) })
  dispatchCount()

  return () => {
    observer.disconnect()
    document.getElementById(STYLE_ID)?.remove()
    // 清除标记类，关闭后完全还原
    document.querySelectorAll<HTMLElement>(`.${FLUSH_CLS}`).forEach((el) => el.classList.remove(FLUSH_CLS))
  }
}

/** headless 检查：runChecks + console 分组输出（collisions/overflows/breakpointCrosses），返回报告 */
export function runHeadlessChecks(): DebugReport {
  const report = runChecks()
  if (typeof console !== 'undefined') {
    console.group('[sr-debug] headless 检查')
    console.log(`flushCount=${report.flushCount} positioned=${report.positioned.length}`)
    if (report.collisions.length > 0) {
      console.log(`collisions: ${report.collisions.length}`)
      console.table(report.collisions.map((c) => ({
        a: c.a, b: c.b,
        overlap: `${c.overlap.width.toFixed(1)}x${c.overlap.height.toFixed(1)}`,
        areaPct: c.areaPct,
      })))
    } else {
      console.log('collisions: 0')
    }
    if (report.overflows.length > 0) {
      console.log(`overflows: ${report.overflows.length}`)
      console.table(report.overflows.map((o) => ({ el: o.el, dir: o.dir, px: o.px })))
    } else {
      console.log('overflows: 0')
    }
    if (report.breakpointCrosses.length > 0) {
      console.log(`breakpointCrosses: ${report.breakpointCrosses.length}`)
      console.table(report.breakpointCrosses.map((b) => ({ el: b.el, bp: b.bp })))
    } else {
      console.log('breakpointCrosses: 0')
    }
    console.groupEnd()
  }
  return report
}
