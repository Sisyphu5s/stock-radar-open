import { Badge } from '@mantine/core'

/**
 * 三套状态元数据(单一事实源:MainLayout / Tasks / JobBar / PublishStepsPanel 共用)。
 * color 字段为 Mantine 色名(薄壳内部消费);外部消费方仅读 label。
 */
export const JOB_STATUS_META: Record<string, { label: string; color: string }> = {
  pending: { label: '排队中', color: 'orange' },
  running: { label: '运行中', color: 'blue' },
  paused: { label: '已暂停', color: 'yellow' },
  done: { label: '已完成', color: 'green' },
  failed: { label: '失败', color: 'red' },
  cancelled: { label: '已取消', color: 'gray' },
}

/** 信号事件状态 */
export const SIGNAL_STATUS_META: Record<string, { label: string; color: string }> = {
  确认: { label: '确认', color: 'green' },
  已忽略: { label: '已忽略', color: 'gray' },
  观察: { label: '观察', color: 'orange' },
}

/** 因子发布状态 */
export const PUBLISH_STATUS_META: Record<string, { label: string; color: string }> = {
  draft: { label: '草稿', color: 'gray' },
  candidate: { label: '候选', color: 'orange' },
  published: { label: '已发布', color: 'green' },
  archived: { label: '已归档', color: 'gray' },
}

interface StatusTagProps {
  status: string
  kind?: 'job' | 'signal' | 'publish'
}

/** 状态 Tag(Mantine Badge 薄壳):任务/信号/发布三套映射统一出口 */
export default function StatusTag({ status, kind = 'job' }: StatusTagProps) {
  const meta = kind === 'signal' ? SIGNAL_STATUS_META : kind === 'publish' ? PUBLISH_STATUS_META : JOB_STATUS_META
  const cfg = meta[status] ?? { label: status, color: 'gray' }
  return (
    <Badge variant="light" color={cfg.color} radius="var(--sr-radius-tag)" size="sm">
      {cfg.label}
    </Badge>
  )
}
