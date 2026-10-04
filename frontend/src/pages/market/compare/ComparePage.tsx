import { Flex, MultiSelect, Text } from '@mantine/core'
import { IconChartLine, IconGridDots, IconListNumbers } from '@tabler/icons-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQueries } from '@tanstack/react-query'
import { api } from '../../../api/client'
import { useWatchlistState } from '../../../data/market'
import { pollInterval } from '../../../data/queryBase'
import type { QueryState } from '../../../data/queryBase'
import { isCnTradingSession } from '../../../utils/time'
import { klineAxisLabel } from '../../../utils/klineSeries'
import type { KlineData } from '../../../utils/klineSeries'
import { chartColors } from '../../../utils/chartTheme'
import { chartDataZoom, chartTheme, chartYAxis } from '../../../utils/echartsTheme'
import { echarts } from '../../../utils/echartsSetup'
import { useEchartsLifecycle } from '../../../hooks/useEchartsLifecycle'
import { useThemeStore } from '../../../stores/useAppStore'
import { CardShell, EmptyState, HScroll, PageHeader, PageShell, SkeletonBlock, StatStrip } from '../../../components/ui'
import { fmtPct } from '../../../utils/format'
import {
  COMPARE_DAYS, MAX_COMPARE_STOCKS, MIN_COMPARE_BARS,
  computeStockMetrics, hexToRgba, loadRecentStocks, normalizeCloses, pairwiseCorrelation, touchRecentStock,
} from '../../../utils/compare'
import type { CompareStock } from '../../../utils/compare'
import './compare.css'

/**
 * 多股对比页（工具页，不入菜单；入口 = 关注页「多股对比」按钮）。
 * 结构：PageShell + PageHeader（股票选择器）→ 归一化净值图 / 指标对比 / 相关性矩阵。
 * 数据：每股经 useQueries 拉日线 K 线（queryKey 与 data/kline.ts 同形 ['mkt','kline',code,'daily',250]，
 * 缓存共享；周期固定 daily、回溯 250 天）。
 * 计算：全部走 utils/compare.ts 纯函数；数据不足（<20 根 K 线）→ 该股「数据不足」。
 */

/** 股票数据就绪状态：加载中 / 加载失败 / 数据不足 / 可计算 */
type CellState = 'loading' | 'failed' | 'short' | 'ready'

interface PreparedStock {
  stock: CompareStock
  state: QueryState<KlineData>
  dates: string[]
  closes: number[]
  enough: boolean
}

/** 单元格渲染：按数据状态回落文案，就绪才格式化数值并着色 */
function cellValue(
  state: CellState,
  raw: number | null,
  fmt: (x: number) => string,
  tone: (x: number) => 'up' | 'down' | 'plain',
): { value: string; tone: 'up' | 'down' | 'plain' } {
  if (state === 'loading') return { value: '加载中…', tone: 'plain' }
  if (state === 'failed') return { value: '加载失败', tone: 'plain' }
  if (state === 'short' || raw == null) return { value: '数据不足', tone: 'plain' }
  return { value: fmt(raw), tone: tone(raw) }
}

