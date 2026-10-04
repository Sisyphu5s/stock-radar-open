import { useCallback, useEffect, useRef, useState } from 'react'
import { getExperimentsPage } from '../../../api/client'
import { invalidateJobsPage, useExperimentsPage } from '../../../data/jobs'
import { useInfiniteScrollEnabled } from '../../../components/ui/InfiniteScrollToggle'
import type { TaskRow } from './constants'

const PAGE_SIZE = 20

export interface InfiniteTasksState {
  on: boolean
  setOn: (next: boolean) => void
  rows: TaskRow[]
  total: number
  loading: boolean
  hasMore: boolean
  error: boolean
  loadMore: () => void
  /** 变更时废弃在途请求并从 offset 0 重建（操作/筛选后调用） */
  reset: () => void
  /** 操作后轻量刷新：重拉第一页并与现有 rows 合并排序（不推进偏移、不重建 DOM，滚动位置不动） */
  refresh: () => void
  /** 本地移除一行（删除操作即时反馈，不等待服务端） */
  removeLocal: (id: number) => void
  /** 错误重试（清错并重置重建） */
  retry: () => void
  /** 筛选键：传给 InfiniteScrollSentinel 的 resetKey（generation 重置） */
  filterKey: string
}

/**
 * 任务长列表无限滚动（服务端增量分页）：
 * - 开启：从 offset 0 按 20/页追加，按 id 去重合并、id 降序重排（服务端序新→旧，新任务固定置顶）；
 * - 关闭：由调用方保留服务端分页（20/页）。
 * - job_type/status/keyword 筛选保持服务端执行；任一变化 → generation 重置、清空并从 0 重建。
 * - 首屏与 15s 轮询走 jobsPagePool 共享 store（useExperimentsPage 第一页，与任务页分页查询
 *   page=1 同 key 同 store → 请求去重，消除同 URL 双路 15s 轮询）；池轮询/失效返回时合并第一页
 *   数据且不破坏已加载顺序，保证暂停/继续/取消后状态及时刷新；手动「加载更多」仍直连分页接口
 *   （增量 offset 页，池只轮询第一页，不构成双路）。
 */
