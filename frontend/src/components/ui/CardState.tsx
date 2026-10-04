import type { CSSProperties, ReactNode } from 'react'
import PageState from './PageState'

interface CardStateProps {
  loading?: boolean
  error?: string | null
  empty?: boolean
  emptyDesc?: ReactNode
  onRetry?: () => void
  loadingText?: string
  /** 最小高度(默认 120px,加载/空态时保持区块稳定) */
  minHeight?: number | string
  style?: CSSProperties
  children: ReactNode
}

/**
 * 区块三态兼容壳:旧 props(loading/error/empty/emptyDesc/onRetry/loadingText/
 * minHeight/style/children)全部不变,内部渲染 PageState 对应形态。
 * 消费方页面在 W5 统一迁移到 PageState 后本壳退役(不删除,过渡期保留)。
 */
export default function CardState({ loading, error, empty, emptyDesc, onRetry, loadingText, minHeight = 120, style, children }: CardStateProps) {
  return (
    <PageState<ReactNode>
      status={loading ? 'loading' : error ? 'error' : empty ? 'empty' : 'ready'}
      data={children}
      error={error}
      emptyDesc={emptyDesc}
      refetch={onRetry}
      loadingText={loadingText}
      minHeight={minHeight}
      style={style}
    >
      {() => <>{children}</>}
    </PageState>
  )
}
