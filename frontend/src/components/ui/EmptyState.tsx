import { Button, EmptyState as MantineEmpty } from '@mantine/core'
import { IconInboxOff } from '@tabler/icons-react'
import type { ReactNode } from 'react'

interface EmptyStateProps {
  /** 错误文案(红字);留空则显示空态 */
  text?: string
  description?: ReactNode
  onRetry?: () => void
  /** 上下内边距(默认 '16px 0') */
  padding?: string | number
}

/**
 * 统一空态/错误态(Mantine EmptyState 薄壳):错误态红字 + 重试按钮,
 * 空态图标 + 描述;替代各页手写组合。契约与旧 antd 版一致。
 */
export default function EmptyState({ text, description, onRetry, padding = '16px 0' }: EmptyStateProps) {
  if (text) {
    return (
      <MantineEmpty
        style={{ padding }}
        title={<span style={{ fontSize: 'var(--sr-font-page)', color: 'var(--sr-error)' }}>{text}</span>}
      >
        {onRetry && (
          <MantineEmpty.Actions>
            <Button size="xs" variant="light" onClick={onRetry}>重试</Button>
          </MantineEmpty.Actions>
        )}
      </MantineEmpty>
    )
  }
  return (
    <MantineEmpty
      icon={<IconInboxOff size={34} stroke={1.4} />}
      description={description ?? '暂无数据'}
      style={{ padding }}
    />
  )
}