export default function ComparePage() {
  const isDark = useThemeStore((s) => s.theme) === 'dark'
  /** 图表色板令牌(P2-78):一次创建、多处引用(isDark 稳定即对象稳定)——
   *  消除指标对比循环/相关性矩阵内每格新建 chartColors 对象的冗余分配 */
  const cc = useMemo(() => chartColors(isDark), [isDark])
  const watchlistState = useWatchlistState()
  const watchlist = watchlistState.value ?? []

  const [selected, setSelected] = useState<string[]>([])
  const [recent, setRecent] = useState<CompareStock[]>(() => loadRecentStocks())

  /** 选项 = 关注列表 ∪ 最近使用（按 code 去重，关注列表名称优先） */
  const options = useMemo(() => {
    const m = new Map<string, string>()
    for (const w of watchlist) m.set(w.code, w.name || w.code)
    for (const r of recent) if (!m.has(r.code)) m.set(r.code, r.name || r.code)
    return [...m.entries()].map(([value, label]) => ({ value, label }))
  }, [watchlist, recent])

  /** 选择变化：记录最近使用（供下次进入时选项补充） */
  const onChange = (vals: string[]) => {
    const next = vals.slice(0, MAX_COMPARE_STOCKS)
    setSelected(next)
    const nameOf = new Map(options.map((o) => [o.value, o.label]))
    for (const code of next) touchRecentStock(code, nameOf.get(code) ?? code)
    setRecent(loadRecentStocks())
  }

  /**
   * 每股 K 线订阅：queryKey/参数与 data/kline.ts 的 useKlineState 完全同形
   * （同 key 共享 TanStack 缓存与轮询；N 只动态数量无法用定长 hooks，走 useQueries）。
   * 语义对齐 klineQuery：60s SWR、非交易时段 5min 轮询。
   */
  const klineStates = useQueries({
    queries: selected.map((code) => ({
      queryKey: ['mkt', 'kline', code, 'daily', COMPARE_DAYS],
      queryFn: async () => {
        const { data } = await api.get(`/stocks/${code}/kline`, { params: { period: 'daily', days: COMPARE_DAYS } })
        return data as KlineData
      },
      staleTime: 60_000,
      refetchInterval: pollInterval(60_000, { slowWhen: () => !isCnTradingSession(), slowPollMs: 300_000 }),
    })),
  })

  /** useQueries 结果 → 与 data/queryBase 相同 QueryState 形状（对齐 useQueryState 的 mapResult） */
  const klineStateList = useMemo<QueryState<KlineData>[]>(
    () => klineStates.map((q) => ({
      value: q.data as KlineData | undefined,
      loading: q.isFetching,
      error: q.error ?? undefined,
      fetchedAt: q.dataUpdatedAt,
    })),
    [klineStates],
  )

  /** 每股预处理：过滤非法收盘价 + 数据充分性判定（< MIN_COMPARE_BARS → 不足） */
  const prepared = useMemo<PreparedStock[]>(() => {
    const nameOf = new Map(options.map((o) => [o.value, o.label]))
    return selected.map((code, i) => {
      const st = klineStateList[i]
      const k = st?.value
      const dates: string[] = []
      const closes: number[] = []
      if (k) {
        const n = Math.min(k.dates.length, k.close.length)
        for (let j = 0; j < n; j++) {
          if (Number.isFinite(k.close[j])) {
            dates.push(k.dates[j])
            closes.push(k.close[j])
          }
        }
      }
      const enough = closes.length >= MIN_COMPARE_BARS
      return {
        stock: { code, name: nameOf.get(code) ?? code },
        state: st ?? { value: undefined, loading: true, error: undefined, fetchedAt: 0 },
        dates,
        closes,
        enough,
      }
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, options, klineStateList])

  const stateOf = (p: PreparedStock): CellState => {
    if (p.state.value === undefined) {
      if (p.state.loading) return 'loading'
      if (p.state.error) return 'failed'
      return 'loading' // 未订阅完成的兜底（理论不可达）
    }
    return p.enough ? 'ready' : 'short'
  }

  // ===== 归一化净值图（echarts Line，多序列同图；x 轴 = 全部股票日期并集，缺失点留空） =====
  const allDates = useMemo(() => {
    const s = new Set<string>()
    for (const p of prepared) if (p.enough) for (const d of p.dates) s.add(d)
    return [...s].sort()
  }, [prepared])

  const chartSeries = useMemo(() => {
    return prepared
      .filter((p) => p.enough)
      .map((p, i) => {
        const nav = normalizeCloses(p.closes)
        const byDate = new Map<string, number>()
        for (let j = 0; j < Math.min(p.dates.length, nav.length); j++) byDate.set(p.dates[j], nav[j])
        const color = cc.lineColors[i % cc.lineColors.length]
        return {
          name: p.stock.name,
          color,
          data: allDates.map((d) => [d, byDate.get(d) ?? null]),
        }
      })
  }, [prepared, allDates, cc])

  const chartRef = useRef<HTMLDivElement>(null)
  const chart = useRef<echarts.ECharts | null>(null)
  useEchartsLifecycle(chartRef, chart)

  // 净值图渲染/重绘：isDark/序列/日期任一变化即 setOption；容器重挂时重建实例；卸载时 dispose
  useEffect(() => {
    const el = chartRef.current
    if (!el) return
    if (!chart.current || chart.current.getDom() !== el) {
      if (chart.current && !chart.current.isDisposed()) chart.current.dispose()
      chart.current = echarts.init(el)
    }
    const t = chartTheme(isDark)
    chart.current.setOption(
      {
        animation: false,
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'cross' },
          valueFormatter: (v: number) => (Number.isFinite(v) ? v.toFixed(3) : '—'),
        },
        legend: { top: 4, type: 'scroll', itemWidth: 14, itemHeight: 8, textStyle: { color: t.text2, fontSize: 11 } },
        grid: { left: 48, right: 16, top: 30, bottom: 26 },
        dataZoom: chartDataZoom(isDark),
        xAxis: {
          type: 'category',
          data: allDates,
          axisLabel: {
            fontSize: 10, color: t.text3, hideOverlap: true,
            formatter: (d: string) => klineAxisLabel(d, 'daily'),
          },
        },
        yAxis: chartYAxis(isDark),
        series: chartSeries.map((s) => ({
          name: s.name,
          type: 'line' as const,
          data: s.data,
          showSymbol: false,
          lineStyle: { width: 1.6, color: s.color },
          itemStyle: { color: s.color },
          emphasis: { focus: 'series' as const },
        })),
      },
      true,
    )
  }, [isDark, chartSeries, allDates])
  useEffect(() => () => {
    const c = chart.current
    if (c && !c.isDisposed()) c.dispose()
    chart.current = null
  }, [])

  // ===== 指标对比（每股一列：涨跌幅/年化波动/最大回撤/夏普） =====
  const metrics = useMemo(() => prepared.map((p) => computeStockMetrics(p.closes)), [prepared])

  // ===== 相关性矩阵（日收益两两 Pearson，日期交集对齐） =====
  const corr = useMemo(
    () => pairwiseCorrelation(prepared.map((p) => ({ dates: p.dates, closes: p.closes }))),
    [prepared],
  )

  const allLoading = prepared.length > 0 && prepared.every((p) => p.state.value === undefined && p.state.loading)

  return (
    <PageShell
      fill
      header={
        <PageHeader
          extra={
            <div className="cmp-selector">
              <MultiSelect
                className="cmp-pick"
                size="xs"
                searchable
                clearable
                placeholder="输入代码 / 名称搜索选择（2~8 只）"
                data={options}
                value={selected}
                onChange={onChange}
                maxValues={MAX_COMPARE_STOCKS}
                limit={30}
                hidePickedOptions
                comboboxProps={{ withinPortal: true }}
              />
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
                已选 {selected.length} / {MAX_COMPARE_STOCKS} 只；选择 2 只以上开始对比
              </Text>
            </div>
          }
        />
      }
    >
      {selected.length < 2 ? (
        <EmptyState
          description={
            <span style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <span>先选 2 只以上股票</span>
              <Text component="span" style={{ color: 'var(--sr-text-2)', fontSize: 'var(--sr-font-sm)' }}>
                同图对比归一化净值 / 指标并排 / 日收益相关性
              </Text>
            </span>
          }
          padding="80px 0"
        />
      ) : allLoading ? (
        <SkeletonBlock rows={5} />
      ) : (
        // T-52 满高:卡组容器占满剩余高度;指标/相关卡弹性填充分配,卡体内部滚动防裁切
        <Flex direction="column" gap="var(--sr-gap-card)" className="cmp-stack">
          <CardShell
            title="归一化净值（首日 = 1）"
            icon={<IconChartLine size={16} />}
            extra={allDates.length > 0 ? (
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
                {allDates[0]} ~ {allDates[allDates.length - 1]}
              </Text>
            ) : undefined}
          >
            {chartSeries.length === 0 ? (
              <EmptyState description="所选股票数据不足（少于 20 根 K 线），无法绘制净值曲线" padding="24px 0" />
            ) : (
              <div ref={chartRef} className="cmp-chart" />
            )}
          </CardShell>

          <CardShell className="cmp-fill-card" title="指标对比（近一年日收盘序列）" icon={<IconListNumbers size={16} />}>
            <HScroll>
              <div
                className="cmp-metrics"
                style={{ gridTemplateColumns: `repeat(${prepared.length}, minmax(150px, 1fr))` }}
                role="table"
                aria-label="多股指标对比"
              >
                {prepared.map((p, i) => {
                  const st = stateOf(p)
                  const m = metrics[i]
                  const ret = cellValue(st, m.periodReturn, (v) => fmtPct(v, 2), (v) => (v > 0 ? 'up' : v < 0 ? 'down' : 'plain'))
                  const vol = cellValue(st, m.annualVol, (v) => fmtPct(v, 2), () => 'plain')
                  const dd = cellValue(st, m.maxDD, (v) => fmtPct(-v, 2), () => 'down')
                  const sharpe = cellValue(st, m.sharpe, (v) => v.toFixed(2), () => 'plain')
                  const color = cc.lineColors[i % cc.lineColors.length]
                  return (
                    <div key={p.stock.code} className="cmp-metrics-col" role="row">
                      <div className="cmp-metrics-head">
                        <span className="cmp-color-dot" style={{ background: color }} aria-hidden />
                        <span className="cmp-metrics-name" title={`${p.stock.name} ${p.stock.code}`}>{p.stock.name}</span>
                        <span className="cmp-metrics-code">{p.stock.code}</span>
                      </div>
                      <StatStrip
                        items={[
                          { key: 'periodReturn', label: '区间涨跌幅', value: ret.value, tone: ret.tone },
                          { key: 'annualVol', label: '年化波动', value: vol.value, tone: vol.tone },
                          { key: 'maxDD', label: '最大回撤', value: dd.value, tone: dd.tone },
                          { key: 'sharpe', label: '夏普（无风险 = 0）', value: sharpe.value, tone: sharpe.tone },
                        ]}
                      />
                    </div>
                  )
                })}
              </div>
            </HScroll>
          </CardShell>

          <CardShell className="cmp-fill-card" title="相关性矩阵（日收益 Pearson）" icon={<IconGridDots size={16} />}>
            <HScroll>
              <div
                className="cmp-correlation"
                style={{ gridTemplateColumns: `minmax(84px, auto) repeat(${prepared.length}, minmax(56px, 1fr))` }}
                role="table"
                aria-label="多股日收益相关性矩阵"
              >
                <div className="cmp-corr-cell cmp-corr-dim" />
                {prepared.map((p) => (
                  <div key={`h-${p.stock.code}`} className="cmp-corr-cell cmp-corr-head" title={p.stock.name}>
                    {p.stock.name}
                  </div>
                ))}
                {prepared.map((p, i) => {
                  const stI = stateOf(p)
                  return (
                    <div key={`r-${p.stock.code}`} className="cmp-corr-row" role="row" style={{ display: 'contents' }}>
                      <div className="cmp-corr-cell cmp-corr-head" title={`${p.stock.name} ${p.stock.code}`}>
                        {p.stock.name}
                      </div>
                      {prepared.map((q, j) => {
                        const stJ = stateOf(q)
                        const r = corr[i]?.[j] ?? null
                        let bg = 'transparent'
                        let text: string | null = null
                        let dim = false
                        if (stI === 'ready' && stJ === 'ready' && r != null) {
                          text = r.toFixed(2)
                          const alpha = Math.min(Math.abs(r), 1) * 0.45
                          bg = hexToRgba(r >= 0 ? cc.up : cc.down, alpha)
                        } else if (stI === 'ready' && stJ === 'ready' && r == null) {
                          text = '—'
                          dim = true
                        } else {
                          text = stI === 'loading' || stJ === 'loading' ? '…' : '数据不足'
                          dim = true
                        }
                        return (
                          <div
                            key={`c-${q.stock.code}`}
                            className={'cmp-corr-cell' + (dim ? ' cmp-corr-dim' : '')}
                            style={{ background: bg }}
                            title={r != null ? `r = ${r.toFixed(4)}` : undefined}
                          >
                            {text}
                          </div>
                        )
                      })}
                    </div>
                  )
                })}
              </div>
            </HScroll>
          </CardShell>
        </Flex>
      )}
    </PageShell>
  )
}
