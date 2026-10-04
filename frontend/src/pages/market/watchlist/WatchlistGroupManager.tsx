import { Button, Group, List, Modal, Text, TextInput, Tooltip } from '@mantine/core'
import { modals } from '@mantine/modals'
import { notifications } from '@mantine/notifications'
import { IconPencil, IconPlus, IconTrash } from '@tabler/icons-react'
import { useState } from 'react'
import type { WatchlistGroup } from '../../../api/watchlistGroups'
import { errMsg } from '../../../utils/format'

interface WatchlistGroupManagerProps {
  opened: boolean
  onClose: () => void
  groups: WatchlistGroup[]
  /** 分组接口失败：不再伪装成「暂无分组」（T-131），显示错误+重试 */
  error?: boolean
  onRetry?: () => void
  /** 新建/重命名（抛错由调用方 toast 展示，此处仅透传 await） */
  onCreate: (name: string) => Promise<void>
  onRename: (id: number, name: string) => Promise<void>
  onDelete: (id: number) => Promise<void>
}

/**
 * 分组管理 Modal（T-12）：顶部新建表单 + 分组列表（名称/数量，行内重命名、确认删除）。
 * 删除走 modals 确认（与页面「全部取消关注」同一交互范式）。
 */
export default function WatchlistGroupManager({ opened, onClose, groups, error = false, onRetry, onCreate, onRename, onDelete }: WatchlistGroupManagerProps) {
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editName, setEditName] = useState('')
  const [savingId, setSavingId] = useState<number | null>(null)

  const submitCreate = async () => {
    const name = newName.trim()
    if (!name) return
    setCreating(true)
    try {
      await onCreate(name)
      setNewName('')
    } catch (e: any) {
      notifications.show({ color: 'red', message: '新建分组失败: ' + errMsg(e) })
    } finally {
      setCreating(false)
    }
  }

  const startEdit = (g: WatchlistGroup) => {
    setEditingId(g.id)
    setEditName(g.name)
  }

  const submitRename = async (g: WatchlistGroup) => {
    const name = editName.trim()
    if (!name) return
    setSavingId(g.id)
    try {
      await onRename(g.id, name)
      setEditingId(null)
    } catch (e: any) {
      notifications.show({ color: 'red', message: '重命名失败: ' + errMsg(e) })
    } finally {
      setSavingId(null)
    }
  }

  const confirmDelete = (g: WatchlistGroup) => {
    modals.openConfirmModal({
      title: `删除分组「${g.name}」？`,
      children: <Text size="sm">将移除该分组及其 {g.count} 只成员（股票关注状态不受影响）</Text>,
      labels: { confirm: '删除', cancel: '取消' },
      confirmProps: { color: 'red' },
      onConfirm: () => {
        onDelete(g.id).catch((e: any) => {
          notifications.show({ color: 'red', message: '删除分组失败: ' + errMsg(e) })
        })
      },
    })
  }

  return (
    <Modal opened={opened} onClose={onClose} title="分组管理" size="sm" centered>
      <Group gap={8} wrap="nowrap" mb="sm">
        <TextInput
          size="xs" flex={1}
          placeholder="新分组名称"
          value={newName}
          onChange={(e) => setNewName(e.currentTarget.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void submitCreate() }}
          aria-label="新分组名称"
        />
        <Button size="xs" leftSection={<IconPlus size={14} />} loading={creating} onClick={() => void submitCreate()}>
          新建
        </Button>
      </Group>

      <List spacing={6} size="sm">
        {error ? (
          <Group gap={8} wrap="wrap">
            <Text style={{ color: 'var(--sr-error)', fontSize: 'var(--sr-font-sm)' }}>分组加载失败，无法展示分组</Text>
            <Button size="xs" variant="subtle" onClick={onRetry} aria-label="重试加载分组">重试</Button>
          </Group>
        ) : groups.length === 0 ? (
          <Text style={{ color: 'var(--sr-text-2)', fontSize: 'var(--sr-font-sm)' }}>暂无分组，输入名称新建一个</Text>
        ) : (
          groups.map((g) => (
            <List.Item key={g.id} className="sr-wl-gm-item">
              {editingId === g.id ? (
                <Group gap={6} wrap="nowrap">
                  <TextInput
                    size="xs" flex={1}
                    value={editName}
                    onChange={(e) => setEditName(e.currentTarget.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') void submitRename(g) }}
                    aria-label="重命名分组"
                  />
                  <Button size="xs" loading={savingId === g.id} onClick={() => void submitRename(g)}>保存</Button>
                  <Button size="xs" variant="default" onClick={() => setEditingId(null)}>取消</Button>
                </Group>
              ) : (
                <Group gap={6} wrap="nowrap">
                  <span className="sr-wl-gm-name">{g.name}</span>
                  <Text size="xs" c="dimmed" style={{ flexShrink: 0 }}>{g.count} 只</Text>
                  <Group gap={2} wrap="nowrap" ml="auto">
                    <Tooltip label="重命名">
                      <Button size="compact-xs" variant="subtle" leftSection={<IconPencil size={13} />} onClick={() => startEdit(g)}>重命名</Button>
                    </Tooltip>
                    <Tooltip label="删除分组">
                      <Button size="compact-xs" variant="subtle" color="red" leftSection={<IconTrash size={13} />} onClick={() => confirmDelete(g)}>删除</Button>
                    </Tooltip>
                  </Group>
                </Group>
              )}
            </List.Item>
          ))
        )}
      </List>
    </Modal>
  )
}
