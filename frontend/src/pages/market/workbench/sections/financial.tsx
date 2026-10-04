import { Button, Tabs, Text } from '@mantine/core'
import { IconChartBar } from '@tabler/icons-react'
import { useMemo, useState } from 'react'
import type { FinancialHistoryRow } from '../../../../api/client'
import { CardShell, CardState, DataTable, EmptyState, MetricStat } from '../../../../components/ui'
import { useFinancialHistoryState, useFinancialLatestState } from '../../../../data/financials'
import { useWorkbenchUi } from '../../../../stores/useWorkbenchUi'
import { fmtAmount, fmtMoneyWithUnit, fmtNum, pctColor } from '../../../../utils/format'
import { useWorkbench } from '../context'
import { toFinite, pctDelta } from './shared'

/** 财务/估值字段元数据（前端展示单一事实源：键名对齐后端 financials_* / valuation_history 列） */
interface FinFieldMeta {
  key: string
  label: string
  fmt: 'money' | 'pct' | 'num' | 'mv' // 金额(元)/百分数值/普通数字/市值(亿)
}

const FIN_FIELDS: Record<'balance' | 'income' | 'cash', FinFieldMeta[]> = {
  balance: [
    { key: 'monetary_funds', label: '货币资金', fmt: 'money' },
    { key: 'accounts_receivable', label: '应收账款', fmt: 'money' },
    { key: 'inventories', label: '存货', fmt: 'money' },
    { key: 'fixed_assets', label: '固定资产', fmt: 'money' },
    { key: 'intangible_assets', label: '无形资产', fmt: 'money' },
    { key: 'goodwill', label: '商誉', fmt: 'money' },
    { key: 'total_current_assets', label: '流动资产合计', fmt: 'money' },
    { key: 'total_assets', label: '资产总计', fmt: 'money' },
    { key: 'total_current_liab', label: '流动负债合计', fmt: 'money' },
    { key: 'total_liabilities', label: '负债合计', fmt: 'money' },
    { key: 'total_equity', label: '所有者权益合计', fmt: 'money' },
    { key: 'parent_equity', label: '归母所有者权益', fmt: 'money' },
  ],
  income: [
    { key: 'revenue', label: '营业收入', fmt: 'money' },
    { key: 'operating_cost', label: '营业成本', fmt: 'money' },
    { key: 'gross_margin', label: '毛利率', fmt: 'pct' },
    { key: 'net_margin', label: '净利率', fmt: 'pct' },
    { key: 'operating_profit', label: '营业利润', fmt: 'money' },
    { key: 'total_profit', label: '利润总额', fmt: 'money' },
    { key: 'net_profit', label: '净利润', fmt: 'money' },
    { key: 'parent_net_profit', label: '归母净利润', fmt: 'money' },
    { key: 'eps', label: '每股收益', fmt: 'num' },
  ],
  cash: [
    { key: 'net_operate_cash', label: '经营现金流净额', fmt: 'money' },
    { key: 'net_invest_cash', label: '投资现金流净额', fmt: 'money' },
    { key: 'net_finance_cash', label: '筹资现金流净额', fmt: 'money' },
    { key: 'cce_add', label: '现金净增加额', fmt: 'money' },
  ],
}

/** 估值序列列（市值单位亿） */
const VAL_FIELDS: FinFieldMeta[] = [
  { key: 'pe', label: 'PE(TTM)', fmt: 'num' },
  { key: 'pb', label: 'PB', fmt: 'num' },
  { key: 'ps', label: 'PS', fmt: 'num' },
  { key: 'total_mv', label: '总市值', fmt: 'mv' },
  { key: 'float_mv', label: '流通市值', fmt: 'mv' },
]

/** 估值迷你图可选指标 */
const VAL_METRICS = ['pe', 'pb', 'ps'] as const

const VAL_METRIC_LABEL: Record<string, string> = { pe: 'PE(TTM)', pb: 'PB', ps: 'PS' }

