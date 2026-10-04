/**
 * Splitter 尺寸持久化共享工具（单一事实源，SignalCenter / StockWorkbench 共用）。
 *
 * 机制（2026-08-15 定，见 DESIGN/10 M1 分栏纪律）：
 * - 持久化值只存「首 pane 比例」（left ∈ [0,100]），right = 100 - left，恒 sum=100——
 *   杜绝双侧独立钳制导致的 sum≠100 比例失真。
 * - 加载时按双 pane 约束做单变量钳制：left ∈ [max(min₁, 100−max₂), min(max₁, 100−min₂)]，
 *   越界值（旧版本残留/手改 localStorage）钳回合法域，非法结构回退默认。
 *   （Mantine useSplitter 对初始 sizes 不做 min/max 钳制，约束仅在拖拽交互时生效——
 *   加载钳制是唯一防线，缺失会渲染出越界 pane，如 K线 90%、副图 10%。）
 */

/** 双 pane 百分比约束（%）：pane1 ∈ [min, max]，pane2 ∈ [min2, max2]（max2 缺省不设上限） */
export interface SplitConstraints {
  min: number
  max: number
  min2: number
  /** pane2 上限（%），缺省 = 100 − min（与 min1 对称的默认上界） */
  max2?: number
}

/** 按约束钳制首 pane 比例（%），并推导右 pane = 100 − left（恒 sum=100） */
export function clampSplitLeft(left: number, c: SplitConstraints): number {
  const max2 = c.max2 ?? 100 - c.min
  const lo = Math.max(c.min, 100 - max2)
  const hi = Math.min(c.max, 100 - c.min2)
  // 约束互斥（lo > hi，如 min 写错）时取 lo，宁可保 min 也不越界
  return Math.min(Math.max(left, lo), Math.max(lo, hi))
}

/** 读取持久化比例：结构合法则钳制，否则回退默认 */
export function loadSplitSizes(key: string, c: SplitConstraints, fallback: [number, number]): [number, number] {
  try {
    const raw = localStorage.getItem(key)
    if (raw) {
      const v = JSON.parse(raw) as unknown
      if (Array.isArray(v) && v.length >= 1) {
        const left = v[0]
        if (typeof left === 'number' && Number.isFinite(left)) {
          const clamped = clampSplitLeft(left, c)
          return [clamped, 100 - clamped]
        }
      }
    }
  } catch { /* localStorage 不可用/损坏时回退默认 */ }
  return fallback
}

/** 持久化比例（只存首 pane，right 恒 = 100 − left，sum=100 保证） */
export function persistSplitSizes(key: string, sizes: readonly number[]): void {
  try {
    const left = sizes[0]
    if (typeof left === 'number' && Number.isFinite(left)) {
      localStorage.setItem(key, JSON.stringify([left]))
    }
  } catch { /* ignore */ }
}

/**
 * Mantine SplitterPaneSize（number | '45%' | '320px' …）→ 数字。
 * 防御：仅接受 number 与 '%' 结尾串；px/rem 等非百分比单位（本应用不出现）按 0 处理，
 * 避免 parseFloat('320px')=320 被当作 % 持久化污染比例。
 */
export function toSplitNumbers(sizes: readonly (number | string)[]): number[] {
  return sizes.map((s) => {
    if (typeof s === 'number') return Number.isFinite(s) ? s : 0
    const m = /^(-?[\d.]+)%$/.exec(s)
    return m ? parseFloat(m[1]) : 0
  })
}
