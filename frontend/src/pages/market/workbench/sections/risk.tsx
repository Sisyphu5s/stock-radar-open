import { Badge, SegmentedControl, Text, Tooltip } from '@mantine/core'
import { IconShieldCheck } from '@tabler/icons-react'
import { useCallback, useEffect, useRef } from 'react'
import { useThemeStore } from '../../../../stores/useAppStore'
import { echarts } from '../../../../utils/echartsSetup'
import { fmtPct, pctColor } from '../../../../utils/format'
import { chartTheme } from '../../../../utils/echartsTheme'
import { chartColors } from '../../../../utils/chartTheme'
import { CardShell, CardState, EmptyState } from '../../../../components/ui'
import { useWorkbench } from '../context'
import { toFinite } from './shared'
import { antdToMantine } from '../tagColor'

/** 指标用法（悬停 Tooltip：解释（后端 explanations）+ 用法；未覆盖 key 回退通用提示） */
const RISK_USAGE: Record<string, string> = {
  annual_return: '年化收益：长期赚钱能力，越高越好；结合波动率看风险收益比',
  var95: '单日最大预期损失（95% 置信）：数值越大风险越高，可理解为 95% 概率单日亏损不超过该比例',
  cvar95: '尾部平均损失：超过 VaR(95%) 后的平均亏损，比 VaR 更关注极端情况',
  var99: '更极端置信度（99%）下的单日预期损失',
  var95_param: '正态假设 VaR：收益近似正态时参考；厚尾时低估风险（应看 CF 修正值）',
  var95_cf: 'Cornish-Fisher 修正 VaR：考虑偏度/峰度，非正态分布更准；与参数法差异大说明分布明显偏离正态',
  var99_cf: '99% 置信度的 Cornish-Fisher 修正 VaR（厚尾下更保守，最值得参考的极端损失值）',
  calmar: 'Calmar 比率：年化收益/最大回撤，越高越好（>1 较优）；衡量回撤后的收益质量',
  ulcer_index: 'Ulcer 指数：回撤深度与持续时间的综合度量，越低越好（<5 持有体验良好）',
  tail_risk: '尾部风险比：CVaR95/VaR95，>1.3 表示尾部偏厚、极端损失概率更高',
  annual_volatility: '年化波动率：日收益标准差×√252；<15% 低波动、15-30% 中、>30% 高',
  ewma_volatility: 'EWMA 波动（RiskMetrics）：近期收益权重更高；显著高于年化波动说明近期风险在上升',
  max_drawdown: '最大回撤：历史峰值到谷底的最大跌幅（负值），衡量最坏亏损',
  drawdown_recovery_days: '回撤恢复天数：最大回撤后回到前高所需交易日，越长恢复越慢',
  avg_drawdown: '平均回撤：全部回撤区间的平均深度（负值）',
  sharpe: '夏普比率：每单位总波动的超额收益；>1 良好、>2 优秀',
  sortino: 'Sortino：仅用下行偏差的夏普变体，更关注亏损侧风险；>1 良好',
  downside_deviation: '下行偏差：仅负收益的波动，衡量亏损侧波动幅度',
  beta: 'Beta：相对沪深300 的敏感度；>1 波动大于市场、<1 更抗跌',
}

/** 指标 Tooltip：解释 + 用法（多行） */
const usageTooltip = (k: string, l: string, explanations?: Record<string, string>): React.ReactNode => (
  <div>
    <div>{explanations?.[k] ?? l}</div>
    <div style={{ marginTop: 2, opacity: 0.85 }}>用法：{RISK_USAGE[k] ?? '数值含义见左侧解释'}</div>
  </div>
)

