import { Badge, Loader, Text } from '@mantine/core'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ColorType, createChart, CrosshairMode, LineSeries, LineStyle } from 'lightweight-charts'
import type { IChartApi, ISeriesApi } from 'lightweight-charts'
import {
  getPaperWatchHistory,
  type PaperMultiWatchResult, type PaperWatchHistoryPoint, type PaperWatchResult, type PaperWatchStock,
} from '../../../api/client'
import { EmptyState, MetricStat } from '../../../components/ui'
import { fmtNum } from '../../../utils/format'
import { isMinutePeriod, periodLabel } from '../../../utils/periods'
import { formatShanghaiFullTime } from '../../../utils/time'
import { klineTickFormatter, klineTsTime, type UTCTimestamp } from '../../../utils/klineSeries'
import { chartColors } from '../../../utils/chartTheme'
import { useThemeStore } from '../../../stores/useAppStore'
import { WATCH_INTERVAL_MS } from './constants'
import './paper-shared.css'
import './WatchResults.css'

/** antd 色名 → Mantine 色名（labelOf/signalLabelMap 返回 antd 色域，Badge 需 Mantine 色名） */
const ANT_TO_MANTINE: Record<string, string> = {
  default: 'gray', blue: 'blue', green: 'green', red: 'red', orange: 'orange', gold: 'yellow',
  volcano: 'orange', purple: 'grape', cyan: 'cyan', magenta: 'pink', geekblue: 'indigo',
  success: 'teal', processing: 'blue', error: 'red', warning: 'yellow',
}
const badgeColor = (c: string): string => ANT_TO_MANTINE[c] ?? 'gray'

/** 实时指标快照展示字段（按存在性渲染，缺字段自动隐藏） */
const SNAPSHOT_FIELDS: { key: string; label: string }[] = [
  // close = 最新 bar 收盘价（后端 watch 快照实际键；旧 price 键后端从未产出，已并入 close）
  { key: 'close', label: '现价' },
  { key: 'rsi6', label: 'RSI6' },
  { key: 'rsi12', label: 'RSI12' },
  { key: 'rsi24', label: 'RSI24' },
  { key: 'kdj_k', label: 'KDJ K' },
  { key: 'kdj_d', label: 'KDJ D' },
  { key: 'kdj_j', label: 'KDJ J' },
  { key: 'macd_dif', label: 'MACD DIF' },
  { key: 'macd_dea', label: 'MACD DEA' },
  { key: 'macd_hist', label: 'MACD柱' },
  { key: 'ma5', label: 'MA5' },
  { key: 'ma10', label: 'MA10' },
  { key: 'ma20', label: 'MA20' },
  { key: 'ma60', label: 'MA60' },
  { key: 'boll_upper', label: 'BOLL上' },
  { key: 'boll_mid', label: 'BOLL中' },
  { key: 'boll_lower', label: 'BOLL下' },
  { key: 'volume_ratio', label: '量比' },
]

