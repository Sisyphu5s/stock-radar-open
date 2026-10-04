import { Button, Checkbox, Flex, Group, Text } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconDownload, IconFileDownload } from '@tabler/icons-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { BTModesResult, BTQuantile } from '../../../api/client'
import { BT_MODE_LABEL } from '../../../api/client'
import { fmtPct, fmtRatio, fmtStab } from '../../../utils/format'
import { drawdownSeries } from '../../../utils/compare'
import { cnTodayOf } from '../../../utils/time'
import { downloadDataUrl, downloadText } from '../../../utils/export'
import EmptyState from '../../../components/ui/EmptyState'
import IconTextButton from '../../../components/ui/IconTextButton'
import MetricStat from '../../../components/ui/MetricStat'
import { chartDataZoom, chartTheme, chartYAxis } from '../../../utils/echartsTheme'
import { echarts } from '../../../utils/echartsSetup'
import { semantic } from '../../../theme/tokens'
import { useThemeStore } from '../../../stores/useAppStore'
import { useEchartsLifecycle } from '../../../hooks/useEchartsLifecycle'
import ResultsTabs from '../shared/ResultsTabs'

/** 分层分位色板：Q1(低因子) 冷色 → Q{n}(高因子) 暖色，5 色；首色取 tokens accent（P1-46 旧 #60a5fa → #4c9aff） */
const QUANTILE_COLORS = (isDark: boolean): string[] => isDark
  ? [semantic.accent.dark, '#2fdd8f', '#f0c457', '#fb923c', '#ff6b6b']
  : ['#1668dc', '#0aa94f', '#d4a017', '#e8883a', '#d62025']

const DIR_LABEL: Record<string, string> = {
  auto: '自动',
  positive: '正向',
  negative: '反向',
  long: '多头(已解析)',
  short: '空头(已解析)',
}

interface BacktestResultsProps {
  bt: BTModesResult | undefined
  meta: any
  stale: boolean
  onRerun: () => void
}

/** MetricStat 网格单元（原 antd Col flex 用法） */
const statCell: React.CSSProperties = { flex: '1 1 140px', minWidth: 0 }

/**
 * 回测页右侧结果：指标/图表/风险/分层 Tabs（扁平，无卡片嵌套）。
 * 图表 effect 依赖 activeTab（ResultsTabs onActiveChange 上报）：首次切到图表 tab 时
 * ref 已挂载、effect 重跑，保证 echarts 在容器有尺寸后 init（宽度 0 白屏防护）。
 */
