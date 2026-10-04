import { Badge, Button, Group, Text } from '@mantine/core'
import { IconArrowLeft, IconPlus, IconTrash } from '@tabler/icons-react'
import { openConfirmModal } from '@mantine/modals'
import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api/client'
import { useDatasetsState, invalidateDatasets, useJobs } from '../../data/jobs'
import PageHeader from '../../components/ui/PageHeader'
import PageShell from '../../components/ui/PageShell'
import DataTable from '../../components/ui/DataTable'
import CardShell from '../../components/ui/CardShell'
import IconTextButton from '../../components/ui/IconTextButton'
import PageState from '../../components/ui/PageState'
import StatStrip from '../../components/ui/StatStrip'
import TaskProgress from '../../components/ui/TaskProgress'
import InfiniteScrollToggle from '../../components/ui/InfiniteScrollToggle'
import { LAST_DATASET_KEY, resolveLastDataset } from '../../utils/useLastDataset'
import { useViewport } from '../../app/useViewport'
import { useDensityStore } from '../../stores/useAppStore'
import BuildDrawer from './datasets/BuildDrawer'
import ResearchFlowBar from './shared/ResearchFlowBar'
import { useListInfinite, ListLoadMeta, ListInfiniteSentinel } from './shared/ListInfinite'
import { toast } from './shared/toast'
import './datasets/datasets.css'

interface DatasetRow {
  id: number
  name: string
  universe: string
  start_date: string | null
  end_date: string | null
  stock_count: number
  row_count: number
  status: string
  created_at: string | null
}

/** 状态元数据（Mantine Badge 语义色；与 JOB_STATUS_META 同源思路，不借涨跌色） */
const DS_STATUS_META: Record<string, { label: string; color: string }> = {
  ready: { label: '就绪', color: 'green' },
  building: { label: '构建中', color: 'blue' },
}

const fmtDate = (iso?: string | null) => (iso ? String(iso).slice(0, 10) : '—')

interface DatasetsPageProps {
  /** 内嵌于研究页视图时返回上一级；独立路由时可不传 */
  onBack?: () => void
  /** 构建完成回调（新数据集 id）；内嵌于研究页时用于自动切换当前数据集 */
  onBuilt?: (dsId: number) => void
}

const INF_KEY = 'sr-research-ds-inf'

/** 数据集管理页：列表（成分/日期/行数/状态）+ 构建 Drawer + 删除确认 + 构建任务状态。
 *  列表区四态收编：useDatasetsState() 完整 QueryState → PageState（loading/error/empty/ready）。 */
