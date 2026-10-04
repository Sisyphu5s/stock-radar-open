import { Progress, Stepper, Text } from '@mantine/core'
import type { JobEvent } from '../../../hooks/useJobEvents'

export interface MonitorPanelProps {
  job: { id: number; status: string; progress?: number; phase?: string }
  events: JobEvent[] | null
}

/** 阶段流水：按出现顺序去重提取 phase 文案 */
function extractPhases(events: JobEvent[] | null): string[] {
  const out: string[] = []
  if (!events) return out
  for (const e of events) {
    if (e.type === 'phase' && typeof e.phase === 'string' && e.phase.length > 0 && !out.includes(e.phase)) {
      out.push(e.phase)
    }
  }
  return out
}

/** 事件时间展示：epoch 秒 → 上海时区 HH:mm:ss（后端任务事件为北京时间语义，禁浏览器本地时区） */
const shTimeFmt = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', hour12: false,
  hour: '2-digit', minute: '2-digit', second: '2-digit',
})

/** 事件日志单行文案（progress 合并为进度百分比） */
function logLine(e: JobEvent): string {
  const time = shTimeFmt.format(new Date(e.ts * 1000))
  switch (e.type) {
    case 'phase':
      return `${time} ${e.phase ?? ''}`
    case 'progress':
      return `${time} 进度 ${Math.round((e.progress ?? 0) * 100) / 100}%`
    case 'done':
      return `${time} 完成`
    case 'failed':
      return `${time} 失败${e.error ? `: ${e.error}` : ''}`
    default:
      return time
  }
}

/**
 * 任务阶段流水监控面板：Stepper 阶段流水（events 去重 phase 序列）+ 进度条 + 最近事件日志。
 * events 为 null（尚未连接/无事件）时显示占位步骤「等待事件」；events 为空但 job.phase 有值
 * 时直接以 job.phase 作为唯一阶段（轮询进度与 SSE 未同步前的兜底）。
 */
export default function MonitorPanel({ job, events }: MonitorPanelProps) {
  const phases = extractPhases(events)
  const stepTitles = phases.length > 0 ? phases : job.phase ? [job.phase] : ['等待事件']
  const done = job.status === 'done'
  const failed = job.status === 'failed'

  // active 语义：done → 全部完成（active=length）；process/error → 最后一步进行中/失败
  const active = done ? stepTitles.length : Math.max(stepTitles.length - 1, 0)
  const pct = Math.round((job.progress ?? (done ? 100 : 0)) * 100) / 100

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-pad-sm)', width: '100%', minWidth: 0 }}>
      <div style={{ maxWidth: '100%', overflowX: 'auto' }}>
        <Stepper
          active={active}
          size="xs"
          color={failed ? 'var(--sr-error)' : undefined}
          styles={{ root: { justifyContent: 'flex-start', flexWrap: 'nowrap' } }}
        >
          {stepTitles.map((t, i) => (
            <Stepper.Step
              key={`${i}-${t}`}
              label={<Text span style={{ fontSize: 'var(--sr-font-xs)', whiteSpace: 'nowrap' }}>{t}</Text>}
            />
          ))}
        </Stepper>
      </div>
      <Progress
        value={pct}
        size="sm"
        radius="var(--sr-radius-tag)"
        color={failed ? 'var(--sr-error)' : done ? 'var(--sr-success)' : 'var(--sr-accent)'}
        striped={!done && !failed}
        animated={!done && !failed}
      />
      {events && events.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
          {events.slice(-12).reverse().map((e, i) => (
            <Text
              key={`${e.ts}-${e.type}-${i}`}
              c="dimmed"
              style={{ fontSize: 'var(--sr-font-xs)', lineHeight: 1.6 }}
            >
              {logLine(e)}
            </Text>
          ))}
        </div>
      )}
    </div>
  )
}
