import { useCallback, useEffect, useRef, useState } from 'react'
import { notifications } from '@mantine/notifications'
import { api, getSignalCatalog } from '../api/client'
import type { WatchlistItem } from '../api/client'
import { invalidateWatchlist, useWatchlist } from '../data/market'
import { STORAGE_KEYS } from '../utils/storageKeys'
import { signalLabelMap } from '../utils/signals'
import { toMarketEpochMs } from '../utils/time'
import { guardedInterval } from '../utils/guardedInterval'
import useWebNotifications from './useWebNotifications'

/** 全局关注提醒数据 hook：统一数据源（取代 WatchlistNotificationBridge 的轮询聚合职责）。
 *
 * 30s 轮询 /market/watchlist/notifications 增量拉取（事件 id 游标）：
 * - 首轮立即、无游标时走 bootstrap：只建游标 + 初始化已读水位，绝不弹历史旧事件；
 * - 之后按 after_id 增量拉取，按股票聚合未读事件；
 * - 已读体系为事件 id 水位（sr-wl-seen-id），与游标解耦：markAllSeen 不推进游标
 *   （游标=拉取水位，已读=UI 水位）。
 * 另提供关注列表（复用 useWatchlist）、乐观 toggleWatch、信号标签映射 labelOf。
 */
const POLL_MS = 30 * 1000
const CURSOR_KEY = STORAGE_KEYS.watchlistNotifyCursor
const SEEN_KEY = STORAGE_KEYS.watchlistSeenId
const FETCH_LIMIT = 100
/** 未读事件池上限：超过后淘汰最旧（id 最小），防止长时间不读导致内存无限增长 */
const MAX_PENDING_EVENTS = 200

interface WatchlistNotifyEvent {
  id: number
  stock_code: string
  stock_name: string
  signals: string[]
  status: string
  triggered_at: string | null
  /** 数据截至时刻（naive ISO，可空；聚合仅用 triggered_at，保持现状） */
  as_of: string | null
  /** 扫描发现时刻（T-80 契约；旧数据 null） */
  scan_discovered_at: string | null
  /** 事件周期（1/5/15/30/60/daily/weekly/monthly；旧数据缺失 → 前端兜底 daily） */
  period: string | null
}

interface NotifyResp {
  data: WatchlistNotifyEvent[]
  latest_id: number
  bootstrap: boolean
}

export interface WatchAlertItem {
  code: string
  name: string
  /** 去重合并后的信号列表 */
  signals: string[]
  /** 最新事件 triggered_at 的市场时区绝对时刻（naive ISO → UTC+8，ms） */
  latest: number
  /** 双时点原始字段（最大 id 未读事件透传；SignalMomentCell 统一渲染，主=理论原始、副=扫描发现） */
  period: string
  triggeredAt: string | null
  asOf: string | null
  discoveredAt: string | null
  /** 该股票未读事件最大 id */
  maxId: number
}

export interface WatchlistAlertsApi {
  /** 仅未读，按最新触发时间降序 */
  alerts: WatchAlertItem[]
  unreadCount: number
  watchlistData: WatchlistItem[]
  markAllSeen: () => void
  toggleWatch: (code: string, name?: string) => Promise<void>
  labelOf: (code: string) => { text: string; color: string }
}

function readCursor(): number | null {
  try {
    const raw = localStorage.getItem(CURSOR_KEY)
    if (raw === null) return null
    const n = Number(raw)
    return Number.isFinite(n) && n >= 0 ? Math.floor(n) : null
  } catch {
    return null
  }
}

function writeCursor(id: number) {
  try {
    localStorage.setItem(CURSOR_KEY, String(id))
  } catch {
    /* ignore quota / unavailable storage */
  }
}

function readSeen(): number | null {
  try {
    const raw = localStorage.getItem(SEEN_KEY)
    if (raw === null) return null
    const n = Number(raw)
    return Number.isFinite(n) && n >= 0 ? Math.floor(n) : null
  } catch {
    return null
  }
}

function writeSeen(id: number) {
  try {
    localStorage.setItem(SEEN_KEY, String(id))
  } catch {
    /* ignore quota / unavailable storage */
  }
}