export default function DatasetsPage({ onBack, onBuilt }: DatasetsPageProps) {
  const viewport = useViewport()
  const isMobile = viewport === 'mobile'
  const density = useDensityStore((s) => s.density)
  const ctlSize = density === 'compact' ? 'small' : 'middle'
  // 完整 QueryState：池未就绪（首载中/失败）与「真的空列表」区分开，不闪空态（P1-1）
  const dsState = useDatasetsState()
  const datasets = (dsState.value ?? []) as DatasetRow[]
  const jobs = useJobs()
  const buildJobs = (jobs ?? []).filter((j) => j.job_type === 'dataset_build').slice(0, 5)
  const [buildOpen, setBuildOpen] = useState(false)
  const [universes, setUniverses] = useState<{ key: string; name: string }[]>([])
  const inf = useListInfinite(datasets.length, INF_KEY, 15)

  // universe 选项（后端 list_universes：hs300/top500/custom）
  useEffect(() => {
    api.get('/datasets/universes').then(({ data }) => {
      setUniverses(data.data ?? [])
    }).catch(() => { /* 保持空，抽屉内提示 */ })
  }, [])

  const del = (row: DatasetRow) => {
    openConfirmModal({
      title: `删除数据集「${row.name}」？`,
      children: `将删除 ${row.stock_count ?? 0} 只成分股 / ${row.row_count ?? 0} 行面板数据及磁盘缓存，不可恢复。`,
      labels: { confirm: '删除', cancel: '取消' },
      confirmProps: { color: 'red' },
      onConfirm: async () => {
        try {
          await api.delete(`/datasets/${row.id}`)
          invalidateDatasets()
          try {
            if (Number(localStorage.getItem(LAST_DATASET_KEY)) === row.id) {
              localStorage.removeItem(LAST_DATASET_KEY)
            }
          } catch { /* ignore */ }
          toast.success('数据集已删除')
        } catch (e: any) {
          toast.error('删除失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
        }
      },
    })
  }

  const statusTag = (s: string) => {
    const cfg = DS_STATUS_META[s] ?? { label: s, color: 'gray' }
    return <Badge variant="light" color={cfg.color}>{cfg.label}</Badge>
  }

  // 列定义 useMemo 稳定引用（DataTable 已 memo）：buildJobs 轮询等无关 setState 不触发全表重渲染。
  // del 仅闭包稳定 toast（引用恒定），故不列入 deps
  const columns = useMemo(() => [
    { title: 'ID', dataIndex: 'id', align: 'center' as const, width: 60 },
    { title: '名称', dataIndex: 'name', ellipsis: true, minWidth: 120 },
    {
      title: '股票池', dataIndex: 'universe', minWidth: 90, ellipsis: true,
      render: (v: string) => <Badge variant="light" color="gray" style={{ fontSize: 'var(--sr-font-xs)' }}>{v}</Badge>,
    },
    { title: '成分', dataIndex: 'stock_count', align: 'right' as const, className: 'sr-num-col', minWidth: 60, render: (v: number) => v ?? '—' },
    { title: '行数', dataIndex: 'row_count', align: 'right' as const, className: 'sr-num-col', minWidth: 60, render: (v: number) => v ?? '—' },
    {
      title: '日期', minWidth: 150,
      render: (_: unknown, r: DatasetRow) => (
        <Text style={{ fontSize: 'var(--sr-font-sm)' }}>{fmtDate(r.start_date)} ~ {fmtDate(r.end_date)}</Text>
      ),
    },
    {
      title: '状态', align: 'center' as const,
      render: (_: unknown, r: DatasetRow) => statusTag(r.status),
    },
    {
      title: '创建时间', minWidth: 140, ellipsis: true,
      render: (_: unknown, r: DatasetRow) => (
        <Text style={{ fontSize: 'var(--sr-font-sm)' }}>{r.created_at ? fmtDate(r.created_at) : '—'}</Text>
      ),
    },
    ...(!isMobile ? [{
      title: '操作', align: 'center' as const, minWidth: 64,
      render: (_: unknown, r: DatasetRow) => (
        <Button size="xs" color="red" leftSection={<IconTrash size={14} />} onClick={() => del(r)}>删除</Button>
      ),
    }] : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [isMobile])

  // 列表区四态：value 未就绪（首载/失败）→ loading/error；就绪后空列表 → empty；否则 ready
  const listStatus = dsState.value === undefined
    ? (dsState.error ? 'error' as const : 'loading' as const)
    : (datasets.length === 0 ? 'empty' as const : 'ready' as const)

  // L0 结论条(05 §5.3):就绪/构建中计数按 status 分组;当前数据集名 = 偏好/上次/首个
  const readyCount = datasets.filter((d) => d.status === 'ready').length
  const buildingCount = datasets.length - readyCount
  const curDsId = resolveLastDataset(datasets)
  const curDsName = datasets.find((d) => d.id === curDsId)?.name

  return (
    <PageShell>
      <PageHeader
        extra={
          <Group gap={8}>
            {onBack && <IconTextButton size={ctlSize} icon={<IconArrowLeft size={16} />} text="返回研究" onClick={onBack} />}
            {!isMobile && <IconTextButton size={ctlSize} icon={<IconPlus size={16} />} text="构建数据集" type="primary" onClick={() => setBuildOpen(true)} />}
          </Group>
        }
      />
      <ResearchFlowBar current="datasets" stepsHidden />

      {/* L0 结论条(05 §5.3):就绪/构建中计数 + 当前数据集名;列表未到时数字位骨架 */}
      <StatStrip
        loading={dsState.value === undefined}
        scrollable={isMobile}
        style={{ marginBottom: 'var(--sr-gap-row)', flexShrink: 0 }}
        items={[
          { key: 'ready', label: '就绪', value: readyCount },
          { key: 'building', label: '构建中', value: buildingCount },
          { key: 'current', label: '当前数据集', value: curDsName ?? '—', sub: curDsId != null ? `#${curDsId}` : undefined },
        ]}
      />

      {buildJobs.length > 0 && (
        <CardShell title={`数据集构建任务 (${buildJobs.length})`}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-pad-md)' }}>
            {buildJobs.map((j) => (
              <TaskProgress key={j.id}
                job={{ id: j.id, job_type: j.job_type, status: j.status, progress: j.progress }}
                typeLabel={{ dataset_build: '数据集构建' }} />
            ))}
          </div>
        </CardShell>
      )}

      <CardShell
        title={`数据集列表 (${datasets.length})`}
        extra={<InfiniteScrollToggle checked={inf.on} onChange={inf.setOn} storageKey={INF_KEY} />}
      >
        <PageState<{ id: number; name?: string }[]>
          status={listStatus}
          data={dsState.value}
          error={dsState.error != null ? String(dsState.error instanceof Error ? dsState.error.message : dsState.error) : null}
          emptyDesc="暂无数据集"
          refetch={() => invalidateDatasets()}
        >
          {(list) => {
            const ds = list as DatasetRow[]
            return (
              <>
                <DataTable
                  rowKey="id"
                  columns={columns}
                  dataSource={inf.on ? ds.slice(0, inf.shown) : ds}
                  pagination={inf.on ? false : { pageSize: 10, showSizeChanger: false }}
                  sticky={!inf.on}
                  fillWidth
                />
                <ListLoadMeta on={inf.on} shown={inf.shown} total={inf.total} />
                <ListInfiniteSentinel on={inf.on} hasMore={inf.hasMore} loadMore={inf.loadMore} />
              </>
            )
          }}
        </PageState>
      </CardShell>

      <BuildDrawer
        open={buildOpen}
        universes={universes}
        onClose={() => setBuildOpen(false)}
        onBuilt={(dsId) => { setBuildOpen(false); onBuilt?.(dsId) }}
      />
    </PageShell>
  )
}