export default function BacktestResults({ bt, meta, stale, onRerun }: BacktestResultsProps) {
  const isDark = useThemeStore((s) => s.theme) === 'dark'
  const navRef = useRef<HTMLDivElement>(null)
  const navChart = useRef<echarts.ECharts | null>(null)
  const ddRef = useRef<HTMLDivElement>(null)
  const ddChart = useRef<echarts.ECharts | null>(null)
  const [visibleKeys, setVisibleKeys] = useState<string[]>([])
  const [showBaseline, setShowBaseline] = useState(true)
  const [logScale, setLogScale] = useState(false)
  const [activeTab, setActiveTab] = useState('metrics')
  const onActiveChange = useCallback((k: string) => setActiveTab(k), [])
  useEchartsLifecycle(navRef, navChart)
  useEchartsLifecycle(ddRef, ddChart)

  const hasModes = !!bt?.modes?.length
  const hasQuantiles = !!bt?.quantiles?.length
  const riskMode = bt?.modes?.[0]
  const risk = riskMode?.risk

  // 新结果加载时重置曲线显隐
  useEffect(() => {
    if (!bt) return
    const keys: string[] = []
    if (bt.modes) for (const x of bt.modes) keys.push('combo_' + x.mode)
    if (bt.quantiles) for (const x of bt.quantiles) keys.push('q' + x.q)
    if (bt.bench_nav?.length) keys.push('bench')
    setVisibleKeys(keys)
  }, [bt])

  useEffect(() => () => {
    const inst = navRef.current ? echarts.getInstanceByDom(navRef.current) : null
    if (inst && !inst.isDisposed()) inst.dispose()
    navChart.current = null
    const dd = ddRef.current ? echarts.getInstanceByDom(ddRef.current) : null
    if (dd && !dd.isDisposed()) dd.dispose()
    ddChart.current = null
  }, [])

  // 回撤曲线（T-03）：各 mode 净值 → 回撤序列（负值,面积线）。前端纯函数 drawdownSeries
  // 与 maxDrawdown 同源;isDark 进重绘依赖(canvas 不解析 CSS var)。
  useEffect(() => {
    if (!bt || !ddRef.current) return
    if (!ddChart.current) ddChart.current = echarts.getInstanceByDom(ddRef.current) ?? echarts.init(ddRef.current)
    const dates = meta?.dates?.length
      ? meta.dates
      : (bt.modes?.[0]?.combo_nav ?? []).map((_, i) => String(i))
    const t = chartTheme(isDark)
    const palette: Record<string, string> = {
      long_short: t.accent,
      long: t.down,
      short: t.up,
    }
    const series = (bt.modes ?? [])
      .filter((x) => visibleKeys.includes('combo_' + x.mode))
      .map((x) => {
        const color = palette[x.mode] ?? t.accent
        return {
          name: BT_MODE_LABEL[x.mode] ?? x.mode,
          type: 'line',
          data: drawdownSeries(x.combo_nav).map((v, i) => [dates[i] ?? String(i), v]),
          showSymbol: false,
          lineStyle: { width: 1.2, color },
          areaStyle: { color, opacity: 0.15 },
        }
      })
    if (!series.length) return
    ddChart.current.setOption({
      animation: false,
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, valueFormatter: (v: number) => fmtPct(v) },
      legend: { show: false },
      grid: { left: 50, right: 16, top: 16, bottom: 28 },
      dataZoom: chartDataZoom(isDark),
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
      yAxis: { ...chartYAxis(isDark), max: 0 }, // 回撤为负值(0 为顶)
      series,
    }, true)
  }, [bt, meta, visibleKeys, isDark, activeTab])

  // 净值曲线（无渐变填充；modes 为空时仍可画分层/基准）
  useEffect(() => {
    if (!bt || !navRef.current) return
    if (!navChart.current) navChart.current = echarts.getInstanceByDom(navRef.current) ?? echarts.init(navRef.current)
    const dates = meta?.dates?.length
      ? meta.dates
      : bt.dates?.length
        ? bt.dates
        : (bt.modes?.[0]?.combo_nav ?? bt.quantiles?.[0]?.combo_nav ?? []).map((_, i) => String(i))
    const series: any[] = []
    let firstVisible = true
    const t = chartTheme(isDark)
    const palette: Record<string, string> = {
      long_short: t.accent,
      long: t.down,
      short: t.up,
    }
    for (const x of bt.modes ?? []) {
      if (!visibleKeys.includes('combo_' + x.mode)) continue
      const color = palette[x.mode] ?? t.accent
      series.push({
        name: BT_MODE_LABEL[x.mode] ?? x.mode,
        type: 'line',
        data: x.combo_nav.map((v, i) => [dates[i] ?? String(i), v]),
        showSymbol: false,
        lineStyle: { width: x.mode === 'long_short' ? 2 : 1.6, color },
        areaStyle: x.mode === 'long_short' ? { color: t.accentAreaWeak } : undefined,
        markLine: showBaseline && firstVisible
          ? {
            symbol: 'none',
            data: [{ yAxis: 1 }],
            lineStyle: { color: t.text3, type: 'dashed', width: 1 },
            label: { show: false },
          }
          : undefined,
      })
      firstVisible = false
    }
    if (bt.quantiles?.length) {
      const qColors = QUANTILE_COLORS(isDark)
      for (const x of bt.quantiles) {
        if (!visibleKeys.includes('q' + x.q)) continue
        series.push({
          name: `Q${x.q}`,
          type: 'line',
          data: x.combo_nav.map((v, i) => [dates[i] ?? String(i), v]),
          showSymbol: false,
          lineStyle: { width: 1.3, color: qColors[(x.q - 1) % qColors.length] },
        })
      }
    }
    if (bt.bench_nav?.length && visibleKeys.includes('bench')) {
      series.push({
        name: '全市场等权基准',
        type: 'line',
        data: bt.bench_nav.map((v, i) => [dates[i] ?? String(i), v]),
        showSymbol: false,
        lineStyle: { width: 1.2, color: t.text3, type: 'dashed' },
      })
    }
    const posVals: number[] = []
    if (logScale) {
      const collect = (arr?: (number | null)[]) => {
        if (arr) for (const v of arr) if (typeof v === 'number' && v > 0) posVals.push(v)
      }
      for (const x of bt.modes ?? []) collect(x.combo_nav)
      if (bt.quantiles) for (const x of bt.quantiles) collect(x.combo_nav)
      collect(bt.bench_nav)
    }
    navChart.current.setOption({
      animation: false,
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, valueFormatter: (v: number) => fmtRatio(v, 4) },
      legend: { show: false },
      grid: { left: 50, right: 16, top: 16, bottom: 28 },
      dataZoom: chartDataZoom(isDark),
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10 } },
      yAxis: {
        ...chartYAxis(isDark),
        type: logScale ? 'log' : 'value',
        ...(logScale && posVals.length ? { min: Math.min(...posVals) * 0.9, max: Math.max(...posVals) * 1.1 } : {}),
      },
      series,
    }, true)
  }, [bt, meta, visibleKeys, showBaseline, logScale, isDark, activeTab])

  const comboOptions: { label: string; value: string }[] = [
    ...(bt?.modes ?? []).map((x) => ({ label: BT_MODE_LABEL[x.mode] ?? x.mode, value: 'combo_' + x.mode })),
    ...(bt?.quantiles?.length ? bt.quantiles.map((x) => ({ label: `Q${x.q}`, value: 'q' + x.q })) : []),
    ...(bt?.bench_nav?.length ? [{ label: '全市场基准', value: 'bench' }] : []),
  ]

  const resetZoom = () => {
    const c = navChart.current
    if (c) c.dispatchAction({ type: 'dataZoom', start: 0, end: 100 })
  }

  // —— T-15 导出：净值曲线 getDataURL → PNG 下载 ——
  const exportNavPng = async () => {
    const c = navChart.current
    if (!c || c.isDisposed()) {
      notifications.show({ color: 'yellow', message: '净值图表尚未就绪，请先切换到图表页签' })
      return
    }
    const dataUrl = await c.getDataURL({ pixelRatio: 2, backgroundColor: chartTheme(isDark).bg })
    downloadDataUrl(`backtest-nav-${cnTodayOf()}.png`, dataUrl)
  }

  // —— T-15 研究报告导出：Markdown 快照（标题/参数快照/关键指标表/说明）——
  const exportReport = () => {
    const date = cnTodayOf()
    const lines = [
      '# 回测报告',
      '',
      `- 生成日期: ${date}`,
    ]
    if (meta) lines.push(`- 样本: ${meta.stocks} 只股票 · ${meta.days} 日`)
    const params = bt?.params
    if (params) {
      lines.push(
        '',
        '## 参数快照',
        '',
        `- 调仓周期: ${params.trade_interval} 日`,
        `- 单边成本: ${fmtPct(Number(params.cost_rate), 1)}`,
      )
      if (Number.isFinite(Number(params.top_pct))) {
        lines.push(`- 多空比例: 各 ${Math.round(Number(params.top_pct) * 100)}%`)
      }
      if (params.n_q) lines.push(`- 分层: ${params.n_q} 层`)
      if (params.direction) lines.push(`- 方向: ${DIR_LABEL[String(params.direction)] ?? params.direction}`)
      if (params.modes?.length) lines.push(`- 使用方式: ${params.modes.map((m) => BT_MODE_LABEL[m] ?? m).join('、')}`)
    }
    if (bt?.modes?.length) {
      lines.push(
        '',
        '## 绩效指标',
        '',
        '| 方式 | 年化收益 | 夏普 | 最大回撤 | 波动率 | 胜率 |',
        '| --- | --- | --- | --- | --- | --- |',
      )
      for (const x of bt.modes) {
        lines.push(
          `| ${BT_MODE_LABEL[x.mode] ?? x.mode} | ${fmtPct(x.metrics.annual_return)} | ${fmtRatio(x.metrics.sharpe, 2)} | ${fmtPct(x.metrics.max_drawdown)} | ${fmtPct(x.metrics.volatility)} | ${fmtPct(x.metrics.win_rate, 1)} |`,
        )
      }
    }
    if (bt?.bench_metrics) {
      lines.push(
        '',
        '## 全市场等权基准',
        '',
        `| 年化收益 | 夏普 | 最大回撤 | 波动率 |`,
        `| --- | --- | --- | --- |`,
        `| ${fmtPct(bt.bench_metrics.annual_return)} | ${fmtRatio(bt.bench_metrics.sharpe, 2)} | ${fmtPct(bt.bench_metrics.max_drawdown)} | ${fmtPct(bt.bench_metrics.volatility)} |`,
      )
    }
    lines.push(
      '',
      '## 说明',
      '',
      '- 本报告为回测结果 Markdown 快照，数据截至生成日期。',
      '- HTML/PDF 版本由浏览器「打印」当前结果页生成（本次未内置 PDF 导出）。',
    )
    downloadText(`backtest-report-${date}.md`, lines.join('\n'), 'text/markdown;charset=utf-8')
  }

  const metricsTab = (
    <Flex direction="column" gap="var(--sr-pad-xl)">
      {hasModes ? (
        (bt?.modes ?? []).map((x, i) => (
          <div key={x.mode}>
            <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>{BT_MODE_LABEL[x.mode] ?? x.mode}</Text>
            <Flex wrap="wrap" gap="var(--sr-pad-md)" style={{ marginTop: 'var(--sr-pad-sm)' }}>
              <div style={statCell}>
                <MetricStat label="年化收益" value={fmtPct(x.metrics.annual_return)}
                  tone={Number(x.metrics.annual_return ?? 0) > 0 ? 'up' : 'down'} />
              </div>
              <div style={statCell}>
                <MetricStat label="夏普比率" value={fmtRatio(x.metrics.sharpe, 2)} />
              </div>
              <div style={statCell}>
                <MetricStat label="最大回撤" value={fmtPct(x.metrics.max_drawdown)}
                  tone={Number(x.metrics.max_drawdown ?? 0) > 0.2 ? 'up' : 'plain'} />
              </div>
              <div style={statCell}>
                <MetricStat label="波动率" value={fmtPct(x.metrics.volatility)} />
              </div>
              <div style={statCell}>
                <MetricStat label="胜率" value={fmtPct(x.metrics.win_rate, 1)} />
              </div>
            </Flex>
            {i < (bt?.modes?.length ?? 0) - 1 && <div style={{ height: 'var(--sr-pad-lg)' }} />}
          </div>
        ))
      ) : hasQuantiles ? (
        <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
          当前仅启用「分层」使用方式，绩效以分层组合（见「分层」页签）为准。
        </Text>
      ) : (
        <EmptyState description="无可用指标" />
      )}
      {bt?.bench_metrics && (
        <div>
          <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>全市场等权基准</Text>
          <Flex wrap="wrap" gap="var(--sr-pad-md)" style={{ marginTop: 'var(--sr-pad-sm)' }}>
            <div style={statCell}>
              <MetricStat label="年化收益" value={fmtPct(bt.bench_metrics.annual_return)}
                tone={Number(bt.bench_metrics?.annual_return ?? 0) > 0 ? 'up' : 'down'} sub="等权" />
            </div>
            <div style={statCell}>
              <MetricStat label="夏普比率" value={fmtRatio(bt.bench_metrics.sharpe, 2)} />
            </div>
            <div style={statCell}>
              <MetricStat label="最大回撤" value={fmtPct(bt.bench_metrics.max_drawdown)} />
            </div>
            <div style={statCell}>
              <MetricStat label="波动率" value={fmtPct(bt.bench_metrics.volatility)} />
            </div>
          </Flex>
        </div>
      )}
      {bt?.params && (
        <Text style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-3)' }}>
          {meta?.stocks} 只股票 · {meta?.days} 日 · 调仓 {bt.params.trade_interval} 日 ·
          单边成本 {fmtPct(Number(bt.params.cost_rate), 1)}
          {Number.isFinite(Number(bt.params.top_pct))
            ? ` · 多空各 ${Math.round(Number(bt.params.top_pct) * 100)}%` : ''}
          {bt.params.n_q ? ` · 分位 ${bt.params.n_q} 层` : ''}
          {bt.params.direction ? ` · 方向 ${DIR_LABEL[String(bt.params.direction)] ?? bt.params.direction}` : ''}
        </Text>
      )}
    </Flex>
  )

  const chartsTab = (
    <Flex direction="column" gap="var(--sr-pad-lg)">
      <Group wrap="wrap" gap={12}>
        <Checkbox.Group
          value={visibleKeys}
          onChange={(v) => setVisibleKeys(v as string[])}
        >
          {comboOptions.map((o) => (
            <Checkbox key={o.value} value={o.value} label={o.label} />
          ))}
        </Checkbox.Group>
        <Checkbox checked={showBaseline} onChange={(e) => setShowBaseline(e.currentTarget.checked)} label="净值基准线 y=1" />
        <Checkbox checked={logScale} onChange={(e) => setLogScale(e.currentTarget.checked)} label="对数坐标" />
        <Button size="xs" variant="default" onClick={resetZoom}>重置缩放</Button>
        <IconTextButton icon={<IconDownload size={14} />} text="导出 PNG" onClick={exportNavPng} />
      </Group>
      <div ref={navRef} role="img" aria-label="回测净值曲线：多方式叠加与全市场等权基准" className="sr-chart-box-md" />
      <div>
        <Text fw={600} style={{ fontSize: 'var(--sr-font-title)', margin: 'var(--sr-pad-lg) 0 var(--sr-pad-sm)' }}>
          回撤曲线
        </Text>
        <div ref={ddRef} role="img" aria-label="回测回撤曲线：各使用方式的净值回撤序列(负值)" className="sr-chart-box-sm" />
      </div>
    </Flex>
  )

  const riskTab = risk ? (
    <Flex direction="column" gap="var(--sr-pad-lg)">
      <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
        风控指标（FRM）· {BT_MODE_LABEL[riskMode?.mode ?? ''] ?? ''}
      </Text>
      <Flex wrap="wrap" gap="var(--sr-pad-md)">
        {[
          { k: 'var95', l: 'VaR(95%)', f: (v: number) => fmtPct(v) },
          { k: 'cvar95', l: 'CVaR(95%)', f: (v: number) => fmtPct(v) },
          { k: 'annual_volatility', l: '年化波动', f: (v: number) => fmtPct(v, 1) },
          { k: 'max_drawdown', l: '最大回撤', f: (v: number) => fmtPct(v, 1) },
          { k: 'sharpe', l: '夏普', f: (v: number) => fmtRatio(v, 2) },
          { k: 'win_rate', l: '胜率', f: (v: number) => fmtStab(v) },
        ].map((x) => {
          const v = (risk as Record<string, number | undefined>)[x.k]
          return (
            <div key={x.k} style={statCell}>
              <MetricStat
                label={x.l}
                value={v != null ? x.f(v) : '—'}
                tone={x.k === 'max_drawdown' && v != null && v > 0.2 ? 'up' : 'plain'}
              />
            </div>
          )
        })}
      </Flex>
    </Flex>
  ) : hasModes || hasQuantiles ? (
    <EmptyState description="该使用方式未返回风控指标" />
  ) : (
    <EmptyState description="暂无风控数据" />
  )

  const quantileTab = hasQuantiles ? (() => {
    const qs = (bt?.quantiles ?? []) as BTQuantile[]
    const qFirst = qs[0]
    const qLast = qs[qs.length - 1]
    const a1 = Number(qFirst?.metrics.annual_return ?? NaN)
    const aN = Number(qLast?.metrics.annual_return ?? NaN)
    const mono = Number.isFinite(a1) && Number.isFinite(aN) ? aN - a1 : null
    return (
      <Flex direction="column" gap="var(--sr-pad-lg)">
        <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
          分层组合（Q1 最低因子 → Q{qs.length} 最高，各分位独立等权）
        </Text>
        <Flex direction="column" gap="var(--sr-pad-sm)">
          {qs.map((x) => (
            <Flex key={x.q} wrap="wrap" align="center" gap={10} style={{
              padding: 'var(--sr-pad-sm) var(--sr-pad-lg)', borderRadius: 'var(--sr-radius-card)',
              background: 'var(--sr-block-bg)', border: '1px solid var(--sr-border)',
            }}>
              <div style={{ flex: '0 0 36px' }}>
                <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>Q{x.q}</Text>
              </div>
              <div style={{ flex: '0 0 96px', fontSize: 'var(--sr-font-sm)' }}>
                年化 <b style={{ color: Number(x.metrics.annual_return ?? 0) > 0 ? 'var(--sr-up)' : 'var(--sr-down)' }}>{fmtPct(x.metrics.annual_return)}</b>
              </div>
              <div style={{ flex: '0 0 72px', fontSize: 'var(--sr-font-sm)' }}>
                夏普 <b>{fmtRatio(x.metrics.sharpe, 2)}</b>
              </div>
              <div style={{ flex: '0 0 96px', fontSize: 'var(--sr-font-sm)' }}>
                回撤 <b>{fmtPct(x.metrics.max_drawdown)}</b>
              </div>
              <div style={{ flex: '1 1 120px', fontSize: 'var(--sr-font-sm)' }}>
                胜率 <b>{fmtPct(x.metrics.win_rate, 1)}</b>
              </div>
            </Flex>
          ))}
        </Flex>
        <Text style={{ fontSize: 'var(--sr-font-sm)' }}>
          <Text c="dimmed" span>单调性 </Text>
          {mono === null ? (
            <Text c="dimmed" span>—（指标不足）</Text>
          ) : (
            <>
              <Text fw={600} span style={{ color: mono >= 0 ? 'var(--sr-up)' : 'var(--sr-down)' }}>
                Q{qs.length}年化 - Q1年化 = {mono >= 0 ? '+' : ''}{fmtPct(mono, 1)}
              </Text>
              <Text c="dimmed" span>
                {' '}（{mono >= 0 ? '高分组占优，因子方向有效' : '低分组占优，因子方向存疑'}）
              </Text>
            </>
          )}
        </Text>
      </Flex>
    )
  })() : (
    <EmptyState description="该使用方式未包含分层组合，可勾选「分层(5分位)」后重新运行" />
  )

  return (
    <Flex direction="column" gap="var(--sr-pad-xl)">
      <Flex justify="flex-end">
        <IconTextButton
          icon={<IconFileDownload size={14} />}
          text="导出报告"
          tooltip="导出回测结果 Markdown 报告（HTML/PDF 由浏览器打印）"
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
        stale={stale}
        staleMessage="参数已变更，当前结果基于旧参数 — 一键重跑"
        onRerun={onRerun}
        onActiveChange={onActiveChange}
      />
    </Flex>
  )
}
