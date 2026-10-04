import { Text } from '@mantine/core'
import type { CSSProperties, ReactNode } from 'react'
import type { AsyncState, AsyncStatus } from '../../hooks/useAsyncState'
import EmptyState from './EmptyState'
import SkeletonBlock from './SkeletonBlock'

interface PageStateProps<T> {
  /** 三态状态对象(useAsyncState 结果);与散 props 二选一,传入时优先 */
  state?: AsyncState<T>
  // ---- 散 props 用法(与 state 二选一,供非 useAsyncState 的调用方) ----
  status?: AsyncStatus
  data?: T
  error?: string | null
  stale?: boolean
  emptyDesc?: ReactNode
  refetch?: () => void
  reset?: () => void
  /** ready 渲染函数(data 保证非空);loading/error/empty 由内部骨架/EmptyState 承担 */
  children: (data: NonNullable<T>) => ReactNode
  /** 加载态说明文字(骨架下方小字) */
  loadingText?: string
  /** 加载/空/错误态最小高度(默认 120px,区块稳定,对齐 CardState) */
  minHeight?: number | string
  /** 透传加载/空/错误态容器(与 CardState style 兼容) */
  style?: CSSProperties
}

/**
 * 三态统一渲染壳(06-design-system §2.2,取代 CardState):
 *   loading → SkeletonBlock + loadingText
 *   error   → EmptyState 错误态(红字 + 重试)
 *   empty   → EmptyState 空态(emptyDesc)
 *   ready   → children(data);stale 时顶部降饱和提示条 + 内容降饱和
 */
export default function PageState<T>({
  state,
  status,
  data,
  error,
  stale,
  emptyDesc,
  refetch,
  reset,
  children,
  loadingText,
  minHeight = 120,
  style,
}: PageStateProps<T>) {
  const s: AsyncState<T> = state ?? {
    status: status ?? 'loading',
    data,
    error: error ?? null,
    stale: stale ?? false,
    emptyDesc,
    refetch: refetch ?? (() => {}),
    reset: reset ?? (() => {}),
  }

  const center: CSSProperties = {
    minHeight,
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 'var(--sr-gap-row)',
    ...style,
  }

  if (s.status === 'loading') {
    return (
      <div style={{ ...center, padding: 'var(--sr-pad-2xl) 0' }}>
        <SkeletonBlock variant="text" rows={3} />
        {loadingText && (
          <Text size="xs" c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>{loadingText}</Text>
        )}
      </div>
    )
  }

  if (s.status === 'error') {
    return (
      <div style={{ ...center, padding: 'var(--sr-pad-2xl) 0' }}>
        <EmptyState text={s.error ?? '加载失败'} onRetry={s.refetch} padding={0} />
      </div>
    )
  }

  if (s.status === 'empty' || s.data == null) {
    return (
      <div style={{ ...center, padding: 'var(--sr-pad-2xl) 0' }}>
        <EmptyState description={s.emptyDesc ?? '暂无数据'} padding={0} />
      </div>
    )
  }

  // ready:数据可用;stale 时旧数据降饱和 + 顶部提示条
  return (
    <>
      {s.stale && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 'var(--sr-gap-row)',
            marginBottom: 'var(--sr-gap-row)',
            padding: '4px var(--sr-pad-md)',
            background: 'var(--sr-block-bg)',
            borderRadius: 'var(--sr-radius-tag)',
          }}
        >
          <Text size="xs" c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
            数据更新中,当前展示为上次结果
          </Text>
        </div>
      )}
      <div style={{ opacity: s.stale ? 0.7 : 1, transition: 'opacity var(--sr-dur-norm) ease' }}>
        {children(s.data as NonNullable<T>)}
      </div>
    </>
  )
}