/** 未读事件按股票聚合：signals 并集去重、maxId=max、最新事件字段（按 id 最大）接管 name/latest；按最新触发降序。 */
function aggregate(events: readonly WatchlistNotifyEvent[], seenId: number): WatchAlertItem[] {
  const byStock = new Map<string, WatchAlertItem>()
  for (const e of events) {
    if (e.id <= seenId) continue
    let g = byStock.get(e.stock_code)
    if (!g) {
      byStock.set(e.stock_code, {
        code: e.stock_code,
        name: e.stock_name || e.stock_code,
        signals: [...(e.signals ?? [])],
        latest: toMarketEpochMs(e.triggered_at),
        period: e.period || 'daily',
        triggeredAt: e.triggered_at,
        asOf: e.as_of,
        discoveredAt: e.scan_discovered_at,
        maxId: e.id,
      })
    } else {
      if (e.id > g.maxId) {
        g.maxId = e.id
        g.name = e.stock_name || e.stock_code
        g.latest = toMarketEpochMs(e.triggered_at)
        g.period = e.period || 'daily'
        g.triggeredAt = e.triggered_at
        g.asOf = e.as_of
        g.discoveredAt = e.scan_discovered_at
      }
      for (const s of e.signals ?? []) if (!g.signals.includes(s)) g.signals.push(s)
    }
  }
  return [...byStock.values()].sort((a, b) => b.latest - a.latest)
}

