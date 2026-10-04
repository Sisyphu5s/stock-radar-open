/** 信号触发时间分级：今日 / 近3日 / 近7日 / 更早 */
import type { SignalCatalogItem, SignalEvent } from '../api/client'
import { SIGNAL_CATEGORY_COLOR } from '../theme/tokens'

/* 信号类别色 / 板块色已随 T-36 迁入 src/theme/tokens.ts（设计令牌单一事实源），
   此处 re-export 保持既有 import 路径不变；signalColor/sectorColor 出口见 tokens.ts。 */
export { SIGNAL_CATEGORY_COLOR, SECTOR_COLOR, signalColor, sectorColor } from '../theme/tokens'

export type SignalAge = '今日' | '近3日' | '近7日' | '更早'

const DAY = 24 * 3600 * 1000

/**
 * 信号触发时间分级：今日 / 近3日 / 近7日 / 更早。
 * naive ISO 按市场时区（Asia/Shanghai, UTC+8）字符串直解为绝对时刻后与 now 比较，
 * 不依赖浏览器本地时区（无时区串被 new Date 按本地时区解析，跨时区会偏）。
 */
export function groupOf(t?: string | null, now = Date.now()): SignalAge {
  if (!t) return '更早'
  let ms = 0
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/.exec(t)
  if (m) {
    const [, y, mo, d, h, mi, s] = m
    ms = Date.UTC(+y, +mo - 1, +d, +h - 8, +mi, +(s ?? 0)) // naive 字段 → UTC+8 绝对时刻
  }
  const diff = now - ms
  if (diff < DAY) return '今日'
  if (diff < 3 * DAY) return '近3日'
  if (diff < 7 * DAY) return '近7日'
  return '更早'
}

export function ageTagColor(age: SignalAge): string {
  switch (age) {
    case '今日': return 'red'
    case '近3日': return 'orange'
    case '近7日': return 'blue'
    default: return 'default'
  }
}

/** catalog 条目 → { code: { text, color } }，驱动标签/徽标展示 */
export function signalLabelMap(items: SignalCatalogItem[]): Record<string, { text: string; color: string }> {
  const m: Record<string, { text: string; color: string }> = {}
  for (const it of items) {
    m[it.code] = { text: it.name, color: SIGNAL_CATEGORY_COLOR[it.category] ?? 'default' }
  }
  return m
}

/** 按股票合并事件的公共分组结果（SignalCenter 的 StockGroup 与 WatchlistPage 的 WlGroup 共用核心） */
export interface GroupedSignals {
  code: string
  name: string
  events: SignalEvent[]
  signals: string[]
}

/**
 * 事件流按股票合并（同一股票多模板信号合并为一行）：
 * 按 code 分组、去重 signals。返回 Map（保留事件原始顺序）。
 * 各消费方在此基础上补充自己的展示字段（best/latest/triggered_at、sector/latestAt 等）。
 */
export function groupSignalEvents(events: readonly SignalEvent[]): Map<string, GroupedSignals> {
  const m = new Map<string, GroupedSignals>()
  mergeSignalEvents(m, events)
  return m
}

/**
 * 增量合并（P2-74）：把事件并入既有分组 Map（groupSignalEvents 的增量形态）。
 * 无限滚动跨批累积场景用：每批只并入新事件，避免对全量累积集合每次重分组（O(N²)）。
 * 语义与 groupSignalEvents 完全一致（分组/去重 signals/保序），后者为其整建形态。
 */
export function mergeSignalEvents(target: Map<string, GroupedSignals>, events: readonly SignalEvent[]): void {
  for (const e of events) {
    let g = target.get(e.stock_code)
    if (!g) {
      g = { code: e.stock_code, name: e.stock_name ?? '', events: [], signals: [] }
      target.set(e.stock_code, g)
    }
    g.events.push(e)
    for (const s of e.signals ?? []) if (!g.signals.includes(s)) g.signals.push(s)
  }
}

/** 共振得分配色:≥high 涨色 / ≥mid 警告色 / 其余强调色。阈值默认 80/50。 */
export function scoreColorScheme(score: number, high = 80, mid = 50): { bg: string; fg: string } {
  const bg = score >= high ? 'var(--sr-up)' : score >= mid ? 'var(--sr-warning)' : 'var(--sr-accent)'
  return { bg, fg: 'var(--sr-on-accent)' }
}
