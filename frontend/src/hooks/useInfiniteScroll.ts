import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

export type InfiniteScrollStatus = 'idle' | 'loading' | 'error' | 'done'

/** 自动填满：单次填满过程连续自动续载的最大批次上限（默认 5；0 关闭自动填满）。
 *  可经 UseInfiniteScrollOptions.fillMaxBatches 按哨兵覆盖。 */
export const FILL_MAX_BATCHES = 5

/** 自动填满判定：内容高度 ≤ 可视高度 + 该值视为“未填满”（需继续加载），与默认 rootMargin 底部预加载区一致 */
const FILL_TOLERANCE = 240

/** 自动探测哨兵最近的滚动祖先（dock 面板 .sr-dock-scroll / 页面 .sr-content / 表格 .sr-dt-viewport 等）。
 *  注意：不能要求祖先“当前已溢出”（scrollHeight>clientHeight）——首屏列表不足一屏时
 *  容器未溢出，若此时回退视口，之后内容增长也不会重新探测，dock 内无限滚动将永不触发（列表截断）。
 *  只要祖先 overflow-y 为 auto/scroll 即视为滚动容器，作为 IntersectionObserver root。 */
function findScrollRoot(el: Element | null): Element | null {
  let cur: Element | null = el?.parentElement ?? null
  while (cur) {
    const s = getComputedStyle(cur)
    if (/(auto|scroll|overlay)/.test(s.overflowY || '')) return cur
    cur = cur.parentElement
  }
  return null
}

export interface UseInfiniteScrollOptions {
  /** 是否还有更多数据；false 时进入 done 态并停止自动触发 */
  hasMore: boolean
  /** 加载更多回调。返回 Promise 时内部自动跟踪 loading；同步返回（客户端分批切片）也可用 */
  loadMore: () => void | Promise<unknown>
  /** 总开关（配合 InfiniteScrollToggle 使用）；false 时暂停自动触发 */
  enabled?: boolean
  /** 外部 loading 覆盖：当加载由页面统一管理（如服务端分页已有自己的 loading）时传入 */
  loading?: boolean
  /** 外部错误标志：为 true 时展示错误重试 UI（调用方负责清错） */
  error?: boolean
  /** 外部错误重试回调：提供时优先于内部重试 */
  retry?: () => void
  /** IntersectionObserver rootMargin（默认 "0px 0px 240px 0px"，提前 240px 预加载） */
  rootMargin?: string
  /** IntersectionObserver threshold */
  threshold?: number | number[]
  /** 重置键：变更时废弃在途加载并重建观察器（如筛选条件/数据源切换） */
  resetKey?: unknown
  /** 自动填满：首批不足容器高度时自动连续续载的最大批次（默认 FILL_MAX_BATCHES=5；0 关闭）。 */
  fillMaxBatches?: number
}

export interface UseInfiniteScrollResult {
  /** 绑定到哨兵元素（InfiniteScrollSentinel 的根节点）的 ref */
  sentinelRef: RefObject<HTMLDivElement | null>
  /** 当前状态：idle 可继续加载 / loading / error / done 已全部 */
  status: InfiniteScrollStatus
  /** 手动触发加载（后备按钮 / 键盘可达入口），内部自带防重复锁 */
  trigger: () => void
  /** 清除内部错误并重试；存在外部 retry 回调时改调外部 retry */
  retry: () => void
}

/**
 * 无限滚动核心 hook：
 * - IntersectionObserver 自动触发 loadMore，loading 期间加锁防重复触发
 * - generation（resetKey）变更时废弃在途请求、清空内部状态并重建观察器
 * - 兼容服务端增量（loadMore 返回 Promise，hasMore 由服务端总数/游标驱动）
 *   与客户端分批（loadMore 同步切片本地数组，hasMore 由剩余数量驱动）
 */
