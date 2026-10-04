import { useCallback, useEffect, useRef, useState } from 'react'
import { notifications } from '@mantine/notifications'
import { api } from '../api/client'
import type { WatchlistItem } from '../api/client'
import { invalidateWatchlist, useWatchlist } from '../data/market'

export interface WatchlistStarApi {
  /** 是否已关注（乐观覆盖后的即时值） */
  watched: boolean
  /** 该 code 请求在途（禁用重复点击） */
  pending: boolean
  /** 切换关注（乐观更新 + 失败回滚 + in-flight 锁；成功保留乐观覆盖，重拉落地后释放） */
  toggle: (name?: string) => Promise<void>
}

/**
 * 关注 code 集合派生（P2-68）：单一订阅 + 成员不变引用稳定。
 * 信号流/移动列表等行内星标由页面级唯一订阅派生集合（O(1) 成员查找）替代各组件
 * 独立订阅 useWatchlist().some()——消除 O(行数×N)；SSE tick 仅价格字段变化不改成员时
 * 集合引用不变 → 星标/列定义引用稳定（按需渲染，不再全体重渲染）。
 * watchlist 返回原始数组（关注计数 / watchlistOnly 签名等消费方继续使用）。
 */
export function useWatchlistCodes(): { codes: ReadonlySet<string>; watchlist: WatchlistItem[] | undefined } {
  const watchlist = useWatchlist()
  const [codes, setCodes] = useState<ReadonlySet<string>>(new Set())
  useEffect(() => {
    const next = new Set((watchlist ?? []).map((w) => w.code))
    setCodes((prev) => {
      // 成员不变复用旧引用（SSE tick 价格字段刷新不换引用 → 下游不重渲染）；
      // 相同引用 setState 被 React bail-out，不触发额外渲染
      if (prev.size === next.size) {
        let same = true
        for (const c of next) if (!prev.has(c)) { same = false; break }
        if (same) return prev
      }
      return next
    })
  }, [watchlist])
  return { codes, watchlist }
}

/**
 * 关注星标核心（乐观覆盖 + in-flight 锁 + 切换）。
 * P2-59：乐观覆盖在 API 成功后不清除，保留到重拉落地（serverWatched 与覆盖一致）由
 * effect 释放——避免「先切 → API 成功清覆盖 → 回落旧服务端值 → 重拉再变」闪回；
 * 失败立即回滚到服务端事实 + 通知。服务端事实来源由调用方注入（订阅派生 / 共享集合）。
 */
function useWatchlistStarState(code: string, serverWatched: boolean): WatchlistStarApi {
  // 乐观覆盖层：code -> 目标态；重拉确认后由 effect 释放
  const [override, setOverride] = useState<boolean | null>(null)
  // in-flight 锁：按 code 的 Set + 计数 tick 触发重渲染（锁在 ref，禁用态失效也防重入）
  const pendingRef = useRef(new Set<string>())
  const [, setTick] = useState(0)
  const rerender = useCallback(() => setTick((t) => t + 1), [])

  const watched = override ?? serverWatched
  const pending = pendingRef.current.has(code)

  // watched 最新值经 ref 供稳定 toggle 闭包读取（不随渲染新建回调，支撑 memo 按需渲染）
  const watchedRef = useRef(watched)
  watchedRef.current = watched

  // P2-59：重拉落地（服务端确认乐观目标）→ 释放覆盖层
  useEffect(() => {
    if (override !== null && serverWatched === override) setOverride(null)
  }, [serverWatched, override])

  const toggle = useCallback(async (name?: string) => {
    if (pendingRef.current.has(code)) return
    const cur = watchedRef.current
    pendingRef.current.add(code)
    setOverride(!cur)
    rerender()
    try {
      if (cur) {
        await api.delete(`/market/watchlist/${code}`)
      } else {
        await api.post(`/market/watchlist/${code}`, { name: name ?? code })
      }
      invalidateWatchlist()
      // 成功：保留乐观覆盖，等 invalidate 后重拉落地（serverWatched === override）由 effect 释放，
      // 避免重拉未落地窗口期回落旧服务端值造成星标闪回
    } catch (e) {
      setOverride(null) // 失败回滚到服务端事实
      notifications.show({
        color: 'red',
        title: '关注操作失败',
        message: e instanceof Error ? e.message : String(e),
      })
    } finally {
      pendingRef.current.delete(code)
      rerender()
    }
  }, [code, rerender])

  return { watched, pending, toggle }
}

/**
 * 关注星标单一实现（收编 useWatchlistAlerts / SignalCenter / useWorkbenchData 三处）。
 * 独立订阅模式（工作台等单实例消费方）：内部订阅 useWatchlist() 派生服务端事实，
 * 本地覆盖层承载乐观态；服务端列表确认后清覆盖（P2-59，防星标闪烁）。
 */
export function useWatchlistStar(code: string): WatchlistStarApi {
  const watchlist = useWatchlist()
  const serverWatched = Array.isArray(watchlist)
    ? watchlist.some((w: { code: string }) => w.code === code)
    : false
  return useWatchlistStarState(code, serverWatched)
}

/**
 * 共享集合模式（信号流等行内星标，P2-68）：serverWatched 由调用方已订阅的
 * 关注集合（useWatchlistCodes）派生传入（O(1) Set 查找），本 hook 不再订阅 useWatchlist。
 */
export function useWatchlistStarFrom(code: string, serverWatched: boolean): WatchlistStarApi {
  return useWatchlistStarState(code, serverWatched)
}