const TAB_META: { key: string; label: string; type: 'balance' | 'income' | 'cash' | 'valuation' | null }[] = [
  { key: 'overview', label: '概览', type: null },
  { key: 'balance', label: '资产负债表', type: 'balance' },
  { key: 'income', label: '利润表', type: 'income' },
  { key: 'cash', label: '现金流量', type: 'cash' },
  { key: 'valuation', label: '估值', type: 'valuation' },
]

/** 历史行日期键：三表 report_date / 估值 date（后端按各自维度倒序返回） */
const rowDate = (r: FinancialHistoryRow): string => String(r.date ?? r.report_date ?? '')

/** 数值清洗 + 按字段口径格式化（金额元 / 百分数值 / 市值亿 / 普通数字） */
function fmtField(v: unknown, meta: FinFieldMeta): string {
  const n = toFinite(v)
  if (n == null) return '—'
  switch (meta.fmt) {
    case 'money':
      return fmtAmount(n)
    case 'pct':
      return n.toFixed(2) + '%'
    case 'mv':
      return fmtNum(n, 1) + '亿'
    default:
      return fmtNum(n, 2)
  }
}

/** 估值序列迷你图（无依赖 SVG 折线，--sr-* 令牌着色；values 按时间升序传入） */
function Sparkline({ values, color, label }: { values: (number | null)[]; color: string; label: string }) {
  const pts = useMemo(() => {
    const nums = values.filter((v): v is number => v != null && Number.isFinite(v))
    if (nums.length < 2) return null
    const w = 260
    const h = 64
    const pad = 3
    const min = Math.min(...nums)
    const max = Math.max(...nums)
    const span = max - min || 1
    return values
      .map((v, i) => {
        if (v == null || !Number.isFinite(v)) return null
        const x = pad + (i / (values.length - 1)) * (w - pad * 2)
        const y = pad + (1 - (v - min) / span) * (h - pad * 2)
        return `${x.toFixed(1)},${y.toFixed(1)}`
      })
      .filter((s): s is string => s != null)
      .join(' ')
  }, [values])
  if (!pts) return null
  return (
    <div>
      <Text size="xs" style={{ color: 'var(--sr-text-3)', marginBottom: 4 }}>{label}</Text>
      <svg width={260} height={64} viewBox="0 0 260 64" style={{ display: 'block' }} aria-label={`${label} 序列迷你图`}>
        <polyline points={pts} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      </svg>
    </div>
  )
}

