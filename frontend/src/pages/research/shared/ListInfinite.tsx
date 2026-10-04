import { useCallback, useEffect, useRef, useState } from 'react'
import { InfiniteScrollSentinel } from '../../../components/ui/InfiniteScrollSentinel'
import { useInfiniteScrollEnabled } from '../../../components/ui/InfiniteScrollToggle'
import './research-shared.css'

export interface ListInfiniteState {
  /** 无限滚动开关（持久化） */
  on: boolean
  setOn: (next: boolean) => void
  /** 当前展示条数（无限开启时；关闭时恒等于 total） */
  shown: number
  hasMore: boolean
  loadMore: () => void
  total: number
}

/**
 * 客户端分批无限滚动状态（研究域长列表共用）：
 * - 关闭：shown = total（调用方保留分页）；
 * - 开启：按 batch 递增展示条数，挂 InfiniteScrollSentinel 增量加载。
 * 持久化 key 按页面传入（localStorage）。
 */
export function useListInfinite(total: number, storageKey: string, batch = 20): ListInfiniteState {
  const [on, setOn] = useInfiniteScrollEnabled(storageKey)
  const [count, setCount] = useState(batch)

  // 重新开启时回到首批，避免残留上一轮的滚动位置
  useEffect(() => {
    if (on) setCount(batch)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [on])

  // 数据源变化（筛选/搜索切换使 total 变化）时重置已展开数：
  // 旧 count 残留到新列表会让新列表直接跳到中段甚至误判"已全部加载"
  const prevTotalRef = useRef(total)
  useEffect(() => {
    if (total !== prevTotalRef.current) {
      prevTotalRef.current = total
      if (on) setCount(batch)
    }
  }, [total, on, batch])

  const shown = Math.min(count, total)
  const hasMore = on && shown < total
  const loadMore = useCallback(() => {
    setCount((c) => Math.min(c + batch, total))
  }, [batch, total])

  return { on, setOn, shown, hasMore, loadMore, total }
}

/** 已加载 / 总数 提示（无限滚动开启时展示；关闭时返回 null） */
export function ListLoadMeta({ on, shown, total }: { on: boolean; shown: number; total: number }) {
  if (!on) return null
  return (
    <div className="sr-list-inf-meta" role="status" aria-live="polite">
      已加载 {shown} / 共 {total}
    </div>
  )
}

/** 无限滚动哨兵（开启时挂载；关闭时 null）。loadMore 为客户端分批同步回调。 */
export function ListInfiniteSentinel({ on, hasMore, loadMore, resetKey }: {
  on: boolean
  hasMore: boolean
  loadMore: () => void
  resetKey?: unknown
}) {
  if (!on) return null
  return (
    <InfiniteScrollSentinel
      hasMore={hasMore}
      loadMore={loadMore}
      enabled
      resetKey={resetKey}
      doneText="已加载全部"
    />
  )
}
