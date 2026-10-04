import { groupOf } from '../../../utils/signals'
import type { SignalAge } from '../../../utils/signals'
import type { SignalEvent } from '../../../api/client'
import { formatFullTime, toMarketEpochMs } from '../../../utils/time'
import { toNum } from '../../../utils/format'
import type { WlRow, WlWatchlistItem } from './types'

export const NEW_MS = 2 * 3600 * 1000

export interface WlRowView {
  code: string
  name: string
  sector?: string
  hasSig: boolean
  signals: string[]
  age: SignalAge | null
  isNew: boolean
  latestAt: string | null
  /** 精确最新行情时点：优先 watchlist.updated_at（行情快照），无则取最新触发时间 */
  quoteAt: string | null
  /** 信号时点原始字段（最新触发事件透传）：SignalMomentCell 统一双时渲染（主=理论原始，副=扫描发现） */
  period: string
  triggeredAt: string | null
  asOf: string | null
  discoveredAt: string | null
  price: number | null
  pct: number | null
  vol: number | null
  dd: number | null
  spark: { closes: number[]; dates: string[] } | null
}

export type KlineInfo = Record<string, { closes: number[]; dates: string[] } | null>
export type RiskInfo = Record<string, { annual_volatility?: number; max_drawdown?: number }>

/** 数字化容错：number 原样；非空字符串转 number；其余 null（复用 utils/format toNum） */
const numOrNull = (v: number | null): number | null => (v != null && Number.isFinite(v) ? v : null)

/** 精确时点：naive ISO 串 → 'MM-DD HH:mm'（字符串直解，不按浏览器时区解析）；解析失败返回 null */
export function fmtClock(iso: string | null | undefined): string | null {
  if (!iso) return null
  const s = formatFullTime(iso, { withYear: false, withSeconds: false })
  // formatFullTime 对无法解析的串原样返回；保持原 fmtClock 契约（失败 → null）
  return s === '—' || s === iso ? null : s
}

/** 触发列 tooltip 辅助：行情快照时点（信号双时 tooltip 由 SignalMomentCell 内部拼接，extraTip 注入此值） */
export function quoteTip(quoteAt: string | null): string | null {
  const s = fmtClock(quoteAt)
  return s ? `行情 ${s}` : null
}

/** 关注行展示模型：只取实际存在的数据（无信号/无 K 线/无风险 → null，不编造占位）。
 *  行情与板块优先取 watchlist 可选字段（last_price/pct_change/industry），
 *  信号 evidence / 事件板块仅兜底。 */
export function toRowView(r: WlRow, klineInfo: KlineInfo, riskInfo: RiskInfo, now: number): WlRowView {
  const g = r.group
  const wl = r.item as WlWatchlistItem
  const hasSig = !!g && g.signals.length > 0
  const latestAt = g?.latestAt ?? null
  const ev = g?.events[0]?.evidence
  const price = numOrNull(toNum(wl.last_price)) ?? numOrNull(toNum(ev?.price))
  const pct = numOrNull(toNum(wl.pct_change)) ?? numOrNull(toNum(ev?.pct_change))
  const sector = (wl.industry ?? '').trim() || g?.sector || undefined
  const ri = riskInfo[r.item.code]
  // 触发列时点：取最新触发事件（triggered_at 最大者）原始字段透传，
  // 双时渲染统一由 SignalMomentCell 完成（状态机单一事实源在组件内）
  let period = 'daily'
  let triggeredAt: string | null = null
  let asOf: string | null = null
  let discoveredAt: string | null = null
  if (hasSig && g && g.events.length > 0) {
    let latest: SignalEvent | null = null
    for (const e of g.events) {
      if (e.triggered_at && (!latest || toMarketEpochMs(e.triggered_at) > toMarketEpochMs(latest.triggered_at))) latest = e
    }
    if (latest?.triggered_at) {
      period = latest.period || 'daily'
      triggeredAt = latest.triggered_at
      asOf = latest.as_of ?? null
      discoveredAt = latest.scan_discovered_at ?? null
    }
  }
  return {
    code: r.item.code,
    name: r.item.name || r.item.code,
    sector,
    hasSig,
    signals: g?.signals ?? [],
    age: latestAt ? groupOf(latestAt, now) : null,
    isNew: !!latestAt && now - toMarketEpochMs(latestAt) < NEW_MS,
    latestAt,
    quoteAt: wl.updated_at ?? latestAt,
    period,
    triggeredAt,
    asOf,
    discoveredAt,
    price,
    pct,
    vol: ri?.annual_volatility ?? null,
    dd: ri?.max_drawdown ?? null,
    spark: klineInfo[r.item.code] ?? null,
  }
}
