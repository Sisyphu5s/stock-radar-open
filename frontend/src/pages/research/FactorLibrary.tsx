import { SegmentedControl, Text } from '@mantine/core'
import { useEffect, useCallback, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { advanceVersion, api, getFactors, getPublishSteps } from '../../api/client'
import type { FactorInfo, FactorVersion } from '../../api/client'
import PageShell from '../../components/ui/PageShell'
import DataTable from '../../components/ui/DataTable'
import EmptyState from '../../components/ui/EmptyState'
import Toolbar from '../../components/ui/Toolbar'
import StatStrip from '../../components/ui/StatStrip'
import InfiniteScrollToggle from '../../components/ui/InfiniteScrollToggle'
import { useViewport } from '../../app/useViewport'
import FactorDetailDrawer from './factors/FactorDetailDrawer'
import type { PublishStepItem } from './factors/FactorDetailDrawer'
import { useFactorColumns, latestVersion, scoreOf } from './factors/FactorTable'
import { stepResultLabel } from './validation/PublishStepsPanel'
import ResearchFlowBar from './shared/ResearchFlowBar'
import { useListInfinite, ListLoadMeta, ListInfiniteSentinel } from './shared/ListInfinite'
import { toast } from './shared/toast'
import './factors/factors.css'

const STATUS_FILTERS = [
  { label: '全部', value: 'all' },
  { label: '草稿', value: 'draft' },
  { label: '候选', value: 'candidate' },
  { label: '已发布', value: 'published' },
  { label: '已归档', value: 'archived' },
]

const INF_KEY = 'sr-research-factors-inf'

export default function FactorLibrary() {
  const navigate = useNavigate()
  const viewport = useViewport()
  const isMobile = viewport === 'mobile'
  const [factors, setFactors] = useState<FactorInfo[]>([])
  const [steps, setSteps] = useState<PublishStepItem[]>([])
  const [loading, setLoading] = useState(false)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [filter, setFilter] = useState('all')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [advancingKey, setAdvancingKey] = useState<string | null>(null)
  /** 撤销 in-flight 防护（advancingKey 同款）：请求未返回时阻塞并发撤销，经 advancing 合并展示忙碌态 */
  const [revertingKey, setRevertingKey] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setLoadErr(null)
    try {
      const [f, s] = await Promise.all([getFactors(), getPublishSteps()])
      setFactors(f)
      setSteps(s as PublishStepItem[])
    } catch (e: any) {
      setLoadErr('因子库加载失败: ' + (e?.message ?? e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const stepName = (key: string) => steps.find((s) => s.key === key)?.name ?? key

  const advance = async (v: FactorVersion, step: string) => {
    setAdvancingKey(`${v.id}:${step}`)
    try {
      const updated = await advanceVersion(v.id, step)
      toast.success(`步骤「${stepName(step)}」完成 → ${stepResultLabel(updated.status)}`)
      await load()
    } catch (e: any) {
      toast.error('推进失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
    } finally {
      setAdvancingKey(null)
    }
  }

  const revert = async (v: FactorVersion, step: string) => {
    if (revertingKey != null) return // in-flight 守卫：防重复撤销并发请求
    setRevertingKey(`${v.id}:${step}`)
    try {
      const updated = await api.post(`/factors/versions/${v.id}/revert`, { step })
      toast.success(`已撤销: ${stepName(step)} → ${stepResultLabel(updated.data.status)}`)
      await load()
    } catch (e: any) {
      toast.error('撤销失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
    } finally {
      setRevertingKey(null)
    }
  }

  const filtered = useMemo(
    () => (filter === 'all' ? factors : factors.filter((f) => f.status === filter)),
    [factors, filter],
  )
  // L0 结论条（05 §5.5 因子库）：因子总数 = factors.length；评分≥3 = 最新版本 scoreOf >= 3（与表格「库评分」列同口径）
  const scoredCount = useMemo(
    () => factors.filter((f) => scoreOf(latestVersion(f)) >= 3).length,
    [factors],
  )
  const inf = useListInfinite(filtered.length, INF_KEY, 20)

  const selected = selectedId != null ? factors.find((f) => f.id === selectedId) ?? null : null

  // useCallback 稳定引用：useFactorColumns 内部 useMemo 依赖 onEval/onBacktest，引用不稳定则列每次重建
  const toEval = useCallback((f: FactorInfo) =>
    navigate(`/research/evaluation?expr=${encodeURIComponent(f.expression)}&ds=${f.dataset_id}`),
  [navigate])
  const toBacktest = useCallback((f: FactorInfo) =>
    navigate(`/research/backtests?expr=${encodeURIComponent(f.expression)}&ds=${f.dataset_id}&from=factors`),
  [navigate])

  const columns = useFactorColumns(
    toEval,
    toBacktest,
  )

  return (
    <PageShell fill={!inf.on} className="sr-sticky-tools">
      <ResearchFlowBar current="factors" stepsHidden />
      {/* L0 结论条（05 §5.5 因子库）：因子总数 · 评分≥3；列表未到时骨架 */}
      <StatStrip
        loading={factors.length === 0}
        scrollable={isMobile}
        style={{ marginBottom: 'var(--sr-gap-row)' }}
        items={[
          { key: 'total', label: '因子总数', value: factors.length },
          { key: 'scored', label: '评分≥3', value: scoredCount, tone: scoredCount > 0 ? 'up' : 'plain' },
        ]}
      />
      <Toolbar sticky tail={
        <>
          <Text span c="dimmed">共 {filtered.length} 个因子</Text>
          <InfiniteScrollToggle checked={inf.on} onChange={inf.setOn} storageKey={INF_KEY} />
        </>
      }>
        <Text fw={600} style={{ fontSize: 'var(--sr-font-xs)' }}>状态筛选</Text>
        <SegmentedControl
          size="xs"
          value={filter}
          onChange={setFilter}
          data={STATUS_FILTERS}
        />
      </Toolbar>
      {loadErr ? (
        <EmptyState text={loadErr} onRetry={() => void load()} />
      ) : filtered.length > 0 ? (
        <>
          <DataTable<FactorInfo>
            rowKey="id"
            loading={loading}
            columns={columns}
            dataSource={inf.on ? filtered.slice(0, inf.shown) : filtered}
            pagination={inf.on ? false : { pageSize: 20, showTotal: (t) => `共 ${t} 个因子` }}
            sticky={!inf.on}
            grow={!inf.on}
            fillWidth
            onRow={(f) => ({
              onClick: () => setSelectedId(f.id),
              style: { cursor: 'pointer' },
            })}
          />
          <ListLoadMeta on={inf.on} shown={inf.shown} total={inf.total} />
          <ListInfiniteSentinel on={inf.on} hasMore={inf.hasMore} loadMore={inf.loadMore} resetKey={filter} />
        </>
      ) : (
        <EmptyState description="当前筛选下没有因子" />
      )}
      <FactorDetailDrawer
        factor={selected}
        steps={steps}
        open={selectedId != null}
        onClose={() => setSelectedId(null)}
        onAdvance={advance}
        onRevert={revert}
        onEval={toEval}
        onBacktest={toBacktest}
        readOnly={isMobile}
        advancing={advancingKey != null || revertingKey != null}
      />
    </PageShell>
  )
}
