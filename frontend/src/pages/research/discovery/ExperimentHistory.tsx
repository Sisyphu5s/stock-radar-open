import { Button, Group, Text } from '@mantine/core'
import { IconFileSearch, IconFlask } from '@tabler/icons-react'
import { useMemo } from 'react'
import type { SrColumn } from '../../../components/ui/tableTypes'
import { fmtTime } from '../../../utils/format'
import { DataTable, EmptyState, InfiniteScrollToggle, StatusTag } from '../../../components/ui'
import { ALGORITHM_OPTIONS, TARGET_OPTIONS, optionLabel } from './FmForm'
import { useListInfinite, ListLoadMeta, ListInfiniteSentinel } from '../shared/ListInfinite'

/** 历史挖掘实验无限滚动开关持久化键 */
const HIST_INF_KEY = 'sr-research-gp-hist-inf'

interface ExperimentHistoryProps {
  history: any[]
  error: string | null
  onReload: () => void
  onOpenDetail: (h: any) => void
  onOpenEvolve: (h: any) => void
}

/**
 * 因子挖掘历史任务表（无限滚动 + 空/错误态；列定义内聚本文件）。
 * 从 FactorMining 拆分：主组件只负责数据加载与回调，本组件承担展示与列表交互状态。
 */
export default function ExperimentHistory({ history, error, onReload, onOpenDetail, onOpenEvolve }: ExperimentHistoryProps) {
  const histInf = useListInfinite(history.length, HIST_INF_KEY, 10)

  // columns 引用稳定化（DataTable 已 memo，避免无关 setState 触发全表重渲染）：
  // 闭包仅引用回调（内部全为 ref/setter/模块函数，无渲染期可变依赖），deps 恒空
  const columns = useMemo<SrColumn<any>[]>(
    () => [
      {
        title: '时间', align: 'left',
        render: (_, h) => <Text style={{ fontSize: 'var(--sr-font-sm)' }}>{fmtTime(h.created_at)}</Text>,
      },
      { title: '任务', align: 'left', minWidth: 72, ellipsis: true, render: (_, h) => `#${h.id}` },
      { title: '算法', align: 'left', minWidth: 96, ellipsis: true, render: (_, h) => optionLabel(ALGORITHM_OPTIONS, h.params?.algorithm) },
      { title: '目标', align: 'left', minWidth: 96, ellipsis: true, render: (_, h) => optionLabel(TARGET_OPTIONS, h.params?.target) },
      {
        title: '种群×代数', align: 'center',
        render: (_, h) => `${h.params?.pop_size ?? 120}×${h.params?.generations ?? 12}`,
      },
      {
        title: '状态', align: 'center',
        render: (_, h) => <StatusTag status={h.status} kind="job" />,
      },
      {
        title: '操作', align: 'center',
        render: (_, h) => (
          <Group gap={4} wrap="nowrap">
            <Button size="xs" variant="default" leftSection={<IconFileSearch size={14} />} onClick={() => onOpenDetail(h)}>
              详情
            </Button>
            <Button size="xs" variant="default" leftSection={<IconFlask size={14} />} onClick={() => onOpenEvolve(h)}>
              进化详情
            </Button>
          </Group>
        ),
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  )

  if (history.length === 0 && error) {
    return <EmptyState text={error} onRetry={onReload} />
  }
  if (history.length === 0) {
    return <EmptyState description="暂无历史挖掘实验" />
  }
  return (
    <>
      <div className="sr-toolbar-tail" style={{ marginBottom: 'var(--sr-pad-xs)' }}>
        <InfiniteScrollToggle checked={histInf.on} onChange={histInf.setOn} storageKey={HIST_INF_KEY} />
      </div>
      <DataTable
        rowKey="id"
        dataSource={histInf.on ? history.slice(0, histInf.shown) : history}
        pagination={histInf.on ? false : { pageSize: 8, showSizeChanger: false }}
        columns={columns}
        fillWidth
      />
      <ListLoadMeta on={histInf.on} shown={histInf.shown} total={histInf.total} />
      <ListInfiniteSentinel on={histInf.on} hasMore={histInf.hasMore} loadMore={histInf.loadMore} />
    </>
  )
}
