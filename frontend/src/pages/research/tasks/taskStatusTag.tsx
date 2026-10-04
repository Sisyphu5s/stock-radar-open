import { Badge, Tooltip } from '@mantine/core'
import StatusTag from '../../../components/ui/StatusTag'
import type { TaskRow } from './constants'

/**
 * 任务状态 Tag + 协作式/错误说明 tooltip（任务中心三处共用）：
 * - paused：说明「在下一个计算检查点暂停」，消除强制即时停止的误解；
 * - failed：展示行内 error 摘要（前 120 字符）；
 * - cancelled：后端新增状态，StatusTag 无对应映射，直接渲染灰标签「已取消」。
 * 其余状态与 StatusTag 行为一致（无 tooltip）。
 */
export default function TaskStatusTag({ job }: { job: Pick<TaskRow, 'status' | 'error'> }) {
  if (job.status === 'cancelled') return <Badge variant="light" color="gray">已取消</Badge>
  const tip =
    job.status === 'paused'
      ? '协作式暂停：任务在下一个计算检查点停下（不是强制即时停止），可「继续」从断点恢复'
      : job.status === 'failed' && job.error
        ? job.error
        : undefined
  const tag = <StatusTag status={job.status} kind="job" />
  return tip ? <Tooltip label={tip}>{tag}</Tooltip> : tag
}
