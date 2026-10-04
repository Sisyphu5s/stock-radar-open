import { IconChartBar } from '@tabler/icons-react'
import { CardShell, CardState, EmptyState, MetricStat } from '../../../../components/ui'
import { useWorkbench } from '../context'
import { fmtAmount, fmtNum, fmtPct } from '../../../../utils/format'

/** 基本面 · 估值 */
export function FundamentalSection() {
  const c = useWorkbench()
  const { fundamental, errFund, onRetry, loading } = c
  if (errFund) {
    return (
      <CardShell icon={<IconChartBar size={18} />} title="基本面 · 估值">
        <EmptyState text={errFund} onRetry={onRetry} />
      </CardShell>
    )
  }
  // 首载加载态：数据未到时显示 Spin，避免空白区
  if (!fundamental) {
    if (loading) {
      return (
        <CardShell icon={<IconChartBar size={18} />} title="基本面 · 估值">
          <CardState loading minHeight={80} loadingText="加载中…">{null}</CardState>
        </CardShell>
      )
    }
    return null
  }
  return (
    <CardShell icon={<IconChartBar size={18} />} title="基本面 · 估值">
      <div className="sr-wb-grid">
        {[
          // PE 缺失/0 → 值 '—' 且不产标注（缺失≠亏损）；负值才标「亏损」，正值按区间估高低
          { l: '市盈率 PE', v: fundamental.pe ? fmtNum(fundamental.pe, 1) : '—', s: fundamental.pe == null || fundamental.pe === 0 ? undefined : (fundamental.pe < 0 ? '亏损' : fundamental.pe < 15 ? '低估' : fundamental.pe < 35 ? '合理' : '偏高') },
          { l: '市净率 PB', v: fundamental.pb ? fmtNum(fundamental.pb, 1) : '—' },
          { l: '总市值', v: fundamental.market_cap ? fmtAmount(fundamental.market_cap * 1e8) : '—' },
          { l: '流通市值', v: fundamental.float_cap ? fmtAmount(fundamental.float_cap * 1e8) : '—' },
          { l: '换手率', v: fundamental.turnover_rate != null ? fmtPct(fundamental.turnover_rate / 100, 1) : '—' },
        ].map((x) => (
          <MetricStat
            key={x.l}
            label={x.l}
            value={x.v}
            sub={x.s ? <span style={{ fontSize: 'var(--sr-font-xs)', color: x.s === '低估' ? 'var(--sr-success)' : x.s === '偏高' ? 'var(--sr-error)' : 'var(--sr-text-3)' }}>{x.s}</span> : undefined}
          />
        ))}
      </div>
    </CardShell>
  )
}
