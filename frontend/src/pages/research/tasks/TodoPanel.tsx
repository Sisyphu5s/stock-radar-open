import { ActionIcon, Button, Checkbox, Group, Modal, SegmentedControl, TextInput, Tooltip } from '@mantine/core'
import { IconPencil, IconPlus, IconTrash, IconX } from '@tabler/icons-react'
import { openConfirmModal } from '@mantine/modals'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../../../api/client'
import { CardState } from '../../../components/ui'
import { toast } from '../shared/toast'

/** 待办契约（与 /api/v1/todos 对齐；本地声明，不侵入全局 client 类型） */
export interface TodoItem {
  id: number
  title: string
  completed: boolean
  created_at: string | null
  updated_at: string | null
}

type TodoFilter = 'all' | 'active' | 'done'

const FILTER_OPTIONS: { label: string; value: TodoFilter }[] = [
  { label: '全部', value: 'all' },
  { label: '待完成', value: 'active' },
  { label: '已完成', value: 'done' },
]

/**
 * 任务中心「待办事项」视图：新增 / 完成切换 / 重命名（弹窗） / 删除 +
 * 全部·待完成·已完成筛选与计数，加载 / 错误 / 空态齐备。
 * 本地按服务端规则排序（未完成优先，同组 updated_at 倒序），保证切换后位置稳定。
 */
export default function TodoPanel() {
  const [items, setItems] = useState<TodoItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<TodoFilter>('all')
  const [draft, setDraft] = useState('')
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState<TodoItem | null>(null)
  const [rename, setRename] = useState('')
  const [savingId, setSavingId] = useState<number | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const { data } = await api.get<{ data: TodoItem[] }>('/todos')
      setItems(data.data ?? [])
    } catch (e: any) {
      setError(e?.response?.data?.detail ?? e?.message ?? '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const counts = useMemo(() => {
    const done = items.filter((t) => t.completed).length
    return { total: items.length, done, active: items.length - done }
  }, [items])

  // 服务端排序复刻：未完成优先（completed ASC），同组内 updated_at 倒序
  const ordered = useMemo(() => [...items].sort((a, b) => {
    if (a.completed !== b.completed) return a.completed ? 1 : -1
    return String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? ''))
  }), [items])

  const filtered = useMemo(() => {
    if (filter === 'active') return ordered.filter((t) => !t.completed)
    if (filter === 'done') return ordered.filter((t) => t.completed)
    return ordered
  }, [ordered, filter])

  const patchItem = useCallback(async (id: number, payload: { title?: string; completed?: boolean }) => {
    setSavingId(id)
    try {
      const { data } = await api.patch<TodoItem>(`/todos/${id}`, payload)
      setItems((prev) => prev.map((t) => (t.id === id ? data : t)))
      return data
    } catch (e: any) {
      toast.error('保存失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
      return null
    } finally {
      setSavingId(null)
    }
  }, [])

  const handleAdd = async () => {
    const title = draft.trim()
    if (!title) return
    setAdding(true)
    try {
      const { data } = await api.post<TodoItem>('/todos', { title })
      setItems((prev) => [data, ...prev])
      setDraft('')
    } catch (e: any) {
      toast.error('新增失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
    } finally {
      setAdding(false)
    }
  }

  const handleToggle = async (item: TodoItem) => {
    await patchItem(item.id, { completed: !item.completed })
  }

  const startEdit = (item: TodoItem) => {
    setEditing(item)
    setRename(item.title)
  }

  const handleRename = async () => {
    if (!editing) return
    const title = rename.trim()
    if (!title) {
      toast.warning('标题不能为空')
      return
    }
    const ok = await patchItem(editing.id, { title })
    if (ok) setEditing(null)
  }

  const handleDelete = async (item: TodoItem) => {
    setSavingId(item.id)
    try {
      await api.delete(`/todos/${item.id}`)
      setItems((prev) => prev.filter((t) => t.id !== item.id))
    } catch (e: any) {
      toast.error('删除失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
    } finally {
      setSavingId(null)
    }
  }

  const confirmDelete = (item: TodoItem) => {
    openConfirmModal({
      title: '删除这条待办？',
      children: '删除后不可恢复。',
      labels: { confirm: '确认删除', cancel: '返回' },
      confirmProps: { color: 'red' },
      onConfirm: () => void handleDelete(item),
    })
  }

  const emptyDesc = filter === 'all'
    ? '暂无待办事项，输入上方内容新增'
    : filter === 'active' ? '没有待完成事项' : '没有已完成事项'

  return (
    <div className="sr-todo">
      <div className="sr-todo-add">
        <TextInput
          className="sr-ctl-h"
          placeholder="新增待办事项，回车提交"
          value={draft}
          maxLength={200}
          disabled={adding}
          styles={{ root: { flex: 1, minWidth: 0 }, input: { height: 'var(--sr-ctl-h)', minHeight: 'var(--sr-ctl-h)' } }}
          rightSection={draft ? (
            <ActionIcon size="xs" variant="subtle" onClick={() => setDraft('')} aria-label="清空输入">
              <IconX size={14} />
            </ActionIcon>
          ) : undefined}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void handleAdd()
          }}
        />
        <Button
          variant="filled" leftSection={<IconPlus size={14} />} loading={adding}
          disabled={!draft.trim()} className="sr-ctl-h"
          onClick={() => void handleAdd()}
        >
          新增
        </Button>
      </div>
      <div className="sr-todo-meta">
        <SegmentedControl size="xs" value={filter} onChange={(v) => setFilter(v as TodoFilter)} data={FILTER_OPTIONS} />
        <span className="sr-todo-count">
          待完成 {counts.active} · 已完成 {counts.done} · 共 {counts.total}
        </span>
      </div>
      <CardState
        loading={loading}
        error={error}
        empty={filtered.length === 0}
        emptyDesc={emptyDesc}
        onRetry={() => void load()}
      >
        <ul className="sr-todo-list">
          {filtered.map((t) => (
            <li key={t.id} className={'sr-todo-row' + (t.completed ? ' sr-todo-done' : '')}>
              <Checkbox
                checked={t.completed}
                disabled={savingId === t.id}
                onChange={() => void handleToggle(t)}
              />
              <span className="sr-todo-title" title={t.title}>{t.title}</span>
              <span className="sr-todo-actions">
                <Tooltip label="重命名">
                  <Button size="xs" variant="subtle" leftSection={<IconPencil size={14} />}
                    disabled={savingId === t.id} onClick={() => startEdit(t)}
                    aria-label={`重命名待办：${t.title}`} />
                </Tooltip>
                <Tooltip label="删除">
                  <Button size="xs" variant="subtle" color="red" leftSection={<IconTrash size={14} />}
                    disabled={savingId === t.id} onClick={() => confirmDelete(t)}
                    aria-label={`删除待办：${t.title}`} />
                </Tooltip>
              </span>
            </li>
          ))}
        </ul>
      </CardState>
      <Modal
        title="重命名待办"
        opened={editing != null}
        onClose={() => setEditing(null)}
      >
        <TextInput
          value={rename}
          maxLength={200}
          placeholder="待办标题"
          autoFocus
          onChange={(e) => setRename(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void handleRename()
          }}
        />
        <Group justify="flex-end" gap="var(--sr-pad-md)" style={{ marginTop: 'var(--sr-pad-lg)' }}>
          <Button variant="default" onClick={() => setEditing(null)}>取消</Button>
          <Button variant="filled" onClick={() => void handleRename()}>保存</Button>
        </Group>
      </Modal>
    </div>
  )
}
