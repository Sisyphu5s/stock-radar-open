import { Button, Group, Tooltip } from '@mantine/core'
import { openConfirmModal } from '@mantine/modals'
import {
  IconArrowBackUp, IconEye, IconInbox, IconPlayerPause, IconPlayerPlay, IconPlayerStop, IconRefresh, IconTrash,
} from '@tabler/icons-react'
import type { MouseEvent, ReactNode } from 'react'
import type { TaskRow } from './constants'
import { canRerun } from './constants'

export interface TaskActionsProps {
  job: TaskRow
  /** icon=紧凑图标按钮（桌面行/移动行）；text=带文字按钮（详情抽屉） */
  mode?: 'icon' | 'text'
  onPause?: (id: number) => void
  onResume?: (id: number) => void
  onCancel?: (id: number) => void
  onDelete?: (id: number) => void
  onRerun?: (id: number) => void
  /** 归档状态（本地标记）：已归档时提供「取消归档」入口 */
  archived?: boolean
  onArchive?: (id: number) => void
  onUnarchive?: (id: number) => void
  /** 行内「详情」入口（仅桌面 icon 模式） */
  onOpen?: () => void
}

const stop = (e: MouseEvent) => e.stopPropagation()

/**
 * 任务行操作（桌面行 / 移动行 / 详情抽屉三处共用，行为一致）：
 * - pending/running：取消；running 额外可「暂停」；
 * - paused：继续（恢复断点）+ 取消；
 * - done/failed：删除；可安全重跑的任务额外提供「重新运行」（复用原 job_type+params，确认后仅创建 1 个新任务）；
 * - 任意状态均可「归档」（本地隐藏 + 统计排除，待后端软删）；已归档任务改为「取消归档」。
 * 暂停文案准确为「在下一个计算检查点暂停」（协作式，非强制即时停止）。
 * 破坏性操作（取消/删除）与重跑走 @mantine/modals openConfirmModal 确认（替代 antd Popconfirm）。
 */
export default function TaskActions({
  job, mode = 'icon', onPause, onResume, onCancel, onDelete, onRerun,
  archived = false, onArchive, onUnarchive, onOpen,
}: TaskActionsProps) {
  const text = mode === 'text'

  const confirm = (title: string, desc: string, danger: boolean, onOk: () => void) => {
    openConfirmModal({
      title,
      children: desc,
      labels: { confirm: danger ? '确认' : '重新运行', cancel: '返回' },
      ...(danger ? { confirmProps: { color: 'red' as const } } : {}),
      onConfirm: onOk,
    })
  }

  // 主操作（可逆的状态迁移）：暂停 / 继续
  const primaryBtn = (action: 'pause' | 'resume') =>
    action === 'pause'
      ? (
        <Tooltip label="在下一个计算检查点暂停（协作式，可继续恢复）" key="pause">
          <Button size={text ? 'sm' : 'xs'} variant={text ? 'filled' : 'subtle'}
            color="var(--sr-accent)"
            leftSection={<IconPlayerPause size={14} />} onClick={() => onPause?.(job.id)}
            aria-label={text ? undefined : `暂停任务 #${job.id}`}>
            {text ? '暂停任务' : null}
          </Button>
        </Tooltip>
      )
      : (
        <Tooltip label="继续执行（从暂停断点恢复）" key="resume">
          <Button size={text ? 'sm' : 'xs'} variant={text ? 'filled' : 'subtle'}
            color="var(--sr-accent)"
            leftSection={<IconPlayerPlay size={14} />} onClick={() => onResume?.(job.id)}
            aria-label={text ? undefined : `继续任务 #${job.id}`}>
            {text ? '继续任务' : null}
          </Button>
        </Tooltip>
      )

  // 次操作：重新运行（创建新任务，原记录保留）
  const rerunBtn = (
    <Tooltip label="重新运行（复用相同类型与参数创建新任务）" key="rerun">
      <Button size={text ? 'sm' : 'xs'} variant={text ? 'default' : 'subtle'} leftSection={<IconRefresh size={14} />}
        onClick={() => confirm(`重新运行任务 #${job.id}？`, '将使用相同类型与参数创建 1 个新任务，原记录保留。', false, () => onRerun?.(job.id))}
        aria-label={text ? undefined : `重新运行任务 #${job.id}`}>
        {text ? '重新运行' : null}
      </Button>
    </Tooltip>
  )

  // 破坏性操作：取消（标记失败保留）/ 删除（不可恢复）
  const cancelBtn = (
    <Tooltip label="取消任务（记录标记为失败保留）" key="cancel">
      <Button size={text ? 'sm' : 'xs'} variant={text ? 'default' : 'subtle'} color="red" leftSection={<IconPlayerStop size={14} />}
        onClick={() => confirm(`取消任务 #${job.id}？`, '任务将被取消，记录标记为失败保留。', true, () => onCancel?.(job.id))}
        aria-label={text ? undefined : `取消任务 #${job.id}`}>
        {text ? '取消任务' : null}
      </Button>
    </Tooltip>
  )
  const deleteBtn = (
    <Tooltip label="删除任务（不可恢复）" key="delete">
      <Button size={text ? 'sm' : 'xs'} variant={text ? 'default' : 'subtle'} color="red" leftSection={<IconTrash size={14} />}
        onClick={() => confirm(`删除任务 #${job.id}？`, '删除后不可恢复。', true, () => onDelete?.(job.id))}
        aria-label={text ? undefined : `删除任务 #${job.id}`}>
        {text ? '删除任务' : null}
      </Button>
    </Tooltip>
  )

  // 归档（本地标记，可逆）：已归档 → 取消归档；未归档 → 归档。任意状态可用
  const archiveBtn = archived ? (
    <Tooltip label="取消归档（恢复显示与统计）" key="unarchive">
      <Button size={text ? 'sm' : 'xs'} variant={text ? 'default' : 'subtle'} leftSection={<IconArrowBackUp size={14} />}
        onClick={() => onUnarchive?.(job.id)}
        aria-label={text ? undefined : `取消归档任务 #${job.id}`}>
        {text ? '取消归档' : null}
      </Button>
    </Tooltip>
  ) : (
    <Tooltip label="归档任务（本地隐藏 + 统计排除，待后端软删支持）" key="archive">
      <Button size={text ? 'sm' : 'xs'} variant={text ? 'default' : 'subtle'} leftSection={<IconInbox size={14} />}
        onClick={() => onArchive?.(job.id)}
        aria-label={text ? undefined : `归档任务 #${job.id}`}>
        {text ? '归档' : null}
      </Button>
    </Tooltip>
  )

  const act: ReactNode[] = []
  if (job.status === 'running') act.push(primaryBtn('pause'), cancelBtn)
  else if (job.status === 'pending') act.push(cancelBtn)
  else if (job.status === 'paused') act.push(primaryBtn('resume'), cancelBtn)
  else if (job.status === 'done' || job.status === 'failed') {
    act.push(deleteBtn)
    if (canRerun(job)) act.push(rerunBtn)
  }
  act.push(archiveBtn)

  return (
    <Group gap={2} onClick={stop}>
      {onOpen && (
        <Tooltip label="详情" key="open">
          <Button size="xs" variant="subtle" leftSection={<IconEye size={14} />} onClick={onOpen} aria-label={`详情任务 #${job.id}`} />
        </Tooltip>
      )}
      {act}
    </Group>
  )
}
