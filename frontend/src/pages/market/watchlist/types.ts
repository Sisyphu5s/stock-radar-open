import type { SignalEvent, WatchlistItem } from '../../../api/client'

/**
 * watchlist 行统一类型：直接使用 client.ts 的 WatchlistItem 契约
 * （已含后端可选下发的行情/板块字段 last_price/pct_change/industry/updated_at；
 * 历史兜底交叉类型已删除，不再本地重定义）。
 */
export type WlWatchlistItem = WatchlistItem

export interface WlGroup {
  code: string
  name: string
  sector?: string
  events: SignalEvent[]
  signals: string[]
  latestAt: string | null
}

export interface WlRow {
  item: WatchlistItem
  group?: WlGroup
}

export type WlFilter = 'all' | 'signal' | 'silent'

export const WL_SEGMENTS: { key: WlFilter; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'signal', label: '有信号' },
  { key: 'silent', label: '静默' },
]