export default function useWatchlistAlerts(): WatchlistAlertsApi {
  // ===== 系统通知:新未读到达且页面不可见时批量通知(内部按已通知水位去重) =====
  const webNotify = useWebNotifications()
  // ref 保持最新:轮询 effect 只挂载一次,enabled/权限在设置页变更后仍能读到最新值
  const webNotifyRef = useRef(webNotify)
  webNotifyRef.current = webNotify

  // ===== 关注列表：useWatchlist 为服务端事实源，本地 state 承载乐观更新 =====
  const freshWatchlist = useWatchlist()
  const [watchlistData, setWatchlistData] = useState<WatchlistItem[]>([])
  useEffect(() => {
    if (freshWatchlist) setWatchlistData(freshWatchlist)
  }, [freshWatchlist])

  // ===== 未读提醒：轮询增量事件池（自上次 markAllSeen 以来按事件 id 去重） + 已读水位 =====
  const [alerts, setAlerts] = useState<WatchAlertItem[]>([])
  const pendingEventsRef = useRef(new Map<number, WatchlistNotifyEvent>())

  // 已读水位与游标解耦：markAllSeen 只推进水位、清空事件池，不动游标
  const markAllSeen = useCallback(() => {
    const pool = pendingEventsRef.current
    if (pool.size > 0) {
      let maxId = 0
      for (const e of pool.values()) if (e.id > maxId) maxId = e.id
      const cur = readSeen() ?? 0
      if (maxId > cur) writeSeen(maxId)
      pool.clear()
    }
    setAlerts([])
  }, [])

  useEffect(() => {
    let stopped = false
    let timer: ReturnType<typeof setInterval> | undefined
    // 首轮标记：仅组件（重新）挂载后的第一次 poll 允许游标回退重拉未读区间
    const firstPollRef = { current: true }

    const poll = async () => {
      if (stopped) return
      try {
        const seen0 = readSeen() ?? 0
        let cursor = readCursor()
        // 进程重启后内存事件池已丢：若拉取游标超前已读水位，首轮回退到已读水位重拉未读区间
        // （已读部分 id≤seen 不入池不重弹），避免「游标先于已读水位推进」导致重启后未读静默丢失
        if (firstPollRef.current && cursor != null && cursor > seen0) cursor = seen0
        firstPollRef.current = false
        const { data } = await api.get<NotifyResp>('/market/watchlist/notifications', {
          params: cursor !== null ? { after_id: cursor, limit: FETCH_LIMIT } : { limit: FETCH_LIMIT },
        })
        if (stopped) return
        // 游标推进到服务端最新（分页正常拉全；重启回退仅发生在首轮，不重复回退）
        writeCursor(data.latest_id)
        // bootstrap：只建游标不弹历史；无游标首轮且已读水位缺失时初始化为 latest_id，保证首屏绝不弹历史旧事件
        if (cursor === null && data.bootstrap && readSeen() === null) {
          writeSeen(data.latest_id)
        }
        // 后续增量轮次：过滤未读事件入池（事件 id 去重），按股票聚合；超上限淘汰最旧
        if (!data.bootstrap && data.data.length > 0) {
          const seen = readSeen() ?? 0
          const pool = pendingEventsRef.current
          let added = false
          for (const e of data.data) {
            if (e.id > seen && !pool.has(e.id)) {
              pool.set(e.id, e)
              added = true
            }
          }
          while (pool.size > MAX_PENDING_EVENTS) {
            let oldest = Number.POSITIVE_INFINITY
            for (const id of pool.keys()) if (id < oldest) oldest = id
            if (!Number.isFinite(oldest)) break
            pool.delete(oldest)
          }
          if (added) {
            const items = aggregate([...pool.values()], seen)
            setAlerts(items)
            // 系统通知:页面不可见且开启+已授权时触发(页面可见时保持站内浮窗,不打扰)
            webNotifyRef.current.maybeNotifyNewSignals(items)
          }
        }
      } catch {
        /* 静默：网络/后端不可用时不打扰用户 */
      }
    }

    // 首轮立即 bootstrap（不弹历史），随后 30s 轮询（后台隐藏跳过 tick，回前台续上）
    void poll()
    timer = guardedInterval(() => { void poll() }, POLL_MS)

    return () => {
      stopped = true
      if (timer) clearInterval(timer)
    }
  }, [])

  // ===== 信号标签映射：挂载后拉一次 catalog，失败回退 code 原文 =====
  const [labelMap, setLabelMap] = useState<Record<string, { text: string; color: string }>>({})
  useEffect(() => {
    let cancelled = false
    getSignalCatalog()
      .then((items) => {
        if (!cancelled) setLabelMap(signalLabelMap(items))
      })
      .catch(() => { /* 拉取失败：labelOf 回退 { text: code, color: 'default' } */ })
    return () => { cancelled = true }
  }, [])
  const labelOf = useCallback(
    (code: string) => labelMap[code] ?? { text: code, color: 'default' },
    [labelMap],
  )

  // ===== 关注切换：以 watchlistData 为唯一事实源，乐观更新 + 失败回滚 + in-flight 锁防双击乱序 =====
  // 实现与 hooks/useWatchlistStar.ts 同构（乐观覆盖 → API → 成功 invalidateWatchlist / 失败回滚 + notifications）；
  // useWatchlistStar 是单 code hook（useWatchlist() 派生 watched），本 hook 是动态 code 列表，
  // 无法直接实例化——保留本地 watchlistData 乐观展示层（浮窗要即时反馈），API 面 toggleWatch(code,name) 不变。
  const pendingWatchRef = useRef(new Set<string>())
  const toggleWatch = useCallback(async (code: string, name?: string) => {
    if (pendingWatchRef.current.has(code)) return
    pendingWatchRef.current.add(code)
    const watched = watchlistData.some((w) => w.code === code)
    if (watched) {
      setWatchlistData((prev) => prev.filter((w) => w.code !== code))
    } else {
      setWatchlistData((prev) => (prev.some((w) => w.code === code) ? prev : [...prev, { code, name: name ?? code }]))
    }
    try {
      if (watched) await api.delete(`/market/watchlist/${code}`)
      else await api.post(`/market/watchlist/${code}`)
      invalidateWatchlist()
    } catch (e) {
      if (watched) {
        setWatchlistData((prev) => (prev.some((w) => w.code === code) ? prev : [...prev, { code, name: name ?? code }]))
      } else {
        setWatchlistData((prev) => prev.filter((w) => w.code !== code))
      }
      // 与 useWatchlistStar 对齐：notifications 语义色（error=red），回滚到服务端事实后提示
      notifications.show({
        color: 'red',
        title: '关注操作失败',
        message: e instanceof Error ? e.message : String(e),
      })
    } finally {
      pendingWatchRef.current.delete(code)
    }
  }, [watchlistData])

  return {
    alerts,
    unreadCount: alerts.length,
    watchlistData,
    markAllSeen,
    toggleWatch,
    labelOf,
  }
}
