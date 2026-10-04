import { Button, Loader, Tooltip } from '@mantine/core'
import { IconRefresh } from '@tabler/icons-react'
import clsx from 'clsx'
import type { CSSProperties, ReactNode } from 'react'
import { useInfiniteScroll } from '../../hooks/useInfiniteScroll'
import type { UseInfiniteScrollOptions } from '../../hooks/useInfiniteScroll'

export interface InfiniteScrollSentinelProps extends Omit<UseInfiniteScrollOptions, 'rootMargin' | 'threshold'> {
  className?: string
  style?: CSSProperties
  /** 加载中文案 */
  loadingText?: ReactNode
  /** 已全部加载文案 */
  doneText?: ReactNode
  /** 错误文案 */
  errorText?: ReactNode
  /** 可聚焦的“加载更多”后备按钮文案（idle 时展示，键盘可达） */
  loadMoreText?: ReactNode
  /** IntersectionObserver rootMargin */
  rootMargin?: string
  /** IntersectionObserver threshold */
  threshold?: number | number[]
  /** 自动填满：首批不足容器高度时自动连续续载的最大批次（默认 FILL_MAX_BATCHES=5；0 关闭） */
  fillMaxBatches?: number
}

/**
 * 无限滚动哨兵：列表底部占位元素，IntersectionObserver 自动触发加载。
 * - 加载中 / 已全部 / 错误重试均带 aria-live 播报（role=status/alert）
 * - 错误时提供可聚焦“重试”按钮；空闲时提供可聚焦“加载更多”后备按钮
 * - 内部经 useInfiniteScroll 防重复触发并处理 generation 清理
 */
export function InfiniteScrollSentinel({
  loadingText = '加载中…',
  doneText = '已加载全部',
  errorText = '加载失败',
  loadMoreText = '加载更多',
  className,
  style,
  ...options
}: InfiniteScrollSentinelProps) {
  const { sentinelRef, status, trigger, retry } = useInfiniteScroll(options)
  return (
    <div ref={sentinelRef} className={clsx('sr-infinite-sentinel', className)} style={style}>
      {status === 'loading' && (
        <div className="sr-infinite-state" role="status" aria-live="polite">
          <Loader size="xs" aria-hidden="true" />
          <span>{loadingText}</span>
        </div>
      )}
      {status === 'done' && (
        <div className="sr-infinite-state sr-infinite-done" role="status" aria-live="polite">
          <span>{doneText}</span>
        </div>
      )}
      {status === 'error' && (
        <div className="sr-infinite-state sr-infinite-error" role="alert" aria-live="assertive">
          <span>{errorText}</span>
          <Tooltip label="重新加载更多数据">
            <Button size="xs" variant="light" color="red" leftSection={<IconRefresh size={12} aria-hidden="true" />} onClick={retry}>
              重试
            </Button>
          </Tooltip>
        </div>
      )}
      {status === 'idle' && (
        <Button
          size="xs"
          variant="subtle"
          className="sr-infinite-more"
          onClick={trigger}
          aria-label="加载更多"
        >
          {loadMoreText}
        </Button>
      )}
    </div>
  )
}

export default InfiniteScrollSentinel
