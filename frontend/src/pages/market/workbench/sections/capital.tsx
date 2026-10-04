import { Tabs, Text } from '@mantine/core'
import { IconChartCandle } from '@tabler/icons-react'
import { useMemo } from 'react'
import type { CapitalHistoryRow, CapitalType } from '../../../../api/capital'
import { CardShell, CardState, DataTable, EmptyState } from '../../../../components/ui'
import { useCapitalHistoryState, invalidateCapital } from '../../../../data/capital'
import { useWorkbenchUi } from '../../../../stores/useWorkbenchUi'
import { fmtAmount, fmtNum } from '../../../../utils/format'
import { useWorkbench } from '../context'
import { toFinite } from './shared'

/** 资金类 Tab 配置（单一事实源：Tab 渲染 + 订阅类型共用） */
const TAB_META: { key: CapitalType; label: string }[] = [
  { key: 'moneyflow', label: '资金流' },
  { key: 'lhb', label: '龙虎榜' },
  { key: 'margin', label: '两融' },
  { key: 'northbound', label: '北向持股' },
]

/** 金额列（元 → 万/亿/万亿 自适应），净额正负分别着色（--sr-up/--sr-down） */
function MoneyCell({ v }: { v: unknown }) {
  const n = toFinite(v)
  if (n == null) return <span>—</span>
  return (
    <span style={{ color: n > 0 ? 'var(--sr-up)' : n < 0 ? 'var(--sr-down)' : undefined }}>
      {fmtAmount(n)}
    </span>
  )
}

/** 序列迷你图（无依赖 SVG 折线，--sr-* 令牌着色；values 按时间升序传入） */
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

/** 资金 section（T-08）：资金流（五档净流入+主力迷你图）/ 龙虎榜 / 两融 / 北向历史持股 四 Tab */
export function CapitalSection() {
  const c = useWorkbench()
  // B2:Tab 状态提升至 useWorkbenchUi store(桌面/窄屏布局翻转整树重挂不复位,切股保持)
  const tab = useWorkbenchUi((s) => s.capitalTab)
  const setTab = useWorkbenchUi((s) => s.setCapitalTab)

  // 四 Tab 共用一份池订阅：仅激活 Tab 订阅（enabled=false 不发起请求、不轮询）
  const q = useCapitalHistoryState(c.code, tab, 60, true)

  const columns = useMemo(() => {
    switch (tab) {
      case 'moneyflow':
        return [
          { key: 'date', title: '日期', width: 100, render: (_v: unknown, r: CapitalHistoryRow) => r.date },
          { key: 'main_net', title: '主力净流入', align: 'right' as const, render: (v: unknown) => <MoneyCell v={v} /> },
          { key: 'super_net', title: '超大单净流入', align: 'right' as const, render: (v: unknown) => <MoneyCell v={v} /> },
          { key: 'large_net', title: '大单净流入', align: 'right' as const, render: (v: unknown) => <MoneyCell v={v} /> },
          { key: 'medium_net', title: '中单净流入', align: 'right' as const, render: (v: unknown) => <MoneyCell v={v} /> },
          { key: 'small_net', title: '小单净流入', align: 'right' as const, render: (v: unknown) => <MoneyCell v={v} /> },
        ]
      case 'lhb':
        return [
          { key: 'date', title: '上榜日', width: 100, render: (_v: unknown, r: CapitalHistoryRow) => r.date },
          { key: 'reason', title: '上榜原因', render: (v: unknown) => String(v ?? '龙虎榜') },
          { key: 'buy_amount', title: '买入额', align: 'right' as const, render: (v: unknown) => fmtAmount(toFinite(v) ?? undefined) },
          { key: 'sell_amount', title: '卖出额', align: 'right' as const, render: (v: unknown) => fmtAmount(toFinite(v) ?? undefined) },
          { key: 'net_amount', title: '净买额', align: 'right' as const, render: (v: unknown) => <MoneyCell v={v} /> },
          { key: 'turnover_rate', title: '换手率', align: 'right' as const, render: (v: unknown) => { const n = toFinite(v); return n == null ? '—' : n.toFixed(2) + '%' } },
        ]
      case 'margin':
        return [
          { key: 'date', title: '日期', width: 100, render: (_v: unknown, r: CapitalHistoryRow) => r.date },
          { key: 'margin_balance', title: '融资余额', align: 'right' as const, render: (v: unknown) => fmtAmount(toFinite(v) ?? undefined) },
          { key: 'short_balance', title: '融券余额', align: 'right' as const, render: (v: unknown) => fmtAmount(toFinite(v) ?? undefined) },
          { key: 'net_buy', title: '融资净买入', align: 'right' as const, render: (v: unknown) => <MoneyCell v={v} /> },
        ]
      case 'northbound':
        return [
          { key: 'date', title: '持股日期', width: 100, render: (_v: unknown, r: CapitalHistoryRow) => r.date },
          { key: 'hold_shares', title: '持股数量(股)', align: 'right' as const, render: (v: unknown) => fmtNum(toFinite(v) ?? undefined, 0) },
          { key: 'hold_ratio', title: '持股占比(%)', align: 'right' as const, render: (v: unknown) => { const n = toFinite(v); return n == null ? '—' : n.toFixed(2) + '%' } },
        ]
    }
  }, [tab])

  // 迷你图序列（倒序 → 升序，从左到右为时间推进）：资金流=主力净流入 / 北向=持股占比
  const spark = useMemo(() => {
    const rows = q.value?.data ?? []
    if (tab === 'moneyflow') return rows.slice().reverse().map((r) => toFinite(r.main_net))
    if (tab === 'northbound') return rows.slice().reverse().map((r) => toFinite(r.hold_ratio))
    return []
  }, [q.value, tab])

  const sparkVisible = tab === 'moneyflow' || tab === 'northbound'
  const sparkLabel =
    tab === 'moneyflow' ? `主力净流入 近 ${spark.length} 个交易日（元）`
      : tab === 'northbound' ? `北向持股占比 近 ${spark.length} 个交易日（%）` : ''

  return (
    <CardShell icon={<IconChartCandle size={18} />} title="资金">
      <Tabs value={tab} onChange={(k) => k && setTab(k as CapitalType)}>
        <Tabs.List style={{ marginBottom: 'var(--sr-gap-card)' }}>
          {TAB_META.map((t) => (
            <Tabs.Tab key={t.key} value={t.key}>{t.label}</Tabs.Tab>
          ))}
        </Tabs.List>

        <Tabs.Panel value={tab}>
          {sparkVisible && spark.length > 0 ? (
            <div style={{ marginBottom: 'var(--sr-gap-card)' }}>
              <Sparkline values={spark} color="var(--sr-accent)" label={sparkLabel} />
            </div>
          ) : null}
          {q.error && !q.value ? (
            <EmptyState
              text={tab === 'northbound'
                ? '北向持股接口暂不可用（港交所 2024-08 起停止披露，无历史数据）'
                : '资金数据加载失败，请检查网络后重试'}
              onRetry={() => invalidateCapital()}
            />
          ) : !q.value && q.loading ? (
            <CardState loading minHeight={80} loadingText="加载中…">{null}</CardState>
          ) : (
            <DataTable
              columns={columns}
              dataSource={q.value?.data ?? []}
              rowKey={(r: CapitalHistoryRow) => `${r.date}-${r.reason ?? ''}`}
              pagination={{ pageSize: 15, hideOnSinglePage: true, showTotal: (t: number) => `共 ${t} 条` }}
              fillWidth
            />
          )}
        </Tabs.Panel>
      </Tabs>
    </CardShell>
  )
}
