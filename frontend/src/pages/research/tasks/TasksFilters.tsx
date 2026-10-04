import { ActionIcon, Group, Select, Switch, Text, TextInput, Tooltip } from '@mantine/core'
import { IconX } from '@tabler/icons-react'
import { useEffect, useState } from 'react'
import InfiniteScrollToggle from '../../../components/ui/InfiniteScrollToggle'
import Toolbar from '../../../components/ui/Toolbar'
import { STATUS_OPTIONS, TYPE_OPTIONS } from './constants'

/** 前端排序键（服务端 /experiments/page 不支持 order_by，前端对当前可见行排序）：
 *  耗时排序需列表快照含 finished_at（现仅详情接口返回），暂禁用待后端补充。 */
export type SortKey = 'time' | 'type' | 'status' | 'duration'

interface TasksFiltersProps {
  type: string
  onType: (v: string) => void
  status: string
  onStatus: (v: string) => void
  sortBy: SortKey
  onSortBy: (v: SortKey) => void
  sortOrder: 'asc' | 'desc'
  onSortOrder: (v: 'asc' | 'desc') => void
  keyword: string
  /** 防抖后的关键词回调（父层负责持久化与回第一页/清空选择） */
  onKeyword: (v: string) => void
  showArchived: boolean
  onShowArchived: (v: boolean) => void
  infOn: boolean
  onInf: (v: boolean) => void
  /** 关键词防抖窗口（默认 250ms）：输入即时响应，停止输入该窗口后发射 onKeyword */
  keywordDebounceMs?: number
}

/** 控件高度统一（.sr-ctl-h 类 + 内联 input 高度，与 DatasetSelector 一致） */
const CTL_STYLES = { input: { height: 'var(--sr-ctl-h)', minHeight: 'var(--sr-ctl-h)' } }

/**
 * 筛选工具条（T-43 从 Tasks.tsx 拆出）：类型/状态 Select + 排序 Select×2 + 关键词 Input
 * + 显示已归档 Switch + 无限滚动 Toggle，全部受控，父层持有状态与持久化。
 * 关键词防抖内聚于此：输入框即时更新，静默 keywordDebounceMs 后经 onKeyword 发射。
 */
export default function TasksFilters({
  type, onType, status, onStatus,
  sortBy, onSortBy, sortOrder, onSortOrder,
  keyword, onKeyword, showArchived, onShowArchived, infOn, onInf,
  keywordDebounceMs = 250,
}: TasksFiltersProps) {
  // 输入框即时响应；父层持久化值变化（外部重置/恢复）时回写
  const [input, setInput] = useState(keyword)
  useEffect(() => {
    if (keyword !== input) setInput(keyword)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keyword])
  // 防抖发射：停止输入 keywordDebounceMs 后通知父层（持久化 + 回第一页 + 清空选择）
  useEffect(() => {
    const t = setTimeout(() => {
      if (input !== keyword) onKeyword(input)
    }, keywordDebounceMs)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [input, keywordDebounceMs])

  const sortTail = (
    <Tooltip label="排序为前端执行：分页模式仅当前页、无限模式为已加载范围（后端 /experiments/page 暂不支持 order_by）；「耗时」需列表含 finished_at 字段（待后端补充）">
      <Group gap={4}>
        <Select value={sortBy} onChange={(v) => v != null && onSortBy(v as SortKey)} className="sr-ctl-h"
          style={{ flex: '1 1 92px', minWidth: 84, maxWidth: 130 }} styles={CTL_STYLES}
          data={[
            { value: 'time', label: '按时间' },
            { value: 'type', label: '按类型' },
            { value: 'status', label: '按状态' },
            { value: 'duration', label: '按耗时', disabled: true },
          ]}
        />
        <Select value={sortOrder} onChange={(v) => v != null && onSortOrder(v as 'asc' | 'desc')} className="sr-ctl-h"
          style={{ flex: '1 1 76px', minWidth: 70, maxWidth: 110 }} styles={CTL_STYLES}
          data={[
            { value: 'desc', label: '降序' },
            { value: 'asc', label: '升序' },
          ]}
        />
      </Group>
    </Tooltip>
  )

  return (
    <Toolbar sticky tail={
      <>
        <Tooltip label="开启后展示已归档任务（归档为本地标记，待后端软删支持）">
          <Group gap={4}>
            <Switch size="xs" checked={showArchived} onChange={(e) => onShowArchived(e.currentTarget.checked)} aria-label="显示已归档" />
            <Text span style={{ fontSize: 'var(--sr-font-xs)' }}>已归档</Text>
          </Group>
        </Tooltip>
        <InfiniteScrollToggle
          checked={infOn} onChange={onInf} storageKey="sr-tasks-infinite"
          tooltip="切换列表模式：无限滚动/分页；无限滚动从 offset 0 增量加载全部任务（筛选保持服务端），不支持批量选择"
        />
      </>
    }>
      <Select value={type} onChange={(v) => v != null && onType(v)} data={TYPE_OPTIONS}
        className="sr-ctl-h" style={{ flex: '1 1 140px', minWidth: 90, maxWidth: 320 }} styles={CTL_STYLES} />
      <Select value={status} onChange={(v) => v != null && onStatus(v)} data={STATUS_OPTIONS}
        className="sr-ctl-h" style={{ flex: '1 1 140px', minWidth: 90, maxWidth: 320 }} styles={CTL_STYLES} />
      {sortTail}
      <TextInput
        placeholder="搜索表达式 / 数据集名 / 任务ID（服务端）" value={input}
        onChange={(e) => setInput(e.target.value)} className="sr-ctl-h"
        style={{ flex: '1 1 140px', minWidth: 90, maxWidth: 320 }} styles={CTL_STYLES}
        rightSection={input ? (
          <ActionIcon size="xs" variant="subtle" onClick={() => setInput('')} aria-label="清空搜索">
            <IconX size={14} />
          </ActionIcon>
        ) : undefined}
      />
    </Toolbar>
  )
}
