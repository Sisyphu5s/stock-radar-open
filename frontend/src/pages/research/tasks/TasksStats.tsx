import { Grid, GridCol, Tooltip } from '@mantine/core'
import MetricStat from '../../../components/ui/MetricStat'

/** 顶部统计概览数据（Tasks.tsx 计算传入：全站共享任务池快照，最近 200 条，非全库总数） */
export interface TasksStatsShape {
  all: number
  active: number
  pending: number
  running: number
  paused: number
  done: number
  failed: number
  cancelled: number
}

interface TasksStatsProps {
  stats: TasksStatsShape
  typeFilter: string
  statusFilter: string
  onType: (v: string) => void
  onStatus: (v: string) => void
}

/** 「任务快照」口径说明（L3 tooltip；05-pages §5.6：注脚改 tooltip 不再占行） */
const SNAPSHOT_TOOLTIP = '统计口径：最近 200 条任务快照（非全库总数），已归档任务不计入；15s 轮询刷新'

/**
 * 顶部统计概览（T-43 从 Tasks.tsx 拆出）：MetricStat 栅格行，active/onClick 与类型/状态筛选联动。
 * 状态语义色走 --sr-* 令牌（与 StatusTag/JOB_STATUS_META 同源，不借涨跌色）。
 * 「任务快照」项口径说明以 L3 tooltip 呈现（sub 注脚行移除）；点击联动筛选行为不变。
 */
export default function TasksStats({ stats, typeFilter, statusFilter, onType, onStatus }: TasksStatsProps) {
  const snapshot = {
    key: 'all', label: '任务快照', value: stats.all, color: 'var(--sr-text-1)',
    active: typeFilter === 'all' && statusFilter === 'all',
    onClick: () => { onType('all'); onStatus('all') },
  }
  const items = [
    { key: 'active', label: '进行中（含排队）', value: stats.active, color: 'var(--sr-accent)', active: statusFilter === 'active', onClick: () => onStatus('active') },
    { key: 'pending', label: '排队中', value: stats.pending, color: 'var(--sr-text-2)', active: statusFilter === 'pending', onClick: () => onStatus('pending') },
    { key: 'paused', label: '已暂停', value: stats.paused, color: 'var(--sr-warning)', active: statusFilter === 'paused', onClick: () => onStatus('paused') },
    { key: 'done', label: '已完成', value: stats.done, color: 'var(--sr-success)', active: statusFilter === 'done', onClick: () => onStatus('done') },
    { key: 'failed', label: '失败', value: stats.failed, color: 'var(--sr-error)', active: statusFilter === 'failed', onClick: () => onStatus('failed') },
    { key: 'cancelled', label: '已取消', value: stats.cancelled, color: 'var(--sr-text-3)', active: statusFilter === 'cancelled', onClick: () => onStatus('cancelled') },
  ]
  return (
    <Grid className="sr-task-stats" gap={12} align="stretch">
      {/* T-63 断点对齐：仅用准许断点 480/576/768/992…（base/sm/md，无 xl）——
          md:3 起 992+ 即 8 列/行，宽屏不再另行加档（xl:1.5 亦为 8 列，语义重复） */}
      <GridCol key={snapshot.key} span={{ base: 6, sm: 4, md: 3 }}>
        {/* Tooltip 需真实 DOM 子元素承载 hover 事件（MetricStat 非 forwardRef 组件，不能直接作 child） */}
        <Tooltip label={SNAPSHOT_TOOLTIP}>
          <div>
            <MetricStat label={snapshot.label} value={snapshot.value} color={snapshot.color} active={snapshot.active} onClick={snapshot.onClick} />
          </div>
        </Tooltip>
      </GridCol>
      {items.map((it) => (
        <GridCol key={it.key} span={{ base: 6, sm: 4, md: 3 }}>
          <MetricStat label={it.label} value={it.value} color={it.color} active={it.active} onClick={it.onClick} />
        </GridCol>
      ))}
    </Grid>
  )
}
