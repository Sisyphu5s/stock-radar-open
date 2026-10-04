import { Alert, Button, Flex, Group, Text } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconArrowRight, IconChartBar, IconDownload, IconFileDownload, IconRocket } from '@tabler/icons-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { EvalResult, WalkForwardResult, WalkForwardWindow } from '../../../api/client'
import { fmtNum, fmtPct, fmtRatio, fmtStab } from '../../../utils/format'
import { cnTodayOf } from '../../../utils/time'
import { downloadDataUrl, downloadText } from '../../../utils/export'
import EmptyState from '../../../components/ui/EmptyState'
import DataTable from '../../../components/ui/DataTable'
import IconTextButton from '../../../components/ui/IconTextButton'
import MetricStat from '../../../components/ui/MetricStat'
import PublishStepsPanel, { PublishFlowStatus, VersionStatusTag } from '../validation/PublishStepsPanel'
import { chartDataZoom, chartTheme, chartYAxis } from '../../../utils/echartsTheme'
import { chartColors } from '../../../utils/chartTheme'
import { echarts } from '../../../utils/echartsSetup'
import { useThemeStore } from '../../../stores/useAppStore'
import { useEchartsLifecycle } from '../../../hooks/useEchartsLifecycle'
import ResultsTabs from '../shared/ResultsTabs'

export interface PublishStepItem {
  key: string
  name: string
  desc: string
}

interface EvaluationResultsProps {
  res: EvalResult | undefined
  oos: any
  /** T-03 Walk-forward 滚动验证结果(与 res 互斥:开关打开时提交 walk_forward 任务) */
  wf?: WalkForwardResult | undefined
  meta: any
  /** 参数已变更：结果基于旧参数，禁发布 */
  stale: boolean
  onRerun: () => void
  /** 用于匹配因子库的表达式的当前值（与提交/创建使用同一来源） */
  matchExpr: string
  factors: { id: number; name: string; expression: string; status: string; versions: any[] }[]
  pubSteps: PublishStepItem[]
  publishing: boolean
  advancing: boolean
  pubError: string | null
  onCreate: () => void
  onAdvance: (step: string) => Promise<void>
  onGoLibrary: () => void
  /** 去回测（携带当前表达式/数据集/周期） */
  onGoBacktest: () => void
  /** 只读模式（移动端）：发布操作隐藏 */
  readOnly?: boolean
}

/** MetricStat 网格单元（原 antd Col flex 用法） */
const statCell: React.CSSProperties = { flex: '1 1 140px', minWidth: 0 }

/**
 * 评估页右侧结果：发布入口（单一）+ 指标/图表/风险/分层 Tabs。
 * 图表 effect 依赖 activeTab（ResultsTabs onActiveChange 上报）：首次切到图表 tab 时
 * ref 已挂载、effect 重跑，保证 echarts 在容器有尺寸后 init（宽度 0 白屏防护）。
 */