/** 财务历史 Tab（T-06）：概览（最近两期摘要 + 三表最新一期）/ 三表历史 / 估值序列 */
export function FinancialSection() {
  const c = useWorkbench()
  const { financials, errFinancials, onRetry, loading } = c
  // B2:Tab 状态提升至 useWorkbenchUi store(桌面/窄屏布局翻转整树重挂不复位,切股保持)
  const tab = useWorkbenchUi((s) => s.financialTab)
  const setTab = useWorkbenchUi((s) => s.setFinancialTab)
  const [valMetric, setValMetric] = useState<'pe' | 'pb' | 'ps'>('pe')

  const activeType = TAB_META.find((t) => t.key === tab)?.type ?? null
  // 三表/估值池：仅激活对应 Tab 时订阅（enabled=false 不发起请求、不轮询）
  const histQ = useFinancialHistoryState(c.code, activeType ?? 'balance', 40, activeType != null)
  const latestQ = useFinancialLatestState(c.code)

  // 历史表格：报告期/日期列 + 按元数据顺序的字段列（右对齐数值）
  const fields = activeType === 'valuation' ? VAL_FIELDS : (activeType ? FIN_FIELDS[activeType] : [])
  const tableCols = useMemo(() => [
    {
      key: 'date',
      title: activeType === 'valuation' ? '日期' : '报告期',
      width: 100,
      render: (_v: unknown, r: FinancialHistoryRow) => rowDate(r),
    },
    ...fields.map((m) => ({
      key: m.key,
      title: m.label,
      align: 'right' as const,
      render: (v: unknown) => fmtField(v, m),
    })),
  ], [activeType, fields])

  // 估值迷你图序列（倒序 → 升序，从左到右为时间推进）
  const valSeries = useMemo(() => {
    const rows = histQ.value?.data ?? []
    return rows.slice().reverse().map((r) => toFinite(r[valMetric]))
  }, [histQ.value, valMetric])

  return (
    <CardShell icon={<IconChartBar size={18} />} title="财务历史">
      <Tabs value={tab} onChange={(k) => k && setTab(k)}>
        <Tabs.List style={{ marginBottom: 'var(--sr-gap-card)' }}>
          {TAB_META.map((t) => (
            <Tabs.Tab key={t.key} value={t.key}>{t.label}</Tabs.Tab>
          ))}
        </Tabs.List>

        {/* ===== 概览：最近两期财务摘要 + 三表最新一期关键字段 ===== */}
        <Tabs.Panel value="overview">
          {errFinancials && !financials ? (
            <EmptyState text={errFinancials} onRetry={onRetry} />
          ) : !financials && loading ? (
            <CardState loading minHeight={80} loadingText="加载中…">{null}</CardState>
          ) : financials ? (
            <>
              <div className="sr-wb-grid">
                {(['营业总收入', '归母净利润', '净利润', '净资产收益率', '销售毛利率', '营业利润率', '总资产周转率', '每股净资产'] as const).map((k) => {
                  if (financials[k] == null) return null
                  const cur = financials[k]
                  const prev = financials[k + '@prev']
                  const isPct = k.includes('率')
                  const num = toFinite(cur)
                  const delta = pctDelta(cur, prev)
                  const curText = num == null
                    ? '—'
                    : isPct
                      ? num.toFixed(2) + '%'
                      : k === '每股净资产'
                        ? fmtNum(num, 4) + (String(cur).match(/[万亿万元]+$/)?.[0] ?? '')
                        : fmtMoneyWithUnit(num, String(cur))
                  return (
                    <MetricStat
                      key={k}
                      label={k}
                      value={curText}
                      sub={delta != null ? (
                        <span style={{ fontSize: 'var(--sr-font-xs)', color: pctColor(delta) }}>
                          {delta >= 0 ? '▲' : '▼'}{Math.abs(delta * 100).toFixed(1)}%
                        </span>
                      ) : undefined}
                    />
                  )
                })}
              </div>
              <Text span size="xs" className="sr-wb-sec-text" style={{ color: 'var(--sr-text-3)', marginTop: 'var(--sr-gap-card)', display: 'block' }}>
                报告期 {financials.period}{financials.prev_period ? `（对比 ${financials.prev_period}）` : ''}
              </Text>
            </>
          ) : null}
          <LatestSummary latest={latestQ.value} loading={latestQ.loading} onRetry={onRetry} />
        </Tabs.Panel>

        {/* ===== 三表 / 估值历史 ===== */}
        <Tabs.Panel value={activeType ?? 'balance'}>
          {activeType === 'valuation' ? (
            <div style={{ marginBottom: 'var(--sr-gap-card)' }}>
              <div style={{ display: 'flex', gap: 8, marginBottom: 'var(--sr-gap-card)' }}>
                {VAL_METRICS.map((m) => (
                  <Button
                    key={m}
                    size="xs"
                    variant={valMetric === m ? 'filled' : 'subtle'}
                    onClick={() => setValMetric(m)}
                  >
                    {VAL_METRIC_LABEL[m]}
                  </Button>
                ))}
              </div>
              <Sparkline values={valSeries} color="var(--sr-accent)" label={`${VAL_METRIC_LABEL[valMetric]} 近 ${valSeries.length} 个交易日`} />
            </div>
          ) : null}
          {histQ.error && !histQ.value ? (
            <EmptyState text="财务历史加载失败" onRetry={onRetry} />
          ) : !histQ.value && histQ.loading ? (
            <CardState loading minHeight={80} loadingText="加载中…">{null}</CardState>
          ) : (
            <DataTable
              columns={tableCols}
              dataSource={histQ.value?.data ?? []}
              rowKey={(r: FinancialHistoryRow) => rowDate(r)}
              pagination={{ pageSize: 15, hideOnSinglePage: true, showTotal: (t: number) => `共 ${t} 期` }}
              fillWidth
            />
          )}
        </Tabs.Panel>
      </Tabs>
    </CardShell>
  )
}

