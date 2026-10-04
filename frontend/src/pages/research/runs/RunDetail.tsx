import { Badge, Group, Text, Tooltip } from '@mantine/core'
import { IconCalendarClock, IconChartLine, IconFlask, IconFunction } from '@tabler/icons-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getJob } from '../../../api/client'
import { useDatasets, useJobState, invalidateJob } from '../../../data/jobs'
import type { FactorResult } from '../../../api/client'
import type { SrColumn } from '../../../components/ui/tableTypes'
import CardState from '../../../components/ui/CardState'
import DataTable from '../../../components/ui/DataTable'
import EmptyState from '../../../components/ui/EmptyState'
import PageShell from '../../../components/ui/PageShell'
import TaskProgress from '../../../components/ui/TaskProgress'
import CardShell from '../../../components/ui/CardShell'
import IconTextButton from '../../../components/ui/IconTextButton'
import InfiniteScrollToggle from '../../../components/ui/InfiniteScrollToggle'
import { fmtRatio } from '../../../utils/format'
import { chartDataZoom, chartTheme, chartYAxis } from '../../../utils/echartsTheme'
import { useThemeStore } from '../../../stores/useAppStore'
import { echarts } from '../../../utils/echartsSetup'
import ResearchFlowBar from '../shared/ResearchFlowBar'
import { useListInfinite, ListLoadMeta, ListInfiniteSentinel } from '../shared/ListInfinite'

const EVO_INF_KEY = 'sr-research-gp-evo-inf'

const TARGET_LABELS: Record<string, string> = {
  ic: 'IC（预测方向）',
  ic_abs: '|IC|（方向无关）',
  icir: 'ICIR（稳健性）',
  ls_annual: '多空年化',
  composite: '综合评分',
}

interface EvolutionPoint {
  gen: number
  best_train_ic?: number | null
  avg_ic?: number | null
  best_expression?: string
  best_complexity?: number
}

interface GpRunResult {
  results: FactorResult[]
  evolution?: EvolutionPoint[]
  meta?: Record<string, unknown>
}

const MetricText = ({ v }: { v: number | null | undefined }) => {
  if (v === null || v === undefined || Number.isNaN(v)) return <Text c="dimmed">—</Text>
  return <span style={{ fontSize: 'var(--sr-font-sm)', color: v > 0 ? 'var(--sr-up)' : v < 0 ? 'var(--sr-down)' : undefined }}>{fmtRatio(v)}</span>
}