export function useInfiniteScroll(options: UseInfiniteScrollOptions): UseInfiniteScrollResult {
  const { hasMore, enabled, loading, error, rootMargin = '0px 0px 240px 0px', threshold = 0, resetKey } = options

  // 内部加载/错误锁（同步读写，避免 setState 异步导致的竞态）
  const intLoadingRef = useRef(false)
  const intErrorRef = useRef(false)
  // generation：resetKey / 挂载变化时自增，用于丢弃旧 generation 的异步回调
  const genRef = useRef(0)
  const latestRef = useRef(options)
  latestRef.current = options

  const sentinelRef = useRef<HTMLDivElement | null>(null)

  const [intLoading, setIntLoading] = useState(false)
  const [intError, setIntError] = useState(false)
  // fillSeq：任何一次“加载完成”（同步分批 / 内部 Promise / generation 重置）后自增，
  // 驱动填满检查 effect 重评估；异步路径的外部 loading 变化同样驱动该 effect
  const [fillSeq, setFillSeq] = useState(0)
  const bumpFill = useCallback(() => setFillSeq((s) => s + 1), [])
  /** 连续自动续载批次计数：满 FILL_MAX_BATCHES 即停，用户滚动相交或 generation 重置时清零 */
  const fillCountRef = useRef(0)

  const safeLoad = useCallback(() => {
    const cfg = latestRef.current
    const { hasMore: more, loadMore: load, enabled: on, loading: extLoading, error: extError } = cfg
    if (!more || !on || extLoading || extError || intLoadingRef.current || intErrorRef.current) return
    const gen = genRef.current
    intLoadingRef.current = true
    let out: unknown
    try {
      out = load()
    } catch {
      intLoadingRef.current = false
      setIntError(true)
      return
    }
    // 同步分批：直接解锁（数据已入列，下一次相交时再触发）
    if (!out || typeof (out as PromiseLike<unknown>).then !== 'function') {
      intLoadingRef.current = false
      bumpFill()
      return
    }
    setIntLoading(true)
    ;(out as PromiseLike<unknown>).then(
      () => {
        if (genRef.current !== gen) return
        intLoadingRef.current = false
        setIntLoading(false)
        bumpFill()
      },
      () => {
        if (genRef.current !== gen) return
        intLoadingRef.current = false
        setIntLoading(false)
        setIntError(true)
        bumpFill()
      },
    )
  }, [bumpFill])

  // 观察器生命周期 + generation 重置
  useEffect(() => {
    genRef.current += 1
    intLoadingRef.current = false
    intErrorRef.current = false
    // 重置自动填满预算并重评估一次：resetKey/开关变化后首批不足一屏时立即续载
    fillCountRef.current = 0
    setIntLoading(false)
    setIntError(false)
    bumpFill()
    const rootEl = sentinelRef.current
    const gen = genRef.current
    if (!rootEl || typeof IntersectionObserver === 'undefined') return
    const root = findScrollRoot(rootEl)
    const io = new IntersectionObserver(
      (entries) => {
        if (genRef.current !== gen) return
        if (entries.some((e) => e.isIntersecting)) {
          // 用户滚动产生的新相交：重置自动填满预算，走常规 safeLoad 防重锁
          fillCountRef.current = 0
          safeLoad()
        }
      },
      { root: root ?? null, rootMargin, threshold },
    )
    io.observe(rootEl)
    return () => io.disconnect()
    // resetKey 变化重建观察器；safeLoad/rootMargin/threshold 通过 latestRef/闭包读取；
    // enabled 纳入依赖：开关切换时重建观察器并重新评估相交
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey, rootMargin, threshold, enabled])

  // 自动填满：每次加载完成后（fillSeq 自增 / 外部 loading 归位）检查容器是否已填满；
  // 未填满且批次未超限则继续 safeLoad，直到填满一屏、数据耗尽或连续 N 批。
  // 覆盖三条路径：同步分批（loadMore 同步 setState，fillSeq 驱动）、内部 Promise（resolve 后
  // bumpFill 驱动）、外部 loading（loading true→false 驱动）。
  useEffect(() => {
    const cfg = latestRef.current
    const { hasMore: more, enabled: on, loading: extLoading, error: extError, fillMaxBatches = FILL_MAX_BATCHES } = cfg
    if (!more || !on || extLoading || extError || intLoadingRef.current || intErrorRef.current) return
    if (fillMaxBatches <= 0 || fillCountRef.current >= fillMaxBatches) return
    // 容器测量：优先滚动祖先（与观察器 root 一致），其次文档滚动元素。
    // 内容已溢出（scrollHeight > clientHeight + 240，哨兵落入底部预加载区）视为已填满 → 停止主动续载，
    // 后续由用户滚动相交（IO）自然触发；内容不足一屏（≤ 可视高度+容差）→ 继续 safeLoad 自动填满。
    const rootEl = sentinelRef.current
    const root = rootEl ? findScrollRoot(rootEl) : null
    if (root) {
      if (root.scrollHeight > root.clientHeight + FILL_TOLERANCE) return
    } else {
      const doc = document.scrollingElement
      if (!doc || doc.scrollHeight > window.innerHeight + FILL_TOLERANCE) return
    }
    fillCountRef.current += 1
    safeLoad()
    // 依赖：fillSeq（同步/内部异步完成）、loading（外部异步完成）、enabled（开启即填满）；
    // hasMore/fillMaxBatches 等经 latestRef 读取
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fillSeq, loading, enabled, safeLoad])

  const status: InfiniteScrollStatus = intError || error ? 'error' : intLoading || loading ? 'loading' : !hasMore ? 'done' : 'idle'

  const retry = useCallback(() => {
    const cfg = latestRef.current
    if (cfg.retry) {
      cfg.retry()
      return
    }
    intErrorRef.current = false
    setIntError(false)
    safeLoad()
  }, [safeLoad])

  return { sentinelRef, status, trigger: safeLoad, retry }
}