/** 单股监控块：指标快照 + 当前触发 + 最近触发点（单股项目整体 / 多股项目每股循环）。 */
function StockWatchBlock({ stock, period, labelOf }: {
  stock: PaperWatchStock
  period: string
  labelOf: (code: string) => { text: string; color: string }
}) {
  const snapshotRows = useMemo(() => {
    const s = stock.snapshot
    if (!s) return []
    return SNAPSHOT_FIELDS
      .filter((f) => typeof s[f.key] === 'number' && Number.isFinite(s[f.key] as number))
      .map((f) => ({ key: f.key, label: f.label, value: s[f.key] as number }))
  }, [stock.snapshot])

  const hitSignals = stock.hit_signals ?? []
  const recentTriggers = stock.recent_triggers ?? []

  return (
    <div className="sr-paper-section">
      <div className="sr-paper-section-title">
        <Text fw={700} style={{ fontSize: 'var(--sr-font-title)' }}>{stock.name || stock.code}（{stock.code}）</Text>
        <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
          {periodLabel(stock.period ?? period)}
        </Text>
      </div>
      {snapshotRows.length === 0 ? (
        <EmptyState description="暂无指标快照" padding="24px 0" />
      ) : (
        <div className="sr-paper-snap">
          {snapshotRows.map((s) => (
            <div key={s.key} className="sr-paper-snap-item">
              <MetricStat label={s.label} value={fmtNum(s.value)} />
            </div>
          ))}
        </div>
      )}
      <div className="sr-paper-section-title">
        <Text fw={700} style={{ fontSize: 'var(--sr-font-title)' }}>当前触发</Text>
      </div>
      <div className={'sr-paper-hits' + (hitSignals.length ? ' sr-paper-hits-on' : '')}>
        {hitSignals.length === 0 ? (
          <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>暂无触发信号</Text>
        ) : (
          hitSignals.map((s) => (
            <Badge key={s} color={badgeColor(labelOf(s).color)} radius="sm" style={{ fontWeight: 600 }}>
              {labelOf(s).text}
            </Badge>
          ))
        )}
      </div>
      <div className="sr-paper-section-title">
        <Text fw={700} style={{ fontSize: 'var(--sr-font-title)' }}>最近触发点</Text>
      </div>
      {recentTriggers.length === 0 ? (
        <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>暂无触发记录</Text>
      ) : (
        <>
          <div className="sr-paper-trig-list">
            {recentTriggers.map((t) => (
              <div key={`${t.date ?? t.time ?? ''}:${t.signal}`} className="sr-paper-trig-row">
                <span className="sr-paper-trig-date">{t.date ?? t.time ?? '—'}</span>
                <Badge color={badgeColor(labelOf(t.signal).color)} radius="sm">{labelOf(t.signal).text}</Badge>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

/**
 * 历史价格曲线：消费 watch/history（prices: {code: close}）画多股价格线，多股/单股统一。
 * lightweight-charts LineSeries 每股一条：快照为真实时刻（60s 落库、间隔不规则），
 * time 走 klineTsTime 完整时刻 → UTC 秒（连续时间轴，不再 echarts 字符串 category 轴），
 * 同 series 时间严格升序唯一；非法值点（null/NaN）直接省略（库不接受 null）。
 * 十字线/滚轮缩放走库默认交互；图例轻量自绘（色点+代码名），点击显隐对应曲线。
 */
function PriceChartSection({ history, codes, names, period }: {
  history: PaperWatchHistoryPoint[]
  codes: string[]
  names: Record<string, string>
  period: string
}) {
  const isDark = useThemeStore((s) => s.theme) === 'dark'
  // 渲染期主题令牌（仅图例 JSX 用；effects 内部各自取，依赖用 isDark boolean 保持稳定）
  const t = chartColors(isDark)
  const chartRef = useRef<HTMLDivElement>(null)
  const chartInst = useRef<IChartApi | null>(null)
  /** code → Line series（创建后全部增量更新，不重建图表实例） */
  const seriesMap = useRef<Map<string, ISeriesApi<'Line'>>>(new Map())
  /** code → 已写入 series 的末点（增量 update 起点，P2-73；series 重建/历史回缩时清空重设） */
  const lastPointRef = useRef<Map<string, { time: UTCTimestamp; value: number }>>(new Map())
  /** 用户点击图例隐藏的曲线 code 集合（空集 = 全部显示） */
  const [hiddenCodes, setHiddenCodes] = useState<ReadonlySet<string>>(() => new Set())
  // 渲染期同步 ref：ref callback 依赖保持空（引用稳定，React 不会重复解绑重建图表），
  // 主题/周期/显隐最新值由 ref 读取、各自 effect 增量 applyOptions 接管
  const hiddenRef = useRef(hiddenCodes)
  hiddenRef.current = hiddenCodes
  const isDarkRef = useRef(isDark)
  isDarkRef.current = isDark
  const periodRef = useRef(period)
  periodRef.current = period

  /**
   * 容器 ref 回调（空依赖、引用恒定）：chart 容器只在有数据分支渲染，挂载即建图、卸载即释放；
   * 空态↔有数据切换不重复创建（history 为空时容器不存在，chart 创建随容器挂载发生）。
   */
  const bindChartRef = useCallback((node: HTMLDivElement | null) => {
    chartRef.current = node
    if (!node) {
      chartInst.current?.remove()
      chartInst.current = null
      seriesMap.current.clear()
      lastPointRef.current.clear()
      return
    }
    if (chartInst.current) return
    const tokens = chartColors(isDarkRef.current)
    chartInst.current = createChart(node, {
      autoSize: true, // ResizeObserver 跟随容器尺寸（--sr-chart-h-sm 固定高度）
      layout: {
        background: { type: ColorType.Solid, color: tokens.bg },
        textColor: tokens.axis,
        fontSize: 10,
        attributionLogo: true,
      },
      grid: {
        vertLines: { color: tokens.grid },
        horzLines: { color: tokens.grid },
      },
      rightPriceScale: {
        borderColor: tokens.border,
        autoScale: true,
        scaleMargins: { top: 0.15, bottom: 0.15 },
      },
      timeScale: {
        borderColor: tokens.border,
        timeVisible: isMinutePeriod(periodRef.current),
        secondsVisible: false,
        tickMarkFormatter: klineTickFormatter(periodRef.current),
        rightOffset: 2,
      },
      crosshair: {
        mode: CrosshairMode.Normal, // 自由十字线（与 echarts 时代 axisPointer cross 语义一致）
        vertLine: { color: tokens.crosshair, width: 1, style: LineStyle.Dashed, labelBackgroundColor: tokens.label.bg },
        horzLine: { color: tokens.crosshair, width: 1, style: LineStyle.Dashed, labelBackgroundColor: tokens.label.bg },
      },
      localization: { priceFormatter: (p: number) => fmtNum(p, 2) },
    })
  }, [])

  // 数据同步：series 集合增删 + 增量 update（P2-73，保留可见 logical range，轮询追加数据不跳视野）。
  // 常规轮询 = 末尾追加（快照 60s 落库一条，15s 轮询至多一两个新点），走 series.update 逐条增量，
  // 不再每股全量重建数组 setData；仅新建 series / 末点时间回缩（切项目/清空重建）才 setData。
  // isDark 不在依赖内（主题走下方 applyOptions 增量 effect，避免主题切换触发数据重建）；
  // 新建 series 的颜色经 isDarkRef 取当前主题。
  useEffect(() => {
    const chart = chartInst.current
    if (!chart || history.length === 0) return
    const range = chart.timeScale().getVisibleLogicalRange()
    // 移除已不在 codes 的 series（切项目股票列表变化）；同步清 lastPoint
    for (const code of [...seriesMap.current.keys()]) {
      if (!codes.includes(code)) {
        chart.removeSeries(seriesMap.current.get(code)!)
        seriesMap.current.delete(code)
        lastPointRef.current.delete(code)
      }
    }
    // 恢复 range 的越界保护：range 需落在新数据点数范围内，否则跳过（保持全览）
    let maxLen = 0
    const tokens = chartColors(isDarkRef.current)
    codes.forEach((code, i) => {
      let series = seriesMap.current.get(code)
      if (!series) {
        series = chart.addSeries(LineSeries, {
          color: tokens.lineColors[i % tokens.lineColors.length],
          lineWidth: 2,
          priceLineVisible: false, // 每股一条不画价格线/末值标签（图例承担），避免右轴叠字
          lastValueVisible: false,
          visible: !hiddenRef.current.has(code),
        })
        seriesMap.current.set(code, series)
      }
      // 全部走 klineTsTime（恒 UTCTimestamp），显式收窄 time 供增量比较（LineData.time 是 Time 联合）
      const data: { time: UTCTimestamp; value: number }[] = []
      for (const h of history) {
        if (!h.ts) continue
        const time = klineTsTime(h.ts)
        if (time == null) continue
        const v = h.prices?.[code]
        if (v == null || !Number.isFinite(v)) continue // 非法值省略（lightweight 不接受 null/NaN）
        data.push({ time, value: v })
      }
      if (data.length > maxLen) maxLen = data.length
      const last = data[data.length - 1]
      const prev = lastPointRef.current.get(code)
      if (!prev) {
        // 新建 series / 无基线：全量 setData 一次
        series.setData(data)
        if (last) lastPointRef.current.set(code, { time: last.time, value: last.value })
      } else if (last && last.time > prev.time) {
        // 常规轮询追加：只 update prev 之后的新点（升序，各点 time 唯一）
        for (const p of data) if (p.time > prev.time) series.update(p)
        lastPointRef.current.set(code, { time: last.time, value: last.value })
      } else if (last && last.time === prev.time) {
        // 末点时间未变：值未变（轮询间隔内未落新快照）零成本跳过；值被修正（快照回填）才 update 原位替换
        if (last.value !== prev.value) series.update(last)
        lastPointRef.current.set(code, { time: last.time, value: last.value })
      } else {
        // 历史回缩/重置（切项目/清空重建）：全量 setData 对齐
        series.setData(data)
        if (last) lastPointRef.current.set(code, { time: last.time, value: last.value })
        else lastPointRef.current.delete(code)
      }
    })
    if (range && range.to > 0 && range.from < maxLen) chart.timeScale().setVisibleLogicalRange(range)
  }, [history, codes])

  // 主题/周期：增量 applyOptions（不 setData，保留视野与缩放状态；series 颜色随主题刷新）
  useEffect(() => {
    const chart = chartInst.current
    if (!chart) return
    const tokens = chartColors(isDark)
    chart.applyOptions({
      layout: { background: { type: ColorType.Solid, color: tokens.bg }, textColor: tokens.axis },
      grid: { vertLines: { color: tokens.grid }, horzLines: { color: tokens.grid } },
      rightPriceScale: { borderColor: tokens.border },
      timeScale: {
        borderColor: tokens.border,
        timeVisible: isMinutePeriod(period),
        secondsVisible: false,
        tickMarkFormatter: klineTickFormatter(period),
      },
      crosshair: {
        vertLine: { color: tokens.crosshair, labelBackgroundColor: tokens.label.bg },
        horzLine: { color: tokens.crosshair, labelBackgroundColor: tokens.label.bg },
      },
    })
    seriesMap.current.forEach((s, code) => {
      const i = codes.indexOf(code)
      s.applyOptions({ color: tokens.lineColors[i % tokens.lineColors.length] })
    })
  }, [isDark, period, codes])

  // 图例显隐：仅 applyOptions，不重建/不 setData
  useEffect(() => {
    seriesMap.current.forEach((s, code) => s.applyOptions({ visible: !hiddenCodes.has(code) }))
  }, [hiddenCodes])

  const toggleVisible = useCallback((code: string) => {
    setHiddenCodes((prev) => {
      const next = new Set(prev)
      if (next.has(code)) next.delete(code)
      else next.add(code)
      return next
    })
  }, [])

  if (history.length === 0) {
    return (
      <div className="sr-paper-section">
        <div className="sr-paper-section-title">
          <Text fw={700} style={{ fontSize: 'var(--sr-font-title)' }}>价格走势</Text>
          <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>暂无历史快照（开始监控后每 60 秒落库一条）</Text>
        </div>
        <EmptyState description="开始监控并保持运行一段时间后，此处展示各股收盘价曲线" padding="24px 0" />
      </div>
    )
  }
  return (
    <div className="sr-paper-section">
      <div className="sr-paper-section-title">
        <Text fw={700} style={{ fontSize: 'var(--sr-font-title)' }}>价格走势</Text>
        <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
          最近 {history.length} 个快照 · {periodLabel(period)}
        </Text>
      </div>
      {/* 图例：轻量自绘（色点+代码名），点击显隐对应曲线 */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sr-gap-ctl)', minWidth: 0 }}>
        {codes.map((c, i) => {
          const hidden = hiddenCodes.has(c)
          return (
            <span
              key={c} role="button" tabIndex={0}
              title={hidden ? '点击显示该股曲线' : '点击隐藏该股曲线'}
              onClick={() => toggleVisible(c)}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleVisible(c) } }}
              style={{ display: 'inline-flex', alignItems: 'center', gap: 5, cursor: 'pointer',
                fontSize: 'var(--sr-font-xs)', color: t.axis, opacity: hidden ? 0.45 : 1, userSelect: 'none' }}
            >
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: t.lineColors[i % t.lineColors.length], flex: 'none' }} />
              {names[c] || c}（{c}）
            </span>
          )
        })}
      </div>
      <div className="sr-paper-chart" ref={bindChartRef} />
    </div>
  )
}

interface WatchResultsProps {
  data: PaperWatchResult | PaperMultiWatchResult | null
  /** 项目 id：拉取历史曲线（watch/history）用 */
  projectId: number
  watching: boolean
  /** 首拉监控数据中（无旧快照时展示加载态） */
  loading: boolean
  period: string
  lastUpdatedAt: number | null
  labelOf: (code: string) => { text: string; color: string }
  onFetchWatch: () => void
}

/** 实时监控结果区：价格走势曲线 + 指标快照网格 + 当前触发 + 最近触发点。
 *  单股 = 整体一块；多股 = 每股一块（stocks 循环）。倒计时轮询由主组件 15s 驱动，
 *  历史快照由后端限频（60s）落库，曲线随轮询同频刷新。 */
export default function WatchResults({ data, projectId, watching, loading, period, lastUpdatedAt, labelOf, onFetchWatch }: WatchResultsProps) {
  const [history, setHistory] = useState<PaperWatchHistoryPoint[]>([])
  // T-130:在途请求 pid 标记——响应须与当前请求 pid 一致才写入(切项目时旧项目慢响应
  // 不再覆盖新项目曲线;新请求接管标记,旧请求结果丢弃)
  const historyInflight = useRef<number | null>(null)

  /** 拉取历史快照（watch/history）：停止监控后保留最后数据；切项目/停止时清空 */
  const fetchHistory = useCallback((pid: number) => {
    historyInflight.current = pid
    getPaperWatchHistory(pid, 60)
      .then((res) => {
        if (historyInflight.current !== pid) return
        setHistory(Array.isArray(res.data) ? res.data : [])
      })
      .catch(() => { /* 历史曲线失败静默：快照区仍可用 */ })
      .finally(() => { if (historyInflight.current === pid) historyInflight.current = null })
  }, [])

  useEffect(() => {
    if (!watching || !projectId) return
    setHistory([]) // 启动监控 / 切换项目：重置曲线再拉取；停止监控不清空（保留最后曲线）
    void fetchHistory(projectId)
    const t = window.setInterval(() => {
      if (document.visibilityState === 'hidden') return // 后台不空转,回前台下一 tick 续上
      void fetchHistory(projectId)
    }, WATCH_INTERVAL_MS)
    return () => window.clearInterval(t)
  }, [watching, projectId, fetchHistory])

  // 快照轮询返回（data 更新）后同步刷新 history：首次监控时快照落库发生在 fetchWatch 之后，
  // 若只靠上面 15s interval，曲线要等最多一个轮询周期才出现——data 驱动保证落库后立即出曲线
  useEffect(() => {
    if (watching && projectId && data != null) void fetchHistory(projectId)
  }, [data, watching, projectId, fetchHistory])

  const isMulti = data != null && 'multi' in data && data.multi
  const multi = isMulti ? (data as PaperMultiWatchResult) : null
  const single = !isMulti && data != null ? (data as PaperWatchResult) : null

  /** 曲线股票序列：多股取 stocks 全部，单股取 data.code */
  const chartCodes = useMemo(() => {
    if (multi) return multi.stocks.map((s) => s.code)
    return single?.code ? [single.code] : []
  }, [multi, single])

  const chartNames = useMemo(() => {
    const names: Record<string, string> = {}
    if (multi) multi.stocks.forEach((s) => { names[s.code] = s.name ?? '' })
    else if (single) names[single.code] = single.name ?? ''
    return names
  }, [multi, single])

  // 客户端拉取时刻（epoch ms 绝对时刻）→ 上海时区串：与面板其余 naive 串直解同口径（P2-63
  // 收敛两套时区——formatFullTime number 分支按浏览器本地时区渲染，跨时区会偏，禁用）
  const updatedText = lastUpdatedAt ? formatShanghaiFullTime(lastUpdatedAt, { withYear: false }) : '—'

  if (!watching && !data) {
    return (
      <EmptyState
        description={
          <span>
            确认参数后点击「开始监控」，实时展示指标快照与信号触发。
            <br />
            修改参数并保存后旧结果会失效，需重新开始监控。
          </span>
        }
        padding="40px 0"
      />
    )
  }
  if (loading && !data) {
    return (
      <div className="sr-paper-state">
        <Loader size="sm" />
        <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>拉取监控数据…</Text>
      </div>
    )
  }
  if (!data) {
    return <EmptyState text="监控数据暂不可用" onRetry={onFetchWatch} padding="40px 0" />
  }
  return (
    <div className="sr-paper-results-inner">
      {!watching && (
        <div className="sr-paper-watch-stopped">
          <Badge variant="default" radius="sm">监控已停止</Badge>
          <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
            已停止实时轮询，以下为最后快照（更新于 {updatedText}）
          </Text>
        </div>
      )}
      <PriceChartSection history={history} codes={chartCodes} names={chartNames} period={period} />
      {multi ? (
        multi.stocks.map((st) => (
          <StockWatchBlock key={st.code} stock={st} period={period} labelOf={labelOf} />
        ))
      ) : (
        <StockWatchBlock
          stock={{
            code: single?.code ?? '',
            name: single?.name ?? '',
            period: single?.period ?? period,
            snapshot: single?.snapshot,
            hit_signals: single?.hit_signals,
            recent_triggers: single?.recent_triggers,
            updated_at: single?.updated_at,
          }}
          period={period}
          labelOf={labelOf}
        />
      )}
    </div>
  )
}