export default function EvaluationResults(p: EvaluationResultsProps) {
  const isDark = useThemeStore((s) => s.theme) === 'dark'
  const { res, oos, meta } = p

  // 激活页签（ResultsTabs 上报）：图表 effect 依赖，首访容器有尺寸时 init
  const [activeTab, setActiveTab] = useState('metrics')
  const onActiveChange = useCallback((k: string) => setActiveTab(k), [])

  const icChartRef = useRef<HTMLDivElement>(null)
  const icChart = useRef<echarts.ECharts | null>(null)
  const bandChartRef = useRef<HTMLDivElement>(null)
  const bandChart = useRef<echarts.ECharts | null>(null)
  const qChartRef = useRef<HTMLDivElement>(null)
  const qChart = useRef<echarts.ECharts | null>(null)
  const wfChartRef = useRef<HTMLDivElement>(null)
  const wfChart = useRef<echarts.ECharts | null>(null)
  useEchartsLifecycle(icChartRef, icChart)
  useEchartsLifecycle(bandChartRef, bandChart)
  useEchartsLifecycle(qChartRef, qChart)
  useEchartsLifecycle(wfChartRef, wfChart)

  useEffect(() => () => {
    const dispose = (el: HTMLDivElement | null, inst: echarts.ECharts | null) => {
      if (inst && !inst.isDisposed()) inst.dispose()
      if (el) {
        const i = echarts.getInstanceByDom(el)
        if (i && !i.isDisposed()) i.dispose()
      }
    }
    dispose(icChartRef.current, icChart.current)
    dispose(bandChartRef.current, bandChart.current)
    dispose(qChartRef.current, qChart.current)
    dispose(wfChartRef.current, wfChart.current)
    icChart.current = null
    bandChart.current = null
    qChart.current = null
    wfChart.current = null
  }, [])

  // IC 时间序列（无渐变填充）
  useEffect(() => {
    if (!res?.ic_series || !icChartRef.current) return
    if (!icChart.current) icChart.current = echarts.init(icChartRef.current)
    const dates = res.dates ?? res.ic_series.map((_, i) => String(i))
    const t = chartTheme(isDark)
    icChart.current.setOption({
      animation: false,
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, valueFormatter: (v: number) => fmtRatio(v, 4) },
      grid: { left: 45, right: 16, top: 24, bottom: 24 },
      dataZoom: chartDataZoom(isDark),
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10, hideOverlap: true } },
      yAxis: chartYAxis(isDark),
      series: [{
        name: 'IC', type: 'line',
        data: res.ic_series.map((v, i) => [dates[i], v]),
        showSymbol: false,
        lineStyle: { width: 1.4, color: t.accent },
        areaStyle: { color: t.accentAreaWeak },
        markLine: {
          silent: true, symbol: 'none',
          lineStyle: { type: 'dashed', color: t.up },
          data: [{ yAxis: 0 }],
          label: { show: false },
        },
      }],
    }, true)
  }, [res, isDark, activeTab])

  // IC 条带图（20 日滚动窗口）
  useEffect(() => {
    if (!res?.ic_series || res.ic_series.length < 20 || !bandChartRef.current) return
    if (!bandChart.current) bandChart.current = echarts.init(bandChartRef.current)
    const W = 20
    const windows = Math.ceil(res.ic_series.length / W)
    const data: [number, number, number][] = []
    res.ic_series.forEach((v, i) => {
      if (v == null) return
      data.push([i % W, Math.floor(i / W), v])
    })
    const absMax = Math.max(0.02, ...res.ic_series.filter((v): v is number => v != null).map((v) => Math.abs(v)))
    const t = chartTheme(isDark)
    bandChart.current.setOption({
      animation: false,
      tooltip: { position: 'top' },
      grid: { left: 40, right: 16, top: 20, bottom: 64 },
      xAxis: {
        type: 'category',
        data: Array.from({ length: W }, (_, i) => i + 1),
        axisLabel: { fontSize: 10, color: t.text3 },
      },
      yAxis: {
        type: 'category',
        data: Array.from({ length: windows }, (_, i) => `#${i + 1}`),
        inverse: true,
        axisLabel: { fontSize: 10, color: t.text3 },
      },
      visualMap: {
        min: -absMax, max: absMax,
        orient: 'horizontal', left: 'center', bottom: 2,
        itemWidth: 14, itemHeight: 140,
        textStyle: { fontSize: 10, color: t.text3 },
        // 负→正三档：accent(蓝) / bg(中性) / up(红)，随主题令牌切换
        inRange: { color: [t.accent, t.bg, t.up] },
      },
      series: [{
        type: 'heatmap', data,
        itemStyle: { borderColor: t.bg, borderWidth: 1 },
      }],
    }, true)
  }, [res, isDark, activeTab])

  // 分层收益图
  useEffect(() => {
    if (!res?.quantiles || !qChartRef.current) return
    if (!qChart.current) qChart.current = echarts.init(qChartRef.current)
    const dates: string[] = (res.dates ?? []).length
      ? (res.dates as string[])
      : (res.quantiles?.q1 ?? []).map((_, i) => String(i))
    const t = chartTheme(isDark)
    // 5 档分层色板随主题：两端 t.up/t.down（强语义），中间三档取线色板（amber→金→青，
    // 随主题提亮）——canvas 不解析 CSS var，JS 令牌切换，isDark 在重绘依赖中
    const c = chartColors(isDark)
    const palette = [t.up, t.amber, c.lineColors[0], c.lineColors[6], t.down]
    const series = Object.entries(res.quantiles).map(([k, v], i) => ({
      name: `${k} (${i + 1 === (res.n_quantiles ?? 5) ? '最高因子' : i === 0 ? '最低因子' : `分位${i + 1}`})`,
      type: 'line' as const,
      data: v.map((x, j) => [dates[j], x]),
      showSymbol: false,
      lineStyle: { width: i === 0 || i === (res.n_quantiles ?? 5) - 1 ? 2.2 : 1.2, color: palette[i] },
      itemStyle: { color: palette[i] },
    }))
    qChart.current.setOption({
      animation: false,
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, valueFormatter: (v: number) => fmtRatio(v, 4) },
      legend: { top: 0, textStyle: { fontSize: 11, color: t.text2 } },
      grid: { left: 45, right: 16, top: 28, bottom: 24 },
      dataZoom: chartDataZoom(isDark),
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10, hideOverlap: true } },
      yAxis: chartYAxis(isDark),
      series,
    }, true)
  }, [res, isDark, activeTab])

  // Walk-forward OOS IC 拼接图（T-03）：各窗样本外 IC 序列首尾拼接,窗边界虚线标出
  useEffect(() => {
    if (!p.wf || !wfChartRef.current) return
    if (!wfChart.current) wfChart.current = echarts.init(wfChartRef.current)
    const t = chartTheme(isDark)
    const dates: string[] = (p.wf.oos_dates ?? []).length
      ? (p.wf.oos_dates as string[])
      : p.wf.oos_ic_series.map((_, i) => String(i))
    // 窗边界标记(拼接序列中的位置):按各窗 n_days 累计
    const marks: any[] = []
    let acc = 0
    for (const w of p.wf.windows) {
      acc += w.n_days
      if (w.window < p.wf.n_windows && w.n_days > 0) {
        marks.push({ xAxis: acc - 0.5, label: { show: false } })
      }
    }
    wfChart.current.setOption({
      animation: false,
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, valueFormatter: (v: number) => fmtRatio(v, 4) },
      grid: { left: 45, right: 16, top: 24, bottom: 24 },
      dataZoom: chartDataZoom(isDark),
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10, hideOverlap: true } },
      yAxis: chartYAxis(isDark),
      series: [{
        name: 'OOS IC', type: 'line',
        data: p.wf.oos_ic_series.map((v, i) => [dates[i] ?? String(i), v]),
        showSymbol: false,
        lineStyle: { width: 1.4, color: t.accent },
        areaStyle: { color: t.accentAreaWeak },
        markLine: {
          silent: true, symbol: 'none',
          lineStyle: { type: 'dashed', color: t.text3, width: 1 },
          data: marks,
          label: { show: false },
        },
      }],
    }, true)
  }, [p.wf, isDark, activeTab])

  const matchedFactor = p.factors.find((f) => f.expression === p.matchExpr.trim())
  const matchedVersion: any = matchedFactor
    ? [...(matchedFactor.versions ?? [])].sort((a: any, b: any) => b.version - a.version)[0]
    : undefined

  // —— T-15 导出：echarts getDataURL → PNG 下载（背景随主题取 chartTheme.bg，canvas 不解析 CSS var） ——
  const exportChartPng = async (name: string, chart: echarts.ECharts | null) => {
    if (!chart || chart.isDisposed()) {
      notifications.show({ color: 'yellow', message: `${name} 图表尚未就绪，请先切换到图表页签` })
      return
    }
    const dataUrl = await chart.getDataURL({ pixelRatio: 2, backgroundColor: chartTheme(isDark).bg })
    downloadDataUrl(`eval-${name}-${cnTodayOf()}.png`, dataUrl)
  }

  // —— T-15 研究报告导出：Markdown 快照（标题/参数快照/关键指标表/说明）——
  // HTML/PDF 本次不做：浏览器「打印」承载 PDF，报告文件内注明
  const exportReport = () => {
    const date = cnTodayOf()
    const lines = [
      '# 因子评估报告',
      '',
      `- 生成日期: ${date}`,
      `- 表达式: \`${p.matchExpr || '—'}\``,
    ]
    if (meta) {
      lines.push(`- 样本: ${meta.stocks} 只股票 · ${meta.days} 日 · 样本外自 ${meta.split_date}`)
    }
    lines.push(
      '',
      '## 关键指标',
      '',
      '| 指标 | 值 |',
      '| --- | --- |',
      `| IC | ${fmtRatio(res?.ic)} |`,
      `| RankIC | ${fmtRatio(res?.rank_ic)} |`,
      `| 多空年化 (Top-Bottom) | ${fmtPct(res?.long_short_annual)} |`,
      `| 换手率 | ${fmtRatio(res?.turnover, 3)} |`,
      `| 稳定性 (IC>0 占比) | ${fmtStab(res?.stability)} |`,
      `| 复杂度 (节点数) | ${fmtNum(res?.complexity, 2)} |`,
    )
    if (oos) {
      lines.push(
        '',
        '## 样本外 (OOS)',
        '',
        `- OOS IC: ${fmtRatio(oos.ic)}`,
        `- OOS 稳定性: ${fmtStab(oos.stability)}`,
        `- OOS 多空年化: ${fmtPct(oos.long_short_annual)}`,
        `- OOS 换手率: ${fmtRatio(oos.turnover, 3)}`,
      )
    }
    lines.push(
      '',
      '## 说明',
      '',
      '- 本报告为评估结果 Markdown 快照，数据截至生成日期。',
      '- HTML/PDF 版本由浏览器「打印」当前结果页生成（本次未内置 PDF 导出）。',
      '- 因子库发布与报告独立：报告仅记录指标，不触发发布动作。',
    )
    downloadText(`factor-report-${date}.md`, lines.join('\n'), 'text/markdown;charset=utf-8')
  }

  // T-03 Walk-forward 汇总(指标 Tab):拼接 OOS IC 的均值/波动/稳定性/正占比
  const wfMetricsTab = p.wf ? (
    <Flex direction="column" gap="var(--sr-pad-lg)">
      <Flex wrap="wrap" gap="var(--sr-pad-md)">
        <div style={statCell}>
          <MetricStat label="OOS IC 均值" value={fmtRatio(p.wf.summary?.mean_ic)}
            tone={p.wf.summary?.mean_ic != null && p.wf.summary.mean_ic > 0 ? 'up' : p.wf.summary?.mean_ic != null && p.wf.summary.mean_ic < 0 ? 'down' : 'plain'} sub="拼接全窗" />
        </div>
        <div style={statCell}>
          <MetricStat label="IC 标准差" value={fmtRatio(p.wf.summary?.ic_std)} sub="跨期波动" />
        </div>
        <div style={statCell}>
          <MetricStat label="稳定性" value={fmtStab(p.wf.summary?.stability)} sub="OOS IC>0 占比" />
        </div>
        <div style={statCell}>
          <MetricStat label="有效样本" value={p.wf.summary?.n_days != null ? String(p.wf.summary.n_days) : '—'} sub={`${p.wf.n_windows} 窗 · ${p.wf.horizon} 日周期`} />
        </div>
      </Flex>
      {meta && (
        <Text style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-3)' }}>
          {meta.stocks} 只股票 · {meta.days} 日 · {p.wf.n_windows} 窗 anchored 滚动验证(因子固定,不做每窗重挖)
        </Text>
      )}
    </Flex>
  ) : null

  const metricsTab = p.wf ? wfMetricsTab : (
    <Flex direction="column" gap="var(--sr-pad-lg)">
      <Flex wrap="wrap" gap="var(--sr-pad-md)">
        <div style={statCell}>
          <MetricStat label="IC" value={fmtRatio(res?.ic)} tone={res?.ic != null && res.ic > 0 ? 'up' : res?.ic != null && res.ic < 0 ? 'down' : 'plain'} />
        </div>
        <div style={statCell}>
          <MetricStat label="RankIC" value={fmtRatio(res?.rank_ic)} />
        </div>
        <div style={statCell}>
          <MetricStat label="多空年化" value={fmtPct(res?.long_short_annual)} sub="Top-Bottom" />
        </div>
        <div style={statCell}>
          <MetricStat label="换手率" value={fmtRatio(res?.turnover, 3)} />
        </div>
        <div style={statCell}>
          <MetricStat label="稳定性" value={fmtStab(res?.stability)} sub="IC>0 占比" />
        </div>
        <div style={statCell}>
          <MetricStat label="复杂度" value={fmtNum(res?.complexity, 2)} sub="节点数" />
        </div>
      </Flex>
      {meta && (
        <Text style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-3)' }}>
          {meta.stocks} 只股票 · {meta.days} 日 · 样本外自 {meta.split_date}
          {oos && <> · OOS IC <b style={{ color: oos.ic > 0 ? 'var(--sr-up)' : 'var(--sr-down)' }}>{fmtRatio(oos.ic)}</b> · OOS 稳定性 {fmtStab(oos.stability)}</>}
        </Text>
      )}
    </Flex>
  )

  // T-03 Walk-forward 图表 Tab:OOS IC 拼接图 + 分窗汇总表
  const wfChartsTab = p.wf ? (
    <Flex direction="column" gap="var(--sr-pad-2xl)">
      <div>
        <Group justify="space-between" align="center" gap="var(--sr-pad-md)">
          <Text fw={600} style={{ fontSize: 'var(--sr-font-title)' }}>Walk-forward OOS IC 拼接（分窗样本外）</Text>
          <IconTextButton icon={<IconDownload size={14} />} text="导出 PNG" onClick={() => exportChartPng('wf-oos-ic', wfChart.current)} />
        </Group>
        <div ref={wfChartRef} role="img" aria-label="Walk-forward 各窗样本外 OOS IC 拼接序列图" className="sr-chart-box-md" style={{ marginTop: 'var(--sr-pad-md)' }} />
      </div>
      <div>
        <Text fw={600} style={{ fontSize: 'var(--sr-font-title)' }}>分窗汇总</Text>
        <DataTable
          rowKey="window"
          dataSource={p.wf.windows}
          pagination={false}
          columns={[
            { title: '窗', dataIndex: 'window', render: (v: number) => `#${v}` },
            { title: '样本外区间', dataIndex: 'oos_start', render: (_: unknown, w: WalkForwardWindow) => (w.oos_start && w.oos_end ? `${w.oos_start} ~ ${w.oos_end}` : '—') },
            { title: 'OOS IC', dataIndex: 'ic', align: 'right', render: (v: number | null) => v == null ? '—' : <b style={{ color: v > 0 ? 'var(--sr-up)' : v < 0 ? 'var(--sr-down)' : 'var(--sr-text-1)' }}>{fmtRatio(v)}</b> },
            { title: '稳定性', dataIndex: 'stability', align: 'right', render: (v: number) => fmtStab(v) },
            { title: '有效样本', dataIndex: 'n_days', align: 'right' },
          ]}
          fillWidth
        />
      </div>
    </Flex>
  ) : null

  const chartsTab = p.wf ? wfChartsTab : (
    <Flex direction="column" gap="var(--sr-pad-2xl)">
      <div>
        <Group justify="space-between" align="center" gap="var(--sr-pad-md)">
          <Text fw={600} style={{ fontSize: 'var(--sr-font-title)' }}>IC 时间序列（稳定性）</Text>
          <IconTextButton icon={<IconDownload size={14} />} text="导出 PNG" onClick={() => exportChartPng('ic-series', icChart.current)} />
        </Group>
        <div ref={icChartRef} role="img" aria-label="IC 时间序列图" className="sr-chart-box-sm" style={{ marginTop: 'var(--sr-pad-md)' }} />
      </div>
      <div>
        <Group justify="space-between" align="center" gap="var(--sr-pad-md)">
          <Text fw={600} style={{ fontSize: 'var(--sr-font-title)' }}>IC 条带图（20 日滚动窗口）</Text>
          <IconTextButton icon={<IconDownload size={14} />} text="导出 PNG" onClick={() => exportChartPng('ic-band', bandChart.current)} />
        </Group>
        {res?.ic_series && res.ic_series.length >= 20 ? (
          <div ref={bandChartRef} role="img" aria-label="IC 条带图（20 日滚动窗口）" className="sr-chart-box-sm" style={{ marginTop: 'var(--sr-pad-md)' }} />
        ) : (
          <div style={{ marginTop: 'var(--sr-pad-md)' }}>
            <EmptyState description="IC 序列不足 20 日" padding={0} />
          </div>
        )}
      </div>
    </Flex>
  )

  const riskTab = oos ? (
    <Flex direction="column" gap="var(--sr-pad-lg)">
      <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
        样本外（OOS）段用于稳健性/风险校验，也是三步发布「样本外验证」的阈值依据。
      </Text>
      <Flex wrap="wrap" gap="var(--sr-pad-md)">
        <div style={statCell}>
          <MetricStat label="OOS IC" value={fmtRatio(oos.ic)} tone={oos.ic != null && oos.ic > 0 ? 'up' : oos.ic != null && oos.ic < 0 ? 'down' : 'plain'} />
        </div>
        <div style={statCell}>
          <MetricStat label="OOS 稳定性" value={fmtStab(oos.stability)} sub="IC>0 占比" />
        </div>
        <div style={statCell}>
          <MetricStat label="OOS 多空年化" value={fmtPct(oos.long_short_annual)} />
        </div>
        <div style={statCell}>
          <MetricStat label="OOS 换手率" value={fmtRatio(oos.turnover, 3)} />
        </div>
      </Flex>
      {meta?.split_date && (
        <Text style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-3)' }}>样本外自 {meta.split_date} 起</Text>
      )}
    </Flex>
  ) : (
    <EmptyState description="暂无样本外风险数据" />
  )

  const quantileTab = res?.quantiles ? (
    <Flex direction="column" gap="var(--sr-pad-lg)">
      <Group justify="space-between" align="center" gap="var(--sr-pad-md)">
        <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
          按因子值分 5 组等权持有，展示各组累计收益（Q1 最低因子 → Q5 最高因子）。
        </Text>
        <IconTextButton icon={<IconDownload size={14} />} text="导出 PNG" onClick={() => exportChartPng('quantile', qChart.current)} />
      </Group>
      <div ref={qChartRef} role="img" aria-label="分层收益曲线图（5 分位组合累计收益）" className="sr-chart-box-md" />
    </Flex>
  ) : (
    <EmptyState description="暂无分层收益数据" />
  )

  return (
    <Flex direction="column" gap="var(--sr-pad-xl)">
      {p.stale && (
        <Alert
          color="yellow"
          title={(
            <Flex justify="space-between" align="center" gap="var(--sr-pad-md)">
              <span>参数已变更，当前结果基于旧参数 — 需重新运行后方可发布</span>
              <Button size="xs" variant="light" onClick={p.onRerun}>重新运行</Button>
            </Flex>
          )}
        />
      )}

      {/* 发布到因子库：唯一发布入口 */}
      <div className="sr-run-panel">
        <div className="sr-run-panel-title">
          <IconRocket style={{ color: 'var(--sr-accent)' }} />
          发布到因子库
          <span style={{ marginLeft: 'auto', fontWeight: 400 }}>
            {matchedFactor && (
              <Group gap={4} wrap="nowrap">
                <Button size="xs" variant="default" leftSection={<IconChartBar size={14} />} onClick={p.onGoBacktest}>
                  去回测
                </Button>
                <Button size="xs" variant="default" leftSection={<IconArrowRight size={14} />} onClick={p.onGoLibrary}>
                  前往因子库
                </Button>
              </Group>
            )}
          </span>
        </div>
        {matchedFactor && matchedVersion ? (
          <Flex direction="column" gap="var(--sr-pad-lg)">
            <Group gap={8} wrap="wrap">
              <VersionStatusTag status={matchedVersion.status} />
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
                因子 #{matchedFactor.id}「{matchedFactor.name}」· 版本 v{matchedVersion.version} · 已完成{' '}
                {['oos_verified', 'stability_checked', 'complexity_checked'].filter((k) => matchedVersion[k]).length}/3
              </Text>
            </Group>
            <PublishStepsPanel
              steps={p.pubSteps}
              version={matchedVersion}
              onAdvance={p.onAdvance}
              advancing={p.advancing}
              readOnly={p.readOnly || p.stale}
            />
            <PublishFlowStatus version={matchedVersion} steps={p.pubSteps} />
            {p.pubError && <Alert color="red" title={p.pubError} />}
          </Flex>
        ) : (
          <Flex direction="column" gap="var(--sr-pad-lg)">
            <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)', margin: 0 }}>
              保存当前表达式与评估指标为草稿因子，按序完成样本外验证 / 稳定性检查 / 复杂度检查后自动发布。
            </Text>
            {p.pubError && <Alert color="red" title={p.pubError} />}
            <div>
              <Button
                variant="filled"
                leftSection={<IconRocket size={14} />}
                loading={p.publishing}
                disabled={p.stale || p.readOnly}
                onClick={p.onCreate}
              >
                创建为因子
              </Button>
              {p.stale && (
                <Text c="yellow" style={{ fontSize: 'var(--sr-font-sm)', display: 'block', marginTop: 'var(--sr-pad-sm)' }}>
                  参数已变更，重新运行评估后方可发布
                </Text>
              )}
            </div>
          </Flex>
        )}
      </div>

      {/* 指标 / 图表 / 风险 / 分层 Tabs（扁平，无卡片嵌套；stale Alert 保留在上方发布区原位） */}
      <Flex justify="flex-end">
        <IconTextButton
          icon={<IconFileDownload size={14} />}
          text="导出报告"
          tooltip="导出评估结果 Markdown 报告（HTML/PDF 由浏览器打印）"
          onClick={exportReport}
        />
      </Flex>
      <ResultsTabs
        items={[
          { key: 'metrics', label: '指标', children: metricsTab },
          { key: 'charts', label: '图表', children: chartsTab },
          { key: 'risk', label: '风险', children: riskTab },
          { key: 'quantile', label: '分层', children: quantileTab },
        ]}
        onActiveChange={onActiveChange}
      />
    </Flex>
  )
}