export function useInfiniteTasks(
  params: { job_type?: string; status?: string; keyword?: string },
  storageKey = 'sr-tasks-infinite',
): InfiniteTasksState {
  const [on, setOn] = useInfiniteScrollEnabled(storageKey, false)
  const [rows, setRows] = useState<TaskRow[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [hasMore, setHasMore] = useState(true)
  const [error, setError] = useState(false)

  const genRef = useRef(0)
  const offsetRef = useRef(0)
  /** rows 的同步镜像：fetchPage 合并排序与 hasMore 判断都在异步回调里，避免闭包读到过期 rows */
  const rowsRef = useRef<TaskRow[]>([])
  /** 手动加载（reset/loadMore）在途计数：>0 时池 merge 不接管 loading 状态（防哨兵提前重入） */
  const inflightRef = useRef(0)
  const paramsRef = useRef(params)
  paramsRef.current = params

  // 共享池订阅第一页（15s 轮询）：与任务页 useExperimentsPage(page=1) 同 key 同 store → 单路请求；
  // enabled=on 门控：无限滚动关闭时不再订阅（分页模式由任务页 pageQuery 独自订阅当前页，
  // 避免 page>1 时多一路无人消费的第一页 15s 轮询）。
  const pageState = useExperimentsPage({ limit: PAGE_SIZE, offset: 0, ...params }, on)

  /** 拉取一页并合并：gen 过期则丢弃结果（筛选重置时废弃在途请求）。
   *  合并语义：按 id 去重（新条目覆盖旧条目）后按 id 降序重排（id 递增，降序 = 时间新→旧，新任务置顶）。
   *  仅供手动加载（reset 首屏不再直连——首屏由共享池驱动；loadMore 增量页）使用；池轮询合并见下方 effect。 */
  const fetchPage = useCallback(async (offset: number, replace: boolean, gen: number) => {
    const res = await getExperimentsPage({ limit: PAGE_SIZE, offset, ...paramsRef.current })
    if (genRef.current !== gen) return
    const items = (res.items ?? []) as TaskRow[]
    setError(false)
    setTotal(res.total ?? 0)
    setHasMore(Boolean(res.has_more))
    offsetRef.current = offset + items.length
    const prev = rowsRef.current
    const next = replace
      ? items
      : [...(() => {
          const merged = new Map<number, TaskRow>()
          for (const r of prev) merged.set(r.id, r)
          for (const r of items) merged.set(r.id, r)
          return merged.values()
        })()].sort((a, b) => b.id - a.id)
    rowsRef.current = next
    setRows(next)
  }, [])

  const runPage = useCallback(async (offset: number, replace: boolean, gen: number) => {
    inflightRef.current += 1
    try {
      await fetchPage(offset, replace, gen)
    } catch {
      if (genRef.current === gen) setError(true)
    } finally {
      inflightRef.current -= 1
      if (genRef.current === gen) setLoading(false)
    }
  }, [fetchPage])

  const reset = useCallback(() => {
    genRef.current += 1
    offsetRef.current = 0
    rowsRef.current = []
    setRows([])
    setTotal(0)
    setHasMore(true)
    setError(false)
    setLoading(true)
    // 首屏交由共享池驱动：筛选参数变化 → 池 key 变化 → 新 store 订阅即拉取；
    // 参数未变（开关重开/重建）→ 失效当前 store 立即重拉（同 key 与任务页分页查询共享，无额外请求）
    invalidateJobsPage()
  }, [])

  /** 轻量刷新：失效分页池当前 store（操作后服务端状态已变），重拉第一页经合并 effect 更新 rows（不重建 DOM、滚动位置不动） */
  const refresh = useCallback(() => {
    if (!on) return
    invalidateJobsPage()
  }, [on])

  /** 本地移除一行（删除操作即时反馈） */
  const removeLocal = useCallback((id: number) => {
    const next = rowsRef.current.filter((r) => r.id !== id)
    rowsRef.current = next
    setRows(next)
  }, [])

  const retry = useCallback(() => {
    setError(false)
    reset()
  }, [reset])

  // 开关打开 / 筛选变化 → 重置 generation 并从 0 重建；关闭时清空累加
  useEffect(() => {
    if (!on) {
      rowsRef.current = []
      setRows([])
      return
    }
    reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [on, params.job_type, params.status, params.keyword])

  const loadMore = useCallback(() => {
    if (loading || !on) return undefined
    setLoading(true)
    const gen = genRef.current
    return runPage(offsetRef.current, false, gen)
  }, [loading, on, runPage])

  // 共享池第一页数据 → 合并进 rows（refreshOnly 语义：只合并、刷新 total/hasMore，不推进 offsetRef，
  // 避免后台轮询把偏移指针重置回首屏——否则每次轮询回退 offset，后续「加载更多」重复拉已加载页）。
  // hasMore 以最新响应为准：首屏已无更多 或 已加载条数 ≥ 最新 total（删除导致 total 减少、offset 越界）
  // → 置 false，避免哨兵在越界偏移上反复空拉。
  useEffect(() => {
    const res = pageState.value
    if (!on || !res) return
    const items = (res.items ?? []) as TaskRow[]
    setError(false)
    setTotal(res.total ?? 0)
    const prev = rowsRef.current
    const next = [...(() => {
      const merged = new Map<number, TaskRow>()
      for (const r of prev) merged.set(r.id, r)
      for (const r of items) merged.set(r.id, r)
      return merged.values()
    })()].sort((a, b) => b.id - a.id)
    rowsRef.current = next
    setRows(next)
    setHasMore(Boolean(res.has_more) && next.length < (res.total ?? 0))
    // 首屏加载收尾：手动加载在途时不动 loading（由 runPage finally 负责，防哨兵提前重入）
    if (inflightRef.current === 0) setLoading(false)
  }, [pageState.value, on])

  // 池首屏失败收尾（value 恒 undefined、手动加载也失败时）→ 停止转圈并亮错误态（retry 重新失效重拉）
  useEffect(() => {
    if (!on || !pageState.error || pageState.loading) return
    if (inflightRef.current > 0) return
    setLoading(false)
    setError(true)
  }, [pageState.error, pageState.loading, on])

  const filterKey = JSON.stringify({ t: params.job_type, s: params.status, k: params.keyword })

  return { on, setOn, rows, total, loading, hasMore, error, loadMore, reset, refresh, removeLocal, retry, filterKey }
}
