import { Button, Text } from '@mantine/core'
import { IconInbox, IconPlayerPause, IconPlayerStop, IconRefresh, IconTrash } from '@tabler/icons-react'
import { openConfirmModal } from '@mantine/modals'

interface BatchActionsProps {
  /** 已选数量（当前页） */
  count: number
  hasRunning: boolean
  hasNonTerminal: boolean
  hasTerminal: boolean
  hasRerunnable: boolean
  hasVisible: boolean
  onPause: () => void
  onCancel: () => void
  onDelete: () => void
  onRerun: () => void
  onArchive: () => void
}

/**
 * 批量操作工具条（T-43 从 Tasks.tsx 拆出）：分页态桌面选中后出现，
 * 按钮按所选任务状态智能启用（仅对适用子集生效）；删除走 openConfirmModal 确认。
 * 实际批量执行器（Promise.allSettled + toast 计数）留在 Tasks.tsx，本组件只收回调。
 */
export default function BatchActions({
  count, hasRunning, hasNonTerminal, hasTerminal, hasRerunnable, hasVisible,
  onPause, onCancel, onDelete, onRerun, onArchive,
}: BatchActionsProps) {
  const confirmDelete = () => {
    openConfirmModal({
      title: '删除所选任务？',
      children: '仅删除已完成/失败/已取消的任务，删除后不可恢复。',
      labels: { confirm: '确认删除', cancel: '返回' },
      confirmProps: { color: 'red' },
      onConfirm: onDelete,
    })
  }
  return (
    <div className="sr-task-batchbar" role="toolbar" aria-label="批量操作">
      <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>已选 {count} 项（当前页）</Text>
      <div style={{ flex: 1 }} />
      <Button size="xs" leftSection={<IconPlayerPause size={14} />} disabled={!hasRunning} onClick={onPause}>批量暂停</Button>
      <Button size="xs" color="red" leftSection={<IconPlayerStop size={14} />} disabled={!hasNonTerminal} onClick={onCancel}>批量取消</Button>
      <Button size="xs" color="red" leftSection={<IconTrash size={14} />} disabled={!hasTerminal} onClick={confirmDelete}>批量删除</Button>
      <Button size="xs" leftSection={<IconRefresh size={14} />} disabled={!hasRerunnable} onClick={onRerun}>批量重跑</Button>
      <Button size="xs" leftSection={<IconInbox size={14} />} disabled={!hasVisible} onClick={onArchive}>批量归档</Button>
    </div>
  )
}
