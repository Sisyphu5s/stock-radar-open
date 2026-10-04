import { Button, Checkbox, Drawer, Flex, Group, Loader, Text } from '@mantine/core'
import { IconChartLine, IconFlask, IconRocket, IconRefresh } from '@tabler/icons-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { SrColumn } from '../../../components/ui/tableTypes'
import type { FactorInfo, FactorVersion } from '../../../api/client'
import { getFactors } from '../../../api/client'
import type { FactorAttributionResult, FactorCorrelationResult, FactorIcDecayResult } from '../../../api/factorAnalysis'
import { getFactorAttribution, getFactorCorrelation, getFactorIcDecay } from '../../../api/factorAnalysis'
import type { PanelRebuildProgress } from '../../../api/client'
import DataTable from '../../../components/ui/DataTable'
import FormulaText from '../../../components/ui/FormulaText'
import EmptyState from '../../../components/ui/EmptyState'
import InfiniteScrollToggle from '../../../components/ui/InfiniteScrollToggle'
import IconTextButton from '../../../components/ui/IconTextButton'
import { MetricStat } from '../../../components/ui'
import PublishStepsPanel, { PublishFlowStatus, PUBLISH_STEP_KEYS, VersionStatusTag } from '../validation/PublishStepsPanel'
import { useListInfinite, ListLoadMeta, ListInfiniteSentinel } from '../shared/ListInfinite'
import { fmtRatio, fmtStab } from '../../../utils/format'
import { latestVersion, scoreOf } from './FactorTable'
import { useDensityStore } from '../../../stores/useAppStore'
import { chartTheme, chartYAxis } from '../../../utils/echartsTheme'
import { echarts } from '../../../utils/echartsSetup'
import { useThemeStore } from '../../../stores/useAppStore'
import { useEchartsLifecycle } from '../../../hooks/useEchartsLifecycle'

const VERS_INF_KEY = 'sr-research-vers-inf'

export interface PublishStepItem {
  key: string
  name: string
  desc: string
}

interface FactorDetailDrawerProps {
  factor: FactorInfo | null
  steps: PublishStepItem[]
  open: boolean
  onClose: () => void
  onAdvance: (v: FactorVersion, step: string) => void
  onRevert: (v: FactorVersion, step: string) => void
  onEval: (f: FactorInfo) => void
  onBacktest: (f: FactorInfo) => void
  /** 移动端只读：隐藏发布/撤销操作 */
  readOnly?: boolean
  /** 步骤推进中（按钮 loading） */
  advancing?: boolean
}