/** 风险指标（含收益分布直方图） */
export function RiskSection() {
  const c = useWorkbench()
  const theme = useThemeStore((st) => st.theme)
  const isDark = theme === 'dark'
  const { up: UP, down: DOWN } = chartTheme(isDark)
  const { axis: AXIS, grid: GRID } = chartColors(isDark)
  const distRef = useRef<HTMLDivElement | null>(null)
  const distChart = useRef<echarts.ECharts | null>(null)
  // init 绑定标记：记录已 init 绑定的容器，容器条件渲染重建时重挂（避免旧实例残留）
  const distInitedEl = useRef<HTMLDivElement | null>(null)
  const { risk, errRisk, riskDays, setRiskDays, onRetry, loading, riskSummary } = c

  // 收益分布直方图渲染（canvas 颜色按主题 JS 常量切换；chart 未 init 时无操作）
  const renderDist = useCallback(() => {
    const chart = distChart.current
    if (!chart) return
    const rets = (risk?.ret_series ?? []).filter((v: number) => Number.isFinite(v)) as number[]
    if (!rets.length) {
      chart.setOption({
        grid: { left: 46, right: 8, top: 8, bottom: 24 },
        xAxis: { type: 'category', data: [], name: '日收益(%)', nameTextStyle: { color: AXIS, fontSize: 9 }, axisLabel: { color: AXIS, fontSize: 9 } },
        yAxis: { type: 'value', name: '频数', nameTextStyle: { color: AXIS, fontSize: 9 }, splitLine: { lineStyle: { color: GRID } }, axisLabel: { color: AXIS, fontSize: 9 } },
        series: [{ type: 'bar', data: [] }],
      }, true)
      return
    }
    const nBuckets = Math.min(30, Math.max(20, Math.floor(rets.length / 8)))
    const min = Math.min(...rets)
    const max = Math.max(...rets)
    const span = max - min
    const binW = span > 0 ? span / nBuckets : 1
    const counts = new Array(nBuckets).fill(0) as number[]
    for (const v of rets) {
      let i = Math.floor((v - min) / binW)
      if (i < 0) i = 0
      if (i >= nBuckets) i = nBuckets - 1
      counts[i]++
    }
    const labels = counts.map((_, i) => {
      const lo = min + i * binW
      const hi = lo + binW
      return `${(lo * 100).toFixed(1)}~${(hi * 100).toFixed(1)}%`
    })
    // 参考线：零线（红绿分界，灰实线）+ VaR(95%) 历史模拟值（红虚线）——映射到桶索引
    const zeroIdx = span > 0 ? Math.max(0, Math.min(nBuckets - 1, Math.round((0 - min) / binW))) : null
    const var95v = toFinite(risk?.var95)
    const varIdx = var95v != null && span > 0 ? Math.max(0, Math.min(nBuckets - 1, Math.round((var95v - min) / binW))) : null
    const markLineData = [
      ...(zeroIdx != null ? [{ xAxis: zeroIdx, lineStyle: { color: GRID, width: 1 }, label: { show: false } }] : []),
      ...(varIdx != null ? [{ xAxis: varIdx, lineStyle: { color: DOWN, width: 1, type: 'dashed', opacity: 0.7 }, label: { formatter: 'VaR95', color: DOWN, fontSize: 9 } }] : []),
    ]
    chart.setOption({
      grid: { left: 46, right: 8, top: 8, bottom: 24 },
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: (ps: any) => `${ps[0]?.axisValue}<br/>频数: ${ps[0]?.value ?? 0}`,
      },
      xAxis: {
        type: 'category', data: labels, name: '日收益(%)',
        nameTextStyle: { color: AXIS, fontSize: 9 },
        axisLabel: { color: AXIS, fontSize: 9, hideOverlap: true },
        axisLine: { lineStyle: { color: GRID } },
      },
      yAxis: {
        type: 'value', name: '频数', nameTextStyle: { color: AXIS, fontSize: 9 }, minInterval: 1,
        splitLine: { lineStyle: { color: GRID } },
        axisLabel: { color: AXIS, fontSize: 9 },
      },
      series: [{
        type: 'bar', barMaxWidth: 16,
        data: counts.map((count, i) => {
          const mid = min + (i + 0.5) * binW
          return { value: count, itemStyle: { color: mid >= 0 ? UP : DOWN } }
        }),
        markLine: markLineData.length ? {
          symbol: 'none', silent: true,
          label: { position: 'insideEndTop' },
          data: markLineData,
        } : undefined,
      }],
    }, true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [risk, isDark])

  // init + 渲染：直方图容器是条件渲染（risk 数据到达后出现），mount 时容器不存在——
  // 固定 deps 的 effect 会空跑一次永久错过（风险图不显示根因）。挂在 renderDist
  // （risk/isDark 变化）上：容器存在才 init/重绘；元素重挂时先 dispose 旧实例再 init。
  // 容器有显式高度（--sr-chart-h-dist, 120px 兜底）+ block 宽度，滚动流下尺寸恒定，
  // 无需原 IO 惰性 init。
  useEffect(() => {
    const el = distRef.current
    if (!el) return
    if (distInitedEl.current !== el) {
      if (distChart.current) distChart.current.dispose()
      distChart.current = echarts.getInstanceByDom(el) ?? echarts.init(el)
      distInitedEl.current = el
    }
    renderDist()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [renderDist])

  // 容器尺寸变化 → chart.resize()：与 init 同根因，容器出现后才绑定（chart 未 init 时无操作）
  const distVisible = (risk?.ret_series?.length ?? 0) > 0
  useEffect(() => {
    const el = distRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => {
      const c = distChart.current
      if (c && !c.isDisposed()) c.resize()
    })
    ro.observe(el)
    return () => ro.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [distVisible])

  // 卸载时销毁实例
  useEffect(() => () => {
    distChart.current?.dispose()
    distChart.current = null
  }, [])

  // 首载加载态：数据未到时显示 Spin，避免空白区
  if (!risk && !errRisk) {
    if (loading) {
      return (
        <CardShell icon={<IconShieldCheck size={18} />} title="风险指标">
          <CardState loading minHeight={80} loadingText="加载中…">{null}</CardState>
        </CardShell>
      )
    }
    return null
  }

  // 风险综合评级：由 useWorkbenchData 派生（computeRiskGrade 唯一实现，与「综合」tab / K 线叠加契约共用）
  const riskGrade = riskSummary.grade

  return (
    <CardShell icon={<IconShieldCheck size={18} />} title="风险指标"
      extra={<SegmentedControl size="xs" value={riskDays} onChange={(v) => setRiskDays(Number(v))}
        data={[{ label: '全部', value: 0 }, { label: '60日', value: 60 }, { label: '120日', value: 120 }, { label: '250日', value: 250 }]} />}>
      {errRisk && !risk ? (
        <EmptyState text={errRisk} onRetry={onRetry} />
      ) : risk && (
        <div>
          {/* 风险数值清洗：NaN/Infinity 统一显示 '—' */}
          {(() => {
            const tailRisk = toFinite(risk.tail_risk)
            const sharpe = toFinite(risk.sharpe)
            const sortino = toFinite(risk.sortino)
            const beta = toFinite(risk.beta)
            const recoveryDays = toFinite(risk.drawdown_recovery_days)
            interface RiskCard { k: string; l: string; v: string; c?: string; s?: string; full?: boolean }
            // 分组行式平铺（T-116 改版：原 MetricStat 卡片网格——有/无 sub 使同排卡片高矮参差、
            // 内容长短驱动视觉凌乱——改为成对行式：宽度够则两列、相邻两项=一起看的指标对同排贴邻，
            // label + value 紧贴、sub 浅色小字缀于数值之后；组内顺序按叙事重排：
            // 收益 → 波动 → 回撤 → 尾部 → 风险调整 → 市场敏感度；组内奇数项以 sr-wb-row-full 横跨整行）。
            // 核心 6 项（3 对）：收益/波动对（风险收益对）→ 回撤/VaR对（最坏亏损+单日损失）→ 夏普/Beta对（风险调整+市场敏感）
            const coreCards: RiskCard[] = [
              { k: 'annual_return', l: '年化收益', v: fmtPct(toFinite(risk.annual_return), 1), c: pctColor(toFinite(risk.annual_return) ?? 0) },
              { k: 'annual_volatility', l: '年化波动', v: fmtPct(toFinite(risk.annual_volatility), 1) },
              { k: 'max_drawdown', l: '最大回撤', v: fmtPct(toFinite(risk.max_drawdown), 1), c: 'var(--sr-accent)' },
              { k: 'var95', l: 'VaR(95%)', v: fmtPct(toFinite(risk.var95), 2), c: 'var(--sr-warning)' },
              { k: 'sharpe', l: '夏普', v: sharpe != null ? String(sharpe) : '—', c: pctColor(sharpe ?? 0) },
              { k: 'beta', l: 'Beta', v: beta != null ? String(beta) : '—', s: beta != null ? (beta > 1 ? '强于市场' : '弱于市场') : undefined },
            ]
            // 更多基础 8 项（4 对）：Sortino/下行偏差（风险调整变体+亏损侧）→ CVaR95/VaR99（尾部递进）→
            // 参数法/尾部风险（参数法+尾部风险比）→ 平均回撤/回撤恢复（回撤补充）
            const moreCards: RiskCard[] = [
              { k: 'sortino', l: 'Sortino', v: sortino != null ? String(sortino) : '—', c: pctColor(sortino ?? 0) },
              { k: 'downside_deviation', l: '下行偏差', v: fmtPct(toFinite(risk.downside_deviation), 1) },
              { k: 'cvar95', l: 'CVaR(95%)', v: fmtPct(toFinite(risk.cvar95), 2), c: 'var(--sr-warning)' },
              { k: 'var99', l: 'VaR(99%)', v: fmtPct(toFinite(risk.var99), 2), c: 'var(--sr-warning)' },
              { k: 'var95_param', l: 'VaR参数法', v: fmtPct(toFinite(risk.var95_param), 2) },
              { k: 'tail_risk', l: '尾部风险', v: tailRisk != null ? String(tailRisk) : '—', s: tailRisk != null ? (tailRisk > 1.3 ? '尾部偏厚' : '尾部正常') : undefined },
              { k: 'avg_drawdown', l: '平均回撤', v: fmtPct(toFinite(risk.avg_drawdown), 1), c: 'var(--sr-accent)' },
              { k: 'drawdown_recovery_days', l: '回撤恢复', v: recoveryDays != null ? recoveryDays + ' 天' : '—' },
            ]
            // 高级 5 项：EWMA 波动（与核心年化波动对比看的近期波动趋势，单项横跨整行）→
            // VaR-CF修正/VaR-CF(99%)（Cornish-Fisher 修正 95/99 递进对）→ Calmar/Ulcer指数（回撤质量/持有体验对）
            const ewma = toFinite(risk.ewma_volatility)
            const annual = toFinite(risk.annual_volatility)
            const ewmaTrend = ewma != null && annual != null && annual > 0
              ? (ewma / annual > 1.05 ? '近期波动↑' : ewma / annual < 0.95 ? '近期波动↓' : '近期平稳')
              : undefined
            const advancedCards: RiskCard[] = [
              { k: 'ewma_volatility', l: 'EWMA波动', v: fmtPct(ewma, 1), c: (ewma ?? 0) >= (annual ?? 0) ? 'var(--sr-warning)' : 'var(--sr-text-1)', s: ewmaTrend, full: true },
              { k: 'var95_cf', l: 'VaR-CF修正', v: fmtPct(toFinite(risk.var95_cf), 2) },
              { k: 'var99_cf', l: 'VaR-CF(99%)', v: fmtPct(toFinite(risk.var99_cf), 2) },
              { k: 'calmar', l: 'Calmar', v: toFinite(risk.calmar) != null ? String(toFinite(risk.calmar)) : '—', c: pctColor(toFinite(risk.calmar) ?? 0) },
              { k: 'ulcer_index', l: 'Ulcer指数', v: toFinite(risk.ulcer_index) != null ? String(toFinite(risk.ulcer_index)) : '—' },
            ]
            // 成对行式（sr-wb-rows）：auto-fill 两列网格，相邻两项同排贴邻（数组顺序=成对语义）；
            // 单行等高不受内容长短影响；sub 浅色小字缀于数值之后；'—' 缺失值淡显；
            // full 项（sr-wb-row-full）横跨整行承载组内单数
            const renderRows = (list: RiskCard[]) => (
              <div className="sr-wb-rows">
                {list.map((x) => (
                  <Tooltip key={x.k} label={usageTooltip(x.k, x.l, risk.explanations)}>
                    <div className={x.full ? 'sr-wb-row sr-wb-row-full' : 'sr-wb-row'} style={{ cursor: 'help' }}>
                      <span className="sr-wb-row-label">{x.l}</span>
                      <span className="sr-wb-row-value" style={{ color: x.c ?? (x.v === '—' ? 'var(--sr-text-3)' : 'var(--sr-text-1)') }}>
                        {x.v}
                        {x.s != null && <span className="sr-wb-row-sub">{x.s}</span>}
                      </span>
                    </div>
                  </Tooltip>
                ))}
              </div>
            )
            // 板块 = 分组小标题 + 行式网格，衬线框区隔（sr-wb-group）
            const renderGroup = (title: string, list: RiskCard[]) => (
              <div className="sr-wb-group">
                <Text span size="xs" className="sr-wb-sec-text" style={{ color: 'var(--sr-text-2)', display: 'block', margin: '0 0 var(--sr-pad-xs)' }}>
                  {title}（{list.length}）
                </Text>
                {renderRows(list)}
              </div>
            )
            // 汇总键值对：label 淡显 + 值强调（sr-wb-summary，非整行小字）
            const summaryKv = (k: string, v: string) => (
              <span key={k} className="sr-wb-kv">
                <span className="sr-wb-kv-k">{k}</span>
                <span className="sr-wb-kv-v">{v}</span>
              </span>
            )
            return (
              <div>
                {riskGrade && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 'var(--sr-gap-card)' }}>
                    <Tooltip label={`${riskGrade.dims.map((d) => `${d.l} ${d.s}/2`).join(' · ')}（总分 ${riskGrade.dims.reduce((a, d) => a + d.s, 0)}/8）`}>
                      <Badge variant="light" color={antdToMantine(riskGrade.color)} radius="var(--sr-radius-tag)" size="sm"
                        style={{ fontWeight: 600, cursor: 'help' }}>{riskGrade.tag}</Badge>
                    </Tooltip>
                    <Text span size="xs" className="sr-wb-sec-text" style={{ color: 'var(--sr-text-3)' }}>{riskGrade.desc}</Text>
                  </div>
                )}
                <Text span size="xs" className="sr-wb-sec-text" style={{ color: 'var(--sr-text-3)', display: 'block', marginBottom: 'var(--sr-gap-card)' }}>
                  基于日线收盘计算，截止最近交易日{risk.window != null ? `（数据窗口 ${risk.window} 根K线）` : ''}
                </Text>
                {renderGroup('核心指标', coreCards)}
                {renderGroup('更多基础指标', moreCards)}
                {renderGroup('高级指标', advancedCards)}
                {(risk.ret_series?.length ?? 0) > 0 && (
                  <div className="sr-wb-group">
                    <Text span size="xs" className="sr-wb-sec-text" style={{ color: 'var(--sr-text-3)', display: 'block', marginBottom: 'var(--sr-pad-xs)' }}>
                      收益分布{risk.window != null ? `（${risk.window} 期）` : ''}
                    </Text>
                    <div ref={distRef} style={{ height: 'var(--sr-chart-h-dist, 120px)' }} />
                  </div>
                )}
                <div className="sr-wb-summary">
                  {summaryKv('样本', risk.periods != null ? `${risk.periods} 期` : '—')}
                  {summaryKv('偏度', toFinite(risk.skewness) != null ? String(toFinite(risk.skewness)) : '—')}
                  {summaryKv('超额峰度', toFinite(risk.kurtosis) != null ? String(toFinite(risk.kurtosis)) : '—')}
                  {summaryKv('胜率', fmtPct(toFinite(risk.win_rate), 0))}
                  {summaryKv('最长连跌', `${risk.max_loss_streak} 日`)}
                  {summaryKv('Alpha', toFinite(risk.alpha) != null ? String(toFinite(risk.alpha)) : '—')}
                  {toFinite(risk.var_pct_budget) != null && summaryKv('风险预算建议仓位', `≤ ${toFinite(risk.var_pct_budget)}%`)}
                </div>
              </div>
            )
          })()}
        </div>
      )}
    </CardShell>
  )
}
