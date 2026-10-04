import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useState } from 'react'
import type { ReactNode } from 'react'

/**
 * 三态统一机制(T-39,06-design-system §2):页面侧异步状态唯一入口。
 * ------------------------------------------------------------
 * 薄语义层:内部直接消费 TanStack Query(轮询/失效/重试等能力全部来自
 * useQuery),本 hook 只做四态(status)归一化 + stale 标记(02 §2.5 硬规则 7:
 * 异步结果必须回答"数据是不是过期的")。
 *
 * 状态语义:
 *   loading — 请求在途且无真实数据(含首载 placeholderData 占位期)
 *   error   — 请求失败;error 为文案,data 为 undefined
 *   empty   — 请求成功但数据为空(isEmpty 判定)
 *   ready   — 请求成功且数据非空
 *   stale   — ready 之上叠加:新请求已发出未返回(keepPrevious 保留旧数据,
 *             UI 需降饱和提示);首次加载不 stale(尚无旧数据可展示)
 *
 * deps 内容变化 → queryKey 变化 → 自动重拉;调用方无需手动 refetch。
 */
export type AsyncStatus = 'loading' | 'error' | 'empty' | 'ready'

export interface AsyncState<T> {
  status: AsyncStatus
  /** ready/empty 时非空(empty 保留原数据如空数组);error 时为 undefined */
  data: T | undefined
  /** 错误文案;仅 status==='error' */
  error: string | null
  /** stale:数据是旧的后台结果(新请求已发出未返回),UI 需降饱和提示 */
  stale: boolean
  /** 空态描述(empty 时展示);由调用方按语义传入 */
  emptyDesc?: ReactNode
  /** 重新请求(清除手动置空,回到查询驱动状态) */
  refetch: () => void
  /** 手动置空(如筛选清空):status → empty,data → undefined */
  reset: () => void
}

export interface UseAsyncStateOptions<T> {
  fetcher: (signal: AbortSignal) => Promise<T>
  /** 参与 queryKey 的依赖;内容变化自动重拉(JSON 序列化后入 key) */
  deps: unknown[]
  /** 空判定;缺省 data 为 null/undefined/空数组 */
  isEmpty?: (d: T) => boolean
  /** 是否保留旧数据直到新数据到达(默认 true:请求中 stale 标记而非清空) */
  keepPrevious?: boolean
}

const defaultIsEmpty = (d: unknown): boolean =>
  d == null || (Array.isArray(d) && d.length === 0)

export function useAsyncState<T>({
  fetcher,
  deps,
  isEmpty = defaultIsEmpty,
  keepPrevious = true,
}: UseAsyncStateOptions<T>): AsyncState<T> {
  const depsKey = JSON.stringify(deps)
  const [cleared, setCleared] = useState(false)

  const q = useQuery<T, Error, T>({
    queryKey: ['useAsyncState', depsKey],
    queryFn: ({ signal }) => fetcher(signal),
    // placeholderData 函数形式:缓存里上一份数据作为占位(keepPrevious 语义)
    placeholderData: keepPrevious ? (prev) => prev : undefined,
  })

  // deps 内容变化 → 解除手动置空,重新由查询驱动
  useEffect(() => {
    setCleared(false)
  }, [depsKey])

  // q.isPending 覆盖两种在途:无数据首载 + placeholderData 占位期
  const status: AsyncStatus = cleared
    ? 'empty'
    : q.isPending
      ? 'loading'
      : q.isError
        ? 'error'
        : isEmpty(q.data)
          ? 'empty'
          : 'ready'

  const data = cleared || q.status === 'error' ? undefined : q.data
  const error = status === 'error' ? q.error?.message || '加载失败' : null
  const stale = !cleared && q.isFetching && q.isSuccess

  // refetch 依赖 q.refetch（TanStack 稳定函数引用）而非 q（每次渲染新对象）——
  // P2-79:q 身份每渲染变化会令 refetch 身份漂移,破环消费方 useMemo/useCallback 缓存
  const qRefetch = q.refetch
  const refetch = useCallback(() => {
    setCleared(false)
    void qRefetch()
  }, [qRefetch])
  const reset = useCallback(() => {
    setCleared(true)
  }, [])

  return { status, data, error, stale, refetch, reset }
}