const versionColumns: SrColumn<FactorVersion>[] = [
  { title: '版本', dataIndex: 'version', align: 'center' },
  {
    title: '表达式', dataIndex: 'expression', ellipsis: true, minWidth: 120,
    render: (_, row) => <FormulaText tex={row.latex} expr={row.expression} size="small" />,
  },
  { title: '复杂度', dataIndex: 'complexity', align: 'center' },
  { title: 'Train IC', dataIndex: 'train_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => fmtRatio(v as number | null, 4) },
  { title: 'Val IC', dataIndex: 'val_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => fmtRatio(v as number | null, 4) },
  { title: 'OOS IC', dataIndex: 'oos_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => fmtRatio(v as number | null, 4) },
  { title: '稳定性', dataIndex: 'stability', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => fmtStab(v as number | null) },
  {
    title: '状态', align: 'center',
    render: (_, row) => <VersionStatusTag status={row.status} />,
  },
]

/** 因子详情 Drawer / Inspector：指标摘要 + 版本历史 + 真实三步发布 + 生命周期分析(T-09) */
export default function FactorDetailDrawer(p: FactorDetailDrawerProps) {
  const { factor } = p
  const v = factor ? latestVersion(factor) : undefined
  const sc = scoreOf(v)
  const versInf = useListInfinite(factor?.versions.length ?? 0, VERS_INF_KEY, 10)
  const density = useDensityStore((s) => s.density)
  const ctlSize = density === 'compact' ? 'small' : 'middle'

  // ===== T-09/T-67 因子分析：仅已发布(主记录)且非 NN 因子展示；分析区平铺渲染(三切面纵向平铺,无 Tabs) =====
  const isDark = useThemeStore((s) => s.theme) === 'dark'
  const canAnalyze = !!factor && factor.kind !== 'nn' && factor.status === 'published' && !!factor.expression
  const [anErr, setAnErr] = useState<string | null>(null)
  // 相关性:候选因子集(已发布表达式因子)+ 选中集
  const [allFactors, setAllFactors] = useState<FactorInfo[]>([])
  const [selected, setSelected] = useState<number[]>([])
  const [corr, setCorr] = useState<FactorCorrelationResult | null>(null)
  const [corrLoading, setCorrLoading] = useState(false)
  const [decay, setDecay] = useState<FactorIcDecayResult | null>(null)
  const [decayLoading, setDecayLoading] = useState(false)
  const [attr, setAttr] = useState<FactorAttributionResult | null>(null)
  const [attrLoading, setAttrLoading] = useState(false)
  // 因子分析冷缓存重建进度（C11b）：后端 202 受理 → panel_build 轮询进度
  const [rebuild, setRebuild] = useState<PanelRebuildProgress | null>(null)

  // 已发布表达式因子(相关性候选集):打开分析区时惰性拉取一次
  useEffect(() => {
    if (!p.open || !canAnalyze || allFactors.length) return
    getFactors()
      .then((fs) => {
        setAllFactors(fs)
        setSelected(fs.filter((f) => f.kind !== 'nn' && f.status === 'published').map((f) => f.id))
      })
      .catch(() => setAnErr('因子列表加载失败，相关性分析不可用'))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.open, canAnalyze])

  const runCorrelation = useCallback(async () => {
    if (!factor) return
    if (selected.length < 2) {
      setAnErr('相关性分析至少需要 2 个因子')
      return
    }
    setCorrLoading(true)
    setAnErr(null)
    try {
      // 冷缓存 202 → 内部轮询 panel_build（1s/120s）→ 完成后重取热数据（C11b）
      const r = await getFactorCorrelation({ factor_ids: selected, dataset_id: factor.dataset_id }, (job) => setRebuild(job))
      setCorr(r)
    } catch (e: any) {
      setAnErr('相关性分析失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
    } finally {
      setCorrLoading(false)
      setRebuild(null)
    }
  }, [factor, selected])

  // 首次进入分析区:候选集就绪且未计算过时自动计算一次(平铺后无 tab 语义,打开即算)
  useEffect(() => {
    if (!p.open || corr || !allFactors.length || selected.length < 2 || corrLoading) return
    void runCorrelation()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.open, allFactors, selected, corr])

  // IC 衰减 / 风格归因:平铺后打开 Drawer 即请求(数据已算则跳过;失败不自动重试,关闭重开或切因子可重试)
  useEffect(() => {
    if (!p.open || decay || !factor) return
    setDecayLoading(true)
    setAnErr(null)
    // 冷缓存 202 → 内部轮询 panel_build（1s/120s）→ 完成后重取热数据（C11b）
    getFactorIcDecay({ expr: factor.expression, dataset_id: factor.dataset_id }, (job) => setRebuild(job))
      .then(setDecay)
      .catch((e: any) => setAnErr('IC 衰减分析失败: ' + (e?.response?.data?.detail ?? e?.message ?? e)))
      .finally(() => { setDecayLoading(false); setRebuild(null) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.open, factor])

  useEffect(() => {
    if (!p.open || attr || !factor) return
    setAttrLoading(true)
    setAnErr(null)
    // 冷缓存 202 → 内部轮询 panel_build（1s/120s）→ 完成后重取热数据（C11b）
    getFactorAttribution({ expr: factor.expression, dataset_id: factor.dataset_id }, (job) => setRebuild(job))
      .then(setAttr)
      .catch((e: any) => setAnErr('风格归因失败: ' + (e?.response?.data?.detail ?? e?.message ?? e)))
      .finally(() => { setAttrLoading(false); setRebuild(null) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.open, factor])

  // ===== echarts 生命周期(容器挂载/尺寸/卸载) =====
  const corrChartRef = useRef<HTMLDivElement>(null)
  const corrChart = useRef<echarts.ECharts | null>(null)
  const decayChartRef = useRef<HTMLDivElement>(null)
  const decayChart = useRef<echarts.ECharts | null>(null)
  const attrChartRef = useRef<HTMLDivElement>(null)
  const attrChart = useRef<echarts.ECharts | null>(null)
  // 惰性 init 标记:记录已 init 绑定的容器,容器条件渲染重建时重挂(避免旧实例残留;risk.tsx 同款模式)
  const corrInitedEl = useRef<HTMLDivElement | null>(null)
  const decayInitedEl = useRef<HTMLDivElement | null>(null)
  const attrInitedEl = useRef<HTMLDivElement | null>(null)
  useEchartsLifecycle(corrChartRef, corrChart)
  useEchartsLifecycle(decayChartRef, decayChart)
  useEchartsLifecycle(attrChartRef, attrChart)

  // 释放三个分析图表实例(关闭时防隐藏容器宽度 0 导致 init 尺寸失真;卸载兜底)
  const disposeAnalysisCharts = useCallback(() => {
    const charts = [corrChart, decayChart, attrChart]
    const els = [corrInitedEl, decayInitedEl, attrInitedEl]
    for (let i = 0; i < charts.length; i++) {
      const c = charts[i].current
      if (c && !c.isDisposed()) c.dispose()
      charts[i].current = null
      els[i].current = null
    }
  }, [])

  useEffect(() => {
    if (p.open) return
    disposeAnalysisCharts()
  }, [p.open, disposeAnalysisCharts])

  useEffect(() => disposeAnalysisCharts, [disposeAnalysisCharts])

  // 相关性热力图(matrix n×n,颜色 = 相关 -1~1;chart 未 init 时无操作,init 由 IO 触发)
  const renderCorr = useCallback(() => {
    const chart = corrChart.current
    if (!chart || !corr) return
    const t = chartTheme(isDark)
    const data: [number, number, number][] = []
    corr.matrix.forEach((row, i) => row.forEach((cell, j) => {
      if (cell != null && Number.isFinite(cell)) data.push([j, i, cell])
    }))
    chart.setOption({
      animation: false,
      tooltip: {
        position: 'top',
        formatter: (params: any) => `${corr.names[params.data[1]]} × ${corr.names[params.data[0]]}<br/>相关: ${params.data[2].toFixed(3)}`,
      },
      grid: { left: 90, right: 10, top: 10, bottom: 60 },
      xAxis: { type: 'category', data: corr.names, axisLabel: { fontSize: 9, rotate: 35, color: t.text3 } },
      yAxis: { type: 'category', data: [...corr.names].reverse(), axisLabel: { fontSize: 9, color: t.text3 } },
      visualMap: {
        min: -1, max: 1, calculable: false, orient: 'horizontal', left: 'center', bottom: 0,
        itemHeight: 60, textStyle: { color: t.text3, fontSize: 9 },
        inRange: { color: [t.down, t.bg, t.up] },
      },
      series: [{
        type: 'heatmap', data,
        label: { show: true, fontSize: 9, color: t.text2, formatter: (params: any) => params.data[2].toFixed(2) },
        itemStyle: { borderColor: t.bg, borderWidth: 1 },
        emphasis: { itemStyle: { shadowBlur: 6, shadowColor: 'rgba(0,0,0,0.3)' } },
      }],
    }, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [corr, isDark])

  // IC 衰减曲线(chart 未 init 时无操作,init 由 IO 触发)
  const renderDecay = useCallback(() => {
    const chart = decayChart.current
    if (!chart || !decay) return
    const t = chartTheme(isDark)
    chart.setOption({
      animation: false,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        valueFormatter: (val: number) => fmtRatio(val, 4),
      },
      grid: { left: 45, right: 16, top: 20, bottom: 28 },
      xAxis: { type: 'category', data: decay.horizons.map((h) => `${h} 日`), axisLabel: { fontSize: 10, color: t.text3 } },
      yAxis: chartYAxis(isDark),
      series: [{
        name: 'IC', type: 'line', data: decay.horizons.map((h, i) => [String(h), decay.ic_means[i]]),
        showSymbol: true, symbolSize: 6,
        lineStyle: { width: 1.6, color: t.accent },
        itemStyle: { color: t.accent },
        markLine: {
          silent: true, symbol: 'none',
          lineStyle: { type: 'dashed', color: t.up },
          data: [{ yAxis: 0 }],
          label: { show: false },
        },
      }],
    }, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [decay, isDark])

  // 行业暴露柱状图(chart 未 init 时无操作,init 由 IO 触发)
  const renderAttr = useCallback(() => {
    const chart = attrChart.current
    if (!chart || !attr) return
    const t = chartTheme(isDark)
    const rows = Object.entries(attr.industry_exposures).filter(([, val]) => val != null)
    chart.setOption({
      animation: false,
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        valueFormatter: (val: number) => fmtRatio(val, 4),
      },
      grid: { left: 8, right: 16, top: 12, bottom: 52 },
      xAxis: {
        type: 'category', data: rows.map(([name]) => name),
        axisLabel: { fontSize: 9, rotate: 40, interval: 0, color: t.text3 },
      },
      yAxis: chartYAxis(isDark),
      series: [{
        name: '行业暴露', type: 'bar', barMaxWidth: 18,
        data: rows.map(([, val]) => val),
        itemStyle: { color: t.accent },
      }],
    }, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attr, isDark])

  // ===== 惰性 init(risk.tsx 同款):进入视口才 echarts.init,离开视口不销毁仅停用,再次进入只重绘不重建。
  // 平铺后三块图表纵向排列,下方图表初始在视口外;容器为条件渲染(数据到达后出现),
  // effect 依赖数据确保容器存在时注册 IO;IntersectionObserver 兜底同步 init =====
  const ensureCorrInit = useCallback(() => {
    const el = corrChartRef.current
    if (!el) return
    if (corrInitedEl.current !== el) {
      if (corrChart.current) corrChart.current.dispose()
      corrChart.current = echarts.getInstanceByDom(el) ?? echarts.init(el)
      corrInitedEl.current = el
    }
    renderCorr()
  }, [renderCorr])

  useEffect(() => {
    const el = corrChartRef.current
    if (!el) return
    if (typeof IntersectionObserver === 'undefined') { ensureCorrInit(); return }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) ensureCorrInit()
    })
    io.observe(el)
    return () => io.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [corr])

  const ensureDecayInit = useCallback(() => {
    const el = decayChartRef.current
    if (!el) return
    if (decayInitedEl.current !== el) {
      if (decayChart.current) decayChart.current.dispose()
      decayChart.current = echarts.getInstanceByDom(el) ?? echarts.init(el)
      decayInitedEl.current = el
    }
    renderDecay()
  }, [renderDecay])

  useEffect(() => {
    const el = decayChartRef.current
    if (!el) return
    if (typeof IntersectionObserver === 'undefined') { ensureDecayInit(); return }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) ensureDecayInit()
    })
    io.observe(el)
    return () => io.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [decay])

  const ensureAttrInit = useCallback(() => {
    const el = attrChartRef.current
    if (!el) return
    if (attrInitedEl.current !== el) {
      if (attrChart.current) attrChart.current.dispose()
      attrChart.current = echarts.getInstanceByDom(el) ?? echarts.init(el)
      attrInitedEl.current = el
    }
    renderAttr()
  }, [renderAttr])

  useEffect(() => {
    const el = attrChartRef.current
    if (!el) return
    if (typeof IntersectionObserver === 'undefined') { ensureAttrInit(); return }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) ensureAttrInit()
    })
    io.observe(el)
    return () => io.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attr])

  // 数据/主题变化:已 init 则重绘(首次 init 由 IO 触发时携带当前数据)
  useEffect(() => {
    if (corrInitedEl.current) renderCorr()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [renderCorr])

  useEffect(() => {
    if (decayInitedEl.current) renderDecay()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [renderDecay])

  useEffect(() => {
    if (attrInitedEl.current) renderAttr()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [renderAttr])

  return (
    <Drawer
      className="sr-factor-drawer"
      title={
        factor ? (
          <Group wrap="wrap" gap={8}>
            <IconRocket size={18} style={{ color: 'var(--sr-accent)' }} />
            <Text fw={600} style={{ fontSize: 'var(--sr-font-head)' }}>{factor.name}</Text>
            {v && <VersionStatusTag status={v.status} />}
            <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>因子 #{factor.id}</Text>
          </Group>
        ) : '因子详情'
      }
      opened={p.open}
      onClose={p.onClose}
      size="min(560px, 96vw)"
      position="right"
    >
      {!factor ? (
        <EmptyState description="请选择因子查看详情" />
      ) : (
        <>
          {factor.description && (
            <div style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-2)', marginBottom: 'var(--sr-pad-xl)' }}>
              {factor.description}
            </div>
          )}

          <div className="sr-run-panel">
            <div className="sr-run-panel-title">指标摘要</div>
            {/* 页面级「统计数字」统一 20px/700（MetricStat 基板承担，与 FactorMining metaView 对齐） */}
            <Flex wrap="wrap" gap="var(--sr-pad-lg)" className="sr-factor-stats">
              <MetricStat label="Train IC" value={fmtRatio(v?.train_ic, 4)} color="var(--sr-text-1)" />
              <MetricStat label="Val IC" value={fmtRatio(v?.val_ic, 4)} color="var(--sr-text-1)" />
              <MetricStat label="OOS IC" value={fmtRatio(v?.oos_ic, 4)} color="var(--sr-text-1)" />
              <MetricStat label="稳定性" value={fmtStab(v?.stability)} color="var(--sr-text-1)" />
              <MetricStat label="复杂度" value={v?.complexity == null ? '—' : String(v.complexity)} color="var(--sr-text-1)" />
              <MetricStat label="库评分" value={sc.toFixed(2)} color={sc >= 3 ? 'var(--sr-up)' : 'var(--sr-text-1)'} />
            </Flex>
          </div>

          <div className="sr-run-panel">
            <div className="sr-run-panel-title">
              发布流程管理
              <Text span c="dimmed" style={{ fontWeight: 400, marginLeft: 'var(--sr-pad-md)', fontSize: 'var(--sr-font-sm)' }}>
                {v ? `最新版本 v${v.version} · 已完成 ${PUBLISH_STEP_KEYS.filter((k) => v[k as keyof FactorVersion]).length}/3` : '暂无版本'}
              </Text>
            </div>
            {v ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-pad-lg)' }}>
                <PublishStepsPanel
                  steps={p.steps}
                  version={v}
                  advancing={p.advancing}
                  onAdvance={(step) => p.onAdvance(v, step)}
                  onRevert={p.readOnly ? undefined : (step) => p.onRevert(v, step)}
                  readOnly={p.readOnly}
                />
                <PublishFlowStatus version={v} steps={p.steps} />
              </div>
            ) : (
              <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
                暂无版本，请先在「因子评估」创建或提交版本。
              </Text>
            )}
            {p.readOnly && (
              <Text span c="dimmed" style={{ display: 'block', marginTop: 'var(--sr-pad-md)', fontSize: 'var(--sr-font-sm)' }}>
                发布操作不可用（只读模式）
              </Text>
            )}
          </div>

          <div className="sr-run-panel">
            <div className="sr-run-panel-title">
              版本历史
              <Text span c="dimmed" style={{ fontWeight: 400, marginLeft: 'var(--sr-pad-md)', fontSize: 'var(--sr-font-sm)' }}>
                {factor.versions.length}
              </Text>
              <span className="sr-toolbar-tail">
                <InfiniteScrollToggle checked={versInf.on} onChange={versInf.setOn} storageKey={VERS_INF_KEY} />
              </span>
            </div>
            <DataTable
              rowKey="id"
              columns={versionColumns}
              dataSource={versInf.on ? factor.versions.slice(0, versInf.shown) : factor.versions}
              pagination={false}
            />
            <ListLoadMeta on={versInf.on} shown={versInf.shown} total={versInf.total} />
            <ListInfiniteSentinel on={versInf.on} hasMore={versInf.hasMore} loadMore={versInf.loadMore} resetKey={factor.id} />
          </div>

          {canAnalyze && (
            <div className="sr-run-panel">
              <div className="sr-run-panel-title">因子分析</div>
              {/* T-67 平铺：相关性 / IC 衰减 / 风格归因 三切面纵向平铺、各自小节标题（原 Tabs 壳删除）；
                  图表容器固定高度（--sr-chart-h-sm 令牌，factors.css）+ IO 惰性 init（视口内才 echarts.init） */}
              <div style={{ marginTop: 'var(--sr-pad-md)' }}>
                <Text fw={600} size="sm" style={{ color: 'var(--sr-text-1)', marginBottom: 'var(--sr-pad-sm)' }}>相关性</Text>
                {allFactors.length === 0 && !anErr ? (
                  <Loader size="sm" />
                ) : (
                  <>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-pad-xs)' }}>
                      <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
                        选择参与相关性分析的已发布因子（共 {allFactors.filter((f) => f.kind !== 'nn' && f.status === 'published').length} 个）
                      </Text>
                      <div style={{ maxHeight: 130, overflowY: 'auto', border: '1px solid var(--sr-border)', borderRadius: 6, padding: 'var(--sr-pad-xs)' }}>
                        <Checkbox.Group value={selected.map(String)} onChange={(vals) => setSelected(vals.map(Number))}>
                          <Flex direction="column" gap={4}>
                            {allFactors
                              .filter((f) => f.kind !== 'nn' && f.status === 'published')
                              .map((f) => (
                                <Checkbox key={f.id} size="xs" value={String(f.id)} label={<Text span style={{ fontSize: 'var(--sr-font-xs)' }}>{f.name}</Text>} />
                              ))}
                          </Flex>
                        </Checkbox.Group>
                      </div>
                    </div>
                    <Group gap={8} style={{ marginTop: 'var(--sr-pad-md)' }}>
                      <Button size="xs" leftSection={<IconRefresh size={14} />} loading={corrLoading} onClick={() => void runCorrelation()}>
                        计算相关性
                      </Button>
                    </Group>
                    {corr && (
                      <>
                        <div ref={corrChartRef} className="sr-factor-an-chart" />
                        <div className="sr-factor-an-cluster">
                          <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
                            冗余分组（单链，|相关系数| ≥ {corr.threshold} 归组）
                          </Text>
                          {corr.clusters.filter((c) => c.members.length > 1).length > 0 ? (
                            corr.clusters.filter((c) => c.members.length > 1).map((c, i) => (
                              <Text key={i} size="xs" style={{ color: 'var(--sr-text-1)' }}>
                                组 {i + 1}: {c.members.join(' / ')}
                              </Text>
                            ))
                          ) : (
                            <Text size="xs" c="dimmed">未发现冗余分组</Text>
                          )}
                        </div>
                      </>
                    )}
                  </>
                )}
              </div>

              <div style={{ marginTop: 'var(--sr-pad-xl)' }}>
                <Text fw={600} size="sm" style={{ color: 'var(--sr-text-1)', marginBottom: 'var(--sr-pad-sm)' }}>IC 衰减</Text>
                {decayLoading && !decay ? <Loader size="sm" /> : decay && (
                  <>
                    <Flex wrap="wrap" gap="var(--sr-pad-lg)">
                      <MetricStat
                        label="IC 半衰期"
                        value={decay.half_life != null ? `${decay.half_life} 日` : '—'}
                        sub="|IC| 首次跌破峰值一半的持有期"
                        color="var(--sr-text-1)"
                      />
                      <MetricStat
                        label="IC 峰值"
                        value={fmtRatio(Math.max(...decay.ic_means.filter((m): m is number => m != null), 0))}
                        sub="各持有期 |IC| 最大值"
                        color="var(--sr-text-1)"
                      />
                    </Flex>
                    <div ref={decayChartRef} className="sr-factor-an-chart" />
                  </>
                )}
              </div>

              <div style={{ marginTop: 'var(--sr-pad-xl)' }}>
                <Text fw={600} size="sm" style={{ color: 'var(--sr-text-1)', marginBottom: 'var(--sr-pad-sm)' }}>风格归因</Text>
                {attrLoading && !attr ? <Loader size="sm" /> : attr && (
                  <>
                    <Flex wrap="wrap" gap="var(--sr-pad-lg)">
                      <MetricStat
                        label="市值暴露"
                        value={fmtRatio(attr.size_exposure, 4)}
                        sub="控制行业后市值回归系数"
                        color="var(--sr-text-1)"
                      />
                      <MetricStat
                        label="残差 IC"
                        value={fmtRatio(attr.residual_ic, 4)}
                        sub={`剥离行业/市值后的 IC（${attr.n_periods} 期）`}
                        color="var(--sr-text-1)"
                      />
                    </Flex>
                    {Object.keys(attr.industry_exposures).length > 0 && (
                      <div ref={attrChartRef} className="sr-factor-an-chart" />
                    )}
                  </>
                )}
              </div>
              {anErr && (
                <Text span c="red" style={{ display: 'block', marginTop: 'var(--sr-pad-md)', fontSize: 'var(--sr-font-sm)' }}>
                  {anErr}
                </Text>
              )}
              {rebuild && (
                <Text span c="dimmed" style={{ display: 'block', marginTop: 'var(--sr-pad-md)', fontSize: 'var(--sr-font-sm)' }}>
                  面板冷缓存重建中（任务 #{rebuild.id}）: {rebuild.phase || rebuild.status}
                </Text>
              )}
            </div>
          )}

          {/* 原 antd Drawer footer：操作按钮置于内容末尾 */}
          <Group gap={8} style={{ marginTop: 'var(--sr-pad-lg)' }}>
            <IconTextButton size={ctlSize} icon={<IconFlask size={16} />} text="去评估" onClick={() => p.onEval(factor)} />
            <IconTextButton size={ctlSize} icon={<IconChartLine size={16} />} text="去回测" onClick={() => p.onBacktest(factor)} />
          </Group>
        </>
      )}
    </Drawer>
  )
}
