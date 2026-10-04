import { Progress, Text } from '@mantine/core'
import type { ReactNode } from 'react'

export interface TaskJob {
  id: number
  job_type: string
  status: string
  progress?: number
  [key: string]: unknown
}

interface TaskProgressProps {
  job: TaskJob
  /** 任务类型中文名映射,如 { gp_run: 'GP 进化' } */
  typeLabel?: Record<string, string>
  /** 展示完成后的额外信息(如最优 IC) */
  extra?: ReactNode
}

/** 任务状态点色(与 StatusTag/JOB_STATUS_META 同源的 Mantine 语义色,不借涨跌色) */
const STATUS_DOT_COLOR: Record<string, string> = {
  done: 'var(--sr-success)',
  failed: 'var(--sr-error)',
  running: 'var(--sr-accent)',
}

/**
 * 任务进度卡(Mantine Progress/Text 薄壳):状态点 + 类型名 + 状态 + 进度条
 * (7 处任务卡统一出口)。antd Badge status 行内状态点在 Mantine 无官方等价
 * (Indicator 为角落角标语义),自绘 8px 圆点承担,色走 --sr-* 语义令牌。
 */
export default function TaskProgress({ job, typeLabel, extra }: TaskProgressProps) {
  const label = typeLabel?.[job.job_type] ?? job.job_type
  const dotColor = STATUS_DOT_COLOR[job.status] ?? 'var(--sr-text-3)'
  const pct = Math.min(100, Math.round((job.progress ?? (job.status === 'done' ? 100 : 0)) * 100) / 100)
  const running = job.status === 'running'
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-pad-sm)', minWidth: 220 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sr-gap-row)' }}>
        <span aria-hidden style={{ width: 8, height: 8, borderRadius: '50%', background: dotColor, flex: 'none' }} />
        <Text span style={{ fontSize: 'var(--sr-font-sm)', fontWeight: 600 }}>{label}</Text>
        <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>#{job.id}</Text>
        <div style={{ flex: 1 }} />
        {extra}
      </div>
      {running && typeof job.phase === 'string' && job.phase.length > 0 && (
        <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>{job.phase}</Text>
      )}
      <Progress
        value={pct}
        size="sm"
        radius="var(--sr-radius-tag)"
        color={job.status === 'failed' ? 'var(--sr-error)' : job.status === 'done' ? 'var(--sr-success)' : 'var(--sr-accent)'}
        striped={running}
        animated={running}
      />
    </div>
  )
}