/** /research/runs/:id 只读详情：任务状态 / 进化曲线 / 表达式演化 / 最终结果，含错误与空态。 */
export default function RunDetail({ jobId }: { jobId: number }) {
  const navigate = useNavigate()
  const isDark = useThemeStore((s) => s.theme) === 'dark'
  const jobQ = useJobState(Number.isFinite(jobId) && jobId > 0 ? jobId : undefined)
  const job = jobQ.value
  // T-128:查询失败(404 之外的网络/500)不再被压成 undefined 永久转圈——15s 轮询自动重试,UI 给错误态+手动重试
  const loadFailed = jobQ.error != null && job === undefined
  const datasets = (useDatasets() ?? []) as { id: number; name?: string }[]
  const [gone, setGone] = useState(false)
  const chartRef = useRef<HTMLDivElement>(null)
  const chart = useRef<echarts.ECharts | null>(null)

  // 一次性存在性检查：任务不存在（404）→ 错误空态，避免轮询层静默挂起
  useEffect(() => {
    let cancelled = false
    setGone(false)
    if (!Number.isFinite(jobId) || jobId <= 0) return
    getJob(jobId).catch((e) => {
      if (cancelled) return
      if (e?.response?.status === 404) setGone(true)
    })
    return () => { cancelled = true }
  }, [jobId])

  const evolution = (job?.result as unknown as GpRunResult | undefined)?.evolution ?? []
  const results: FactorResult[] = job?.result?.results ?? []
  const p = job?.params ?? {}
  const status = job?.status
  const done = status === 'done'
  const failed = status === 'failed'
  const running = status === 'pending' || status === 'running'
  const evoInf = useListInfinite(evolution.length, EVO_INF_KEY, 20)
  const evoList = [...evolution].reverse()

  useEffect(() => {
    if (!evolution.length || !chartRef.current) return
    if (!chart.current) chart.current = echarts.getInstanceByDom(chartRef.current) ?? echarts.init(chartRef.current)
    const t = chartTheme(isDark)
    chart.current.setOption({
      animation: false,
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, valueFormatter: (v: number) => fmtRatio(v, 3) },
      legend: { top: 0, textStyle: { fontSize: 11, color: t.text2 } },
      grid: { left: 45, right: 16, top: 28, bottom: 24 },
      dataZoom: chartDataZoom(isDark),
      xAxis: { type: 'category', data: evolution.map((e) => e.gen), axisLabel: { fontSize: 10, hideOverlap: true } },
      yAxis: chartYAxis(isDark),
      series: [
        {
          name: '最佳训练 IC', type: 'line',
          data: evolution.map((e) => e.best_train_ic),
          showSymbol: false, lineStyle: { width: 1.8, color: t.accent }, itemStyle: { color: t.accent },
        },
        {
          name: '平均 IC', type: 'line',
          data: evolution.map((e) => e.avg_ic),
          showSymbol: false, lineStyle: { width: 1.4, color: t.amber }, itemStyle: { color: t.amber },
        },
      ],
    }, true)
  }, [job, isDark])

  useEffect(() => () => {
    const inst = chartRef.current ? echarts.getInstanceByDom(chartRef.current) : null
    if (inst) inst.dispose()
    chart.current = null
  }, [])

  // 列定义 useMemo 稳定引用（DataTable 已 memo）：本页轮询 job 时无关 setState 不触发全表重渲染
  const columns = useMemo<SrColumn<FactorResult>[]>(() => [
    {
      title: '表达式', dataIndex: 'expression', ellipsis: true, minWidth: 120,
      render: (v) => <Tooltip label={v}><Text component="code" style={{ fontSize: 'var(--sr-font-sm)' }}>{v}</Text></Tooltip>,
    },
    { title: '训练 IC', dataIndex: 'train_ic', minWidth: 80, align: 'right', className: 'sr-num-col', render: (v) => <MetricText v={v} /> },
    { title: '验证 IC', dataIndex: 'val_ic', minWidth: 80, align: 'right', className: 'sr-num-col', render: (v) => <MetricText v={v} /> },
    { title: '复杂度', dataIndex: 'complexity', minWidth: 70, align: 'center' },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [])

  const failedErr = failed ? (job?.error ?? '任务失败') : null

  if (!Number.isFinite(jobId) || jobId <= 0) {
    return (
      <PageShell>
        {/* title 传空串：CardShell 结构统一（空 title 不渲染标题行内容） */}
        <CardShell title="">
          <EmptyState
            description="未指定任务，请从因子挖掘结果页进入"
            onRetry={() => navigate('/tasks')}
          />
        </CardShell>
      </PageShell>
    )
  }

  if (gone) {
    return (
      <PageShell>
        <CardShell title="">
          <EmptyState
            text={`任务 #${jobId} 不存在或已被删除`}
            onRetry={() => navigate('/tasks')}
          />
        </CardShell>
      </PageShell>
    )
  }

  if (loadFailed) {
    return (
      <PageShell>
        <CardShell title="">
          <EmptyState
            text={`任务 #${jobId} 加载失败（网络或服务异常，自动重试中）`}
            onRetry={() => { if (Number.isFinite(jobId) && jobId > 0) invalidateJob(jobId) }}
          />
        </CardShell>
      </PageShell>
    )
  }

  return (
    <PageShell>
      <ResearchFlowBar
        current="discovery"
        discoveryView="gp"
        dataset={Number(p.dataset_id) > 0 ? datasets.find((d) => d.id === Number(p.dataset_id)) ?? { id: Number(p.dataset_id) } : null}
        expression={results[0]?.expression ?? null}
        job={job ? { id: job.id, status: job.status } : null}
        onJob={() => navigate('/tasks')}
      />
      <CardShell icon={<IconFlask />} title={`任务 #${jobId}`}>
        <CardState loading={!job}>
          {job && (
            <>
              <TaskProgress
                job={{ id: job.id, job_type: 'gp_run', status: job.status, progress: job.progress ?? 0 }}
                typeLabel={{ gp_run: 'GP 进化' }}
                extra={<Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>{job.status}</Text>}
              />
              {failedErr && <Text c="red">{failedErr}</Text>}
              <div style={{ marginTop: 'var(--sr-pad-lg)' }}>
                <Group wrap="wrap" gap="var(--sr-pad-xs)">
                  <Badge color="blue" variant="light">数据集 #{String(p.dataset_id ?? '—')}</Badge>
                  <Badge color="cyan" variant="light">{TARGET_LABELS[String(p.target)] ?? String(p.target ?? '—')}</Badge>
                  <Badge color={p.penalty_complexity ? 'orange' : 'gray'} variant="light">
                    复杂度惩罚 {p.penalty_complexity ? '开' : '关'}
                  </Badge>
                  <Badge variant="light">种群 {String(p.pop_size ?? '—')}</Badge>
                  <Badge variant="light">代数 {String(p.generations ?? '—')}</Badge>
                  <Badge variant="light">周期 {String(p.horizon ?? '—')}日</Badge>
                  <Badge variant="light">Top {String(p.top_n ?? '—')}</Badge>
                  {Array.isArray(p.features) && <Badge variant="light">特征 {p.features.length} 个</Badge>}
                  {Boolean(p.algorithm) && <Badge variant="light">算法 {String(p.algorithm)}</Badge>}
                </Group>
              </div>
              <div style={{ marginTop: 'var(--sr-pad-lg)' }}>
                <IconTextButton icon={<IconCalendarClock />} text="回任务管理" onClick={() => navigate('/tasks')} />
              </div>
            </>
          )}
        </CardState>
      </CardShell>

      <CardShell icon={<IconChartLine />} title="进化过程（每代最佳训练 IC）">
        <CardState
          loading={running}
          error={failedErr}
          empty={done && !evolution.length}
          emptyDesc="该任务没有进化过程数据"
        >
          {evolution.length > 0 && (
            <div ref={chartRef} role="img" aria-label="每代最佳训练 IC 与平均 IC 进化曲线" className="sr-chart"
              style={{ height: 'clamp(var(--sr-chart-h-sm), 42vh, var(--sr-chart-h-lg))' }} />
          )}
        </CardState>
      </CardShell>

      <CardShell
        icon={<IconFunction />}
        title="表达式演化"
        extra={<InfiniteScrollToggle checked={evoInf.on} onChange={evoInf.setOn} storageKey={EVO_INF_KEY} />}
      >
        <CardState
          loading={running}
          error={failedErr}
          empty={done && !evolution.length}
          emptyDesc="暂无表达式演化记录"
        >
          {evolution.length > 0 && (
            <>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-pad-sm)' }}>
                {(evoInf.on ? evoList.slice(0, evoInf.shown) : evoList).map((evo, i) => (
                  <div key={evo.gen} style={{
                    display: 'flex', alignItems: 'center', gap: 'var(--sr-pad-lg)',
                    padding: 'var(--sr-pad-sm) var(--sr-pad-lg)', borderRadius: 'var(--sr-radius-card)',
                    background: i === 0 ? 'var(--sr-block-bg)' : 'transparent',
                    border: i === 0 ? '1px solid var(--sr-border)' : '1px solid transparent',
                  }}>
                    <Badge color="blue" variant="light" style={{ flexShrink: 0 }}>第 {evo.gen} 代</Badge>
                    <Tooltip label={evo.best_expression}>
                      <Text component="code" truncate style={{ flex: 1, fontSize: 'var(--sr-font-sm)', minWidth: 0 }}>{evo.best_expression}</Text>
                    </Tooltip>
                    <Text fw={600} style={{
                      flexShrink: 0,
                      color: (evo.best_train_ic ?? 0) > 0 ? 'var(--sr-up)' : (evo.best_train_ic ?? 0) < 0 ? 'var(--sr-down)' : undefined,
                    }}>
                      IC {fmtRatio(evo.best_train_ic)}
                    </Text>
                    <Badge variant="light" style={{ flexShrink: 0 }}>复杂度 {evo.best_complexity}</Badge>
                  </div>
                ))}
              </div>
              <ListLoadMeta on={evoInf.on} shown={evoInf.shown} total={evoInf.total} />
              <ListInfiniteSentinel on={evoInf.on} hasMore={evoInf.hasMore} loadMore={evoInf.loadMore} />
            </>
          )}
        </CardState>
      </CardShell>

      <CardShell title="最终结果（Top 因子）">
        <CardState
          loading={running}
          error={failedErr}
          empty={done && !results.length}
          emptyDesc="暂无因子结果"
        >
          <DataTable
            rowKey={(r) => r.expression}
            columns={columns}
            dataSource={results}
            pagination={false}
            fillWidth
          />
        </CardState>
      </CardShell>
    </PageShell>
  )
}