/** 概览 Tab：三表最新一期关键字段（balance/cash 为 T-06 新增维度，income 与上方摘要互补） */
function LatestSummary({ latest, loading, onRetry }: { latest: ReturnType<typeof useFinancialLatestState>['value']; loading: boolean; onRetry: () => void }) {
  if (loading && !latest) {
    return <CardState loading minHeight={60} loadingText="最新一期加载中…">{null}</CardState>
  }
  if (!latest || (latest.balance == null && latest.income == null && latest.cash == null)) {
    return (
      <div style={{ marginTop: 'var(--sr-gap-card)' }}>
        <EmptyState text={loading ? '加载中…' : '暂无财务历史数据'} onRetry={loading ? undefined : onRetry} />
      </div>
    )
  }
  const blocks: { title: string; rows: { label: string; value: string }[] }[] = []
  const b = latest.balance
  if (b) {
    blocks.push({
      title: `资产负债表（${b.report_date ?? ''}）`,
      rows: [
        { label: '货币资金', value: fmtField(b.monetary_funds, { key: '', label: '', fmt: 'money' }) },
        { label: '资产总计', value: fmtField(b.total_assets, { key: '', label: '', fmt: 'money' }) },
        { label: '负债合计', value: fmtField(b.total_liabilities, { key: '', label: '', fmt: 'money' }) },
        { label: '归母权益', value: fmtField(b.parent_equity, { key: '', label: '', fmt: 'money' }) },
        { label: '存货', value: fmtField(b.inventories, { key: '', label: '', fmt: 'money' }) },
      ],
    })
  }
  const i = latest.income
  if (i) {
    blocks.push({
      title: `利润表（${i.report_date ?? ''}）`,
      rows: [
        { label: '营业收入', value: fmtField(i.revenue, { key: '', label: '', fmt: 'money' }) },
        { label: '归母净利润', value: fmtField(i.parent_net_profit, { key: '', label: '', fmt: 'money' }) },
        { label: '毛利率', value: fmtField(i.gross_margin, { key: '', label: '', fmt: 'pct' }) },
        { label: '净利率', value: fmtField(i.net_margin, { key: '', label: '', fmt: 'pct' }) },
        { label: '每股收益', value: fmtField(i.eps, { key: '', label: '', fmt: 'num' }) },
      ],
    })
  }
  const cash = latest.cash
  if (cash) {
    blocks.push({
      title: `现金流量表（${cash.report_date ?? ''}）`,
      rows: [
        { label: '经营现金流', value: fmtField(cash.net_operate_cash, { key: '', label: '', fmt: 'money' }) },
        { label: '投资现金流', value: fmtField(cash.net_invest_cash, { key: '', label: '', fmt: 'money' }) },
        { label: '筹资现金流', value: fmtField(cash.net_finance_cash, { key: '', label: '', fmt: 'money' }) },
        { label: '现金净增加', value: fmtField(cash.cce_add, { key: '', label: '', fmt: 'money' }) },
      ],
    })
  }
  return (
    <div style={{ marginTop: 'var(--sr-gap-card)', display: 'grid', gap: 'var(--sr-gap-card)' }}>
      {blocks.map((blk) => (
        <CardShell key={blk.title} title={blk.title}>
          <div className="sr-wb-grid">
            {blk.rows.map((r) => (
              <MetricStat key={r.label} label={r.label} value={r.value} />
            ))}
          </div>
        </CardShell>
      ))}
    </div>
  )
}
