import { Badge, Button, Flex, Group, Input, Loader, Modal, NumberInput, SegmentedControl, Select, Stack, Text, TextInput, Tooltip } from '@mantine/core'
import { DateInput } from '@mantine/dates'
import { useForm } from '@mantine/form'
import { openConfirmModal } from '@mantine/modals'
import { notifications } from '@mantine/notifications'
import { IconChartLine, IconInfoCircle, IconPlus, IconRefresh, IconTrash } from '@tabler/icons-react'
import dayjs from 'dayjs'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  cancelPaperOrder,
  createPaperAccount,
  deletePaperAccount,
  getPaperAccounts,
  getPaperOrders,
  getPaperPerformance,
  getPaperPositions,
  getPaperTrades,
  placePaperOrder,
  type PaperAccount,
  type PaperOrder,
  type PaperPerformance,
  type PaperPositionRow,
  type PaperTrade,
} from '../../../api/client'
import { DataTable, CardShell, EmptyState, PageHeader, StatStrip } from '../../../components/ui'
import type { SrColumnsType } from '../../../components/ui/tableTypes'
import { errMsg, fmtNum, fmtPct, pctColor } from '../../../utils/format'
import { formatFullTime, formatSignalTime } from '../../../utils/time'
import { klineAxisLabel } from '../../../utils/klineSeries'
import { chartColors } from '../../../utils/chartTheme'
import { echarts } from '../../../utils/echartsSetup'
import { chartTheme } from '../../../utils/echartsTheme'
import { useThemeStore } from '../../../stores/useAppStore'
import { useEchartsLifecycle } from '../../../hooks/useEchartsLifecycle'
import './paper-shared.css'

/** antd message → @mantine/notifications（T-44：色/时长对齐 research/shared/toast.ts 契约） */
const notifySuccess = (m: string) => notifications.show({ message: m, color: 'teal', autoClose: 2500 })
const notifyError = (m: string) => notifications.show({ message: m, color: 'red', autoClose: 4000 })
const notifyWarning = (m: string) => notifications.show({ message: m, color: 'yellow', autoClose: 3200 })
const notifyInfo = (m: string) => notifications.show({ message: m, color: 'blue', autoClose: 2500 })

/** antd 色名 → Mantine 色名（ORDER_STATUS 等返回 antd 色域，Badge 需 Mantine 色名） */
const ANT_TO_MANTINE: Record<string, string> = {
  default: 'gray', blue: 'blue', green: 'green', red: 'red', orange: 'orange', gold: 'yellow',
  volcano: 'orange', purple: 'grape', cyan: 'cyan', magenta: 'pink', geekblue: 'indigo',
  success: 'teal', processing: 'blue', error: 'red', warning: 'yellow',
}
const badgeColor = (c: string): string => ANT_TO_MANTINE[c] ?? 'gray'

/**
 * T-138:DateInput 的 Date 对象取浏览器本地 Y/M/D 直解(YYYY-MM-DD)——bar_date 语义是
 * 「用户选择的日历日期」,与浏览器时区无关。原实现 dayjs(v).tz('Asia/Shanghai') 把本地
 * 时刻标注为上海时区,非 UTC+8 浏览器(东/西偏)会偏移一天;本地字段直解任意时区一致。
 */
export function formatBarDate(d: Date | null | undefined): string | null {
  if (!d || Number.isNaN(d.getTime())) return null
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** 委托状态 → 文案 + 色（单一事实源，表格/提示共用） */
const ORDER_STATUS: Record<string, { text: string; color: string }> = {
  filled: { text: '已成交', color: 'success' },
  pending: { text: '挂起', color: 'processing' },
  canceled: { text: '已撤单', color: 'default' },
  rejected: { text: '已拒绝', color: 'error' },
}

const ORDER_TYPE_TEXT = { market: '市价', limit: '限价' } as const

/** 三张表格统一分页：模块常量（非渲染期新建对象，引用稳定，防 Table 无谓重渲染） */
const TABLE_PAGINATION = { pageSize: 10, showSizeChanger: false }

/** 客户端排序比较（DataTable 列 sorter 契约）：null 恒最小；数值按差值、字符串按字典序。
 *  ISO 时间/日期串字典序即时间序（与 sortProjects 同一事实），委托/成交表排序统一走此入口。 */
const cmpSort = (a: unknown, b: unknown): number => {
  if (a == null && b == null) return 0
  if (a == null) return -1
  if (b == null) return 1
  if (typeof a === 'number' && typeof b === 'number') return a - b
  return String(a).localeCompare(String(b), 'zh')
}

/** hex → rgba：面积填充色由主色令牌派生（暗色 alpha 略高补偿暗底，保持视觉一致） */
function withAlpha(hex: string, alpha: number): string {
  const n = parseInt(hex.slice(1), 16)
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`
}

/** 净值曲线：echarts 折线（canvas 颜色按 isDark JS 常量切换，与 KlineChart/统计图同模式；
 *  涨跌色走 utils/chartTheme 单一事实源令牌，禁硬编码）。
 *  x 轴标签用 klineAxisLabel 统一格式化（日线 MM-DD、跨年自动补 YYYY-，取代旧手写 slice(5)）。 */
function NetValueChart({ curve }: { curve: PaperPerformance['curve'] }) {
  const isDark = useThemeStore((s) => s.theme) === 'dark'
  const chartRef = useRef<HTMLDivElement>(null)
  const chartInst = useRef<echarts.ECharts | null>(null)
  useEchartsLifecycle(chartRef, chartInst)

  useEffect(() => {
    if (curve.length === 0 || !chartRef.current) return
    if (!chartInst.current) chartInst.current = echarts.getInstanceByDom(chartRef.current) ?? echarts.init(chartRef.current)
    const t = chartTheme(isDark)
    const up = chartColors(isDark).up
    chartInst.current.setOption({
      animation: false,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        valueFormatter: (v: number) => (v == null ? '—' : fmtNum(v, 2)),
      },
      grid: { left: 56, right: 16, top: 20, bottom: 28 },
      xAxis: {
        type: 'category',
        data: curve.map((p) => klineAxisLabel(p.date, 'daily')),
        axisLabel: { fontSize: 10, color: t.text3, hideOverlap: true },
      },
      yAxis: {
        type: 'value',
        scale: true,
        splitLine: { lineStyle: { color: t.grid } },
        axisLabel: { fontSize: 10, color: t.text3 },
      },
      series: [{
        name: '总资产',
        type: 'line',
        data: curve.map((p) => p.equity),
        // 单点（仅一笔成交且无后续 bar 估值）时折线不可见，退化为实心点符号兜底；多点不显示符号
        showSymbol: curve.length <= 1,
        lineStyle: { width: 1.6, color: up },
        itemStyle: { color: up },
        areaStyle: { color: withAlpha(up, isDark ? 0.1 : 0.08) },
      }],
    }, true)
  }, [curve, isDark])

  useEffect(() => () => {
    if (chartInst.current && !chartInst.current.isDisposed()) chartInst.current.dispose()
    chartInst.current = null
  }, [])

  return <div className="sr-paper-chart" ref={chartRef} />
}

interface PortfolioProps {
  account: PaperAccount
  positions: PaperPositionRow[]
  orders: PaperOrder[]
  trades: PaperTrade[]
  perf: PaperPerformance | null
  loading: boolean
  onRefresh: () => void
  onPlaced: () => void
}

/** 下单表单 + 持仓/委托/成交表格：选定账户后的仪表盘主体。
 *  L0 结论条（账户视图）：总资产 / 总收益 X% / 今日 X%（今日取净值曲线末两点差值）。
 *  Mantine 迁移（T-44）：antd Form → @mantine/form useForm；bar 时点 → @mantine/dates DateInput
 *  （value 用 Date 对象，提交时 dayjs(v).format('YYYY-MM-DD') 序列化 naive 串，禁 new Date 解析后端时间）。 */
function PortfolioBody({ account, positions, orders, trades, perf, loading, onRefresh, onPlaced }: PortfolioProps) {
  const form = useForm<{
    code: string
    side: 'buy' | 'sell'
    order_type: 'market' | 'limit'
    price: number | null
    quantity: number | null
    bar_date: Date | null
  }>({
    initialValues: { code: '', side: 'buy', order_type: 'market', price: null, quantity: 100, bar_date: null },
    validate: {
      code: (v) => (v.trim() ? undefined : '请输入股票代码'),
      price: (v, values) => (values.order_type === 'limit' && (v == null || Number(v) <= 0) ? '限价单请输入委托价' : undefined),
      quantity: (v) => (v == null || Number(v) <= 0 ? '请输入数量' : undefined),
    },
  })
  const [submitting, setSubmitting] = useState(false)

  const equity = perf?.metrics?.final_equity ?? account.cash
  const pnl = equity - account.initial_cash
  const pnlPct = account.initial_cash > 0 ? pnl / account.initial_cash : 0
  /** 当日盈亏：净值曲线末两点差值（最后一根 = 当日收盘估值，前一根 = 前一日） */
  const dayPnl = useMemo(() => {
    const c = perf?.curve ?? []
    return c.length >= 2 ? c[c.length - 1].equity - c[c.length - 2].equity : null
  }, [perf])
  const dayPnlPct = useMemo(() => {
    const c = perf?.curve ?? []
    return dayPnl != null && c.length >= 2 && c[c.length - 2].equity > 0
      ? dayPnl / c[c.length - 2].equity
      : null
  }, [perf, dayPnl])

  const onSubmit = async () => {
    const { hasErrors } = form.validate()
    if (hasErrors) return
    const v = form.values
    setSubmitting(true)
    try {
      const o = await placePaperOrder(account.id, {
        code: v.code.trim(),
        side: v.side,
        order_type: v.order_type,
        price: v.order_type === 'limit' ? v.price : null,
        quantity: v.quantity ?? 0,
        // T-138:本地日期字段直解,不经 dayjs.tz(跨时区偏移)
        bar_date: formatBarDate(v.bar_date),
      })
      if (o.status === 'filled') notifySuccess(`已成交：${o.code} ${o.side === 'buy' ? '买入' : '卖出'} ${fmtNum(o.filled_qty, 0)} 股 @ ${fmtNum(o.filled_avg_price, 2)}`)
      else if (o.status === 'rejected') notifyWarning(`委托被拒绝：${o.reject_reason ?? ''}`)
      else notifyInfo(`委托已挂起（限价未触发），可在下方撤单`)
      form.setFieldValue('code', '')
      onPlaced()
    } catch (e) {
      notifyError(errMsg(e))
    } finally {
      setSubmitting(false)
    }
  }

  const cancelOrder = async (oid: number) => {
    try {
      await cancelPaperOrder(account.id, oid)
      notifySuccess('已撤单')
      onRefresh()
    } catch (e) {
      notifyError(errMsg(e))
    }
  }

  const posColumns = useMemo<SrColumnsType<PaperPositionRow>>(() => [
    { title: '股票', dataIndex: 'code', render: (_, r) => <span>{r.name || r.code}<Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}> {r.code}</Text></span> },
    { title: '数量', dataIndex: 'quantity', align: 'right', render: (v: number) => fmtNum(v, 0) },
    { title: '成本', dataIndex: 'avg_cost', align: 'right', render: (v: number) => fmtNum(v, 3) },
    { title: '现价', dataIndex: 'close', align: 'right', render: (v: number | null) => (v == null ? '—' : fmtNum(v, 2)) },
    { title: '市值', dataIndex: 'market_value', align: 'right', render: (v: number | null) => (v == null ? '—' : fmtNum(v, 2)) },
    { title: '浮动盈亏', dataIndex: 'pnl', align: 'right', render: (v: number | null) => <span style={{ color: pctColor(v) }}>{v == null ? '—' : fmtNum(v, 2)}</span> },
    { title: '盈亏比例', dataIndex: 'pnl_pct', align: 'right', render: (v: number | null) => <span style={{ color: pctColor(v) }}>{v == null ? '—' : fmtPct(v)}</span> },
  ], [])

  const orderColumns = useMemo<SrColumnsType<PaperOrder>>(() => [
    // 委托时间 = 绝对时刻含时分：直连统一出口 formatFullTime（与 fmtTime 语义等价，禁手裁）；sorter=客户端排序（sortScope='loaded'）
    { title: '委托时间', dataIndex: 'created_at', width: 120, sorter: (a, b) => cmpSort(a.created_at, b.created_at), render: (v: string | null) => formatFullTime(v, { withYear: false, withSeconds: false }) },
    { title: '股票', dataIndex: 'code', sorter: (a, b) => cmpSort(a.code, b.code), render: (_, r) => <span>{r.name || r.code}</span> },
    { title: '方向', dataIndex: 'side', width: 56, sorter: (a, b) => cmpSort(a.side, b.side), render: (s: string) => <span style={{ color: s === 'buy' ? 'var(--sr-up)' : 'var(--sr-down)' }}>{s === 'buy' ? '买入' : '卖出'}</span> },
    { title: '类型', dataIndex: 'order_type', width: 56, sorter: (a, b) => cmpSort(a.order_type, b.order_type), render: (t: keyof typeof ORDER_TYPE_TEXT) => ORDER_TYPE_TEXT[t] ?? t },
    { title: '价格', dataIndex: 'price', align: 'right', width: 70, sorter: (a, b) => cmpSort(a.price, b.price), render: (v: number | null) => (v == null ? '市价' : fmtNum(v, 2)) },
    { title: '数量', dataIndex: 'quantity', align: 'right', width: 80, sorter: (a, b) => a.quantity - b.quantity, render: (v: number) => fmtNum(v, 0) },
    { title: '成交价', dataIndex: 'filled_avg_price', align: 'right', width: 80, sorter: (a, b) => cmpSort(a.filled_avg_price, b.filled_avg_price), render: (v: number | null) => (v == null ? '—' : fmtNum(v, 2)) },
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (s: keyof typeof ORDER_STATUS, r) => ORDER_STATUS[s] ? (
        <Group gap={4}>
          <Badge color={badgeColor(ORDER_STATUS[s].color)} radius="sm">{ORDER_STATUS[s].text}</Badge>
          {s === 'pending' && <Button variant="subtle" size="xs" onClick={() => cancelOrder(r.id)}>撤单</Button>}
        </Group>
      ) : s,
    },
    { title: '说明', dataIndex: 'reject_reason', render: (v: string | null) => <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>{v ?? '—'}</Text> },
  ], [account.id, cancelOrder])

  const tradeColumns = useMemo<SrColumnsType<PaperTrade>>(() => [
    // 成交日期 bar_date = 日期语义（日精度）：formatSignalTime('daily') 输出 MM-DD、跨年补 YYYY-；sorter=客户端排序（sortScope='loaded'）
    { title: '成交日期', dataIndex: 'bar_date', width: 90, sorter: (a, b) => cmpSort(a.bar_date, b.bar_date), render: (v: string | null) => formatSignalTime(v, 'daily') },
    { title: '股票', dataIndex: 'code', sorter: (a, b) => cmpSort(a.code, b.code), render: (_, r) => <span>{r.name || r.code}</span> },
    { title: '方向', dataIndex: 'side', width: 56, sorter: (a, b) => cmpSort(a.side, b.side), render: (s: string) => <span style={{ color: s === 'buy' ? 'var(--sr-up)' : 'var(--sr-down)' }}>{s === 'buy' ? '买入' : '卖出'}</span> },
    { title: '价格', dataIndex: 'price', align: 'right', width: 80, sorter: (a, b) => a.price - b.price, render: (v: number) => fmtNum(v, 2) },
    { title: '数量', dataIndex: 'quantity', align: 'right', width: 80, sorter: (a, b) => a.quantity - b.quantity, render: (v: number) => fmtNum(v, 0) },
    { title: '金额', dataIndex: 'amount', align: 'right', width: 100, sorter: (a, b) => a.amount - b.amount, render: (v: number) => fmtNum(v, 2) },
    { title: '费用', dataIndex: 'fee', align: 'right', width: 80, sorter: (a, b) => a.fee - b.fee, render: (v: number) => fmtNum(v, 2) },
  ], [])

  return (
    <div className="sr-paper-page">
      {/* L0 结论条（账户视图，05 §5.4）：总资产 · 总收益 X% · 今日 X%；loading 时数字位骨架不空白。
          设计书曾提「总资产 display 28px」——统一走 StatStrip 22px 基板（骨架/一致性优先），此裁决于 2026-08-14 记档。
          数字口径沿用 T-46：总收益/今日以百分比为主值，金额并入 sub（原 MetricStat 口径不丢）。 */}
      <StatStrip
        loading={loading}
        items={[
          { key: 'equity', label: '总资产', value: fmtNum(equity, 2), sub: `初始 ${fmtNum(account.initial_cash, 0)} · 可用 ${fmtNum(account.cash, 2)}` },
          { key: 'total-pnl', label: '总收益', value: fmtPct(pnlPct), tone: pnl >= 0 ? 'up' : 'down', sub: fmtNum(pnl, 2) },
          {
            key: 'day-pnl', label: '今日',
            value: dayPnlPct == null ? '—' : fmtPct(dayPnlPct),
            tone: dayPnl == null ? 'plain' : dayPnl >= 0 ? 'up' : 'down',
            sub: dayPnl == null ? '暂无净值数据' : fmtNum(dayPnl, 2),
          },
        ]}
      />

      <Flex gap="var(--sr-gap-card)" align="stretch" wrap="wrap" style={{ minWidth: 0 }}>
        <div className="sr-paper-section" style={{ flex: '1 1 420px', minWidth: 0 }}>
          <div className="sr-paper-section-title">
            <Text fw={700} style={{ fontSize: 'var(--sr-font-title)' }}>净值曲线</Text>
            <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>{perf && perf.curve.length > 0 ? `${perf.curve.length} 个交易日` : '暂无成交，成交后按日线收盘估值'}</Text>
          </div>
          {perf && perf.curve.length > 0 ? (
            <NetValueChart curve={perf.curve} />
          ) : (
            <div style={{ minHeight: 180 }}><EmptyState description="暂无净值数据（先完成一笔成交）" padding="40px 0" /></div>
          )}
          {/* 指标条：无成交时后端 metrics=null（空态），整条隐藏，与上方「暂无净值数据」空态一致（T-97） */}
          {perf?.metrics && (
            <Flex gap="var(--sr-gap-ctl)" style={{ minWidth: 0 }}>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
                总收益 <span style={{ color: pctColor(perf.metrics.total_return) }}>{perf.metrics.total_return == null ? '—' : fmtPct(perf.metrics.total_return)}</span>
              </Text>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
                年化 <span style={{ color: pctColor(perf.metrics.annualized_return) }}>{perf.metrics.annualized_return == null ? '—' : fmtPct(perf.metrics.annualized_return)}</span>
              </Text>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
                最大回撤 <span style={{ color: 'var(--sr-down)' }}>{perf.metrics.max_drawdown == null ? '—' : fmtPct(perf.metrics.max_drawdown)}</span>
              </Text>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
                夏普 <span style={{ color: 'var(--sr-text-1)' }}>{perf.metrics.sharpe == null ? '—' : fmtNum(perf.metrics.sharpe, 2)}</span>
              </Text>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
                胜率 <span style={{ color: 'var(--sr-text-1)' }}>{perf.metrics.win_rate == null ? '—' : fmtPct(perf.metrics.win_rate, 1)}</span>
                <span style={{ color: 'var(--sr-text-3)' }}>（{perf.metrics.winning_trades}/{perf.metrics.trade_count} 平仓）</span>
              </Text>
            </Flex>
          )}
        </div>

        <div className="sr-paper-section" style={{ flex: '1 1 300px', minWidth: 0, maxWidth: 460 }}>
          <div className="sr-paper-section-title">
            <Text fw={700} style={{ fontSize: 'var(--sr-font-title)' }}>下单</Text>
            <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>按 bar 收盘价撮合 · 涨跌停/停牌约束后续版本</Text>
          </div>
          <Stack gap="var(--sr-gap-row)">
            <div>
              <Input.Label required>代码</Input.Label>
              <TextInput
                placeholder="如 600519.SH"
                value={form.values.code}
                onChange={(e) => form.setFieldValue('code', e.currentTarget.value)}
                error={form.errors.code}
              />
            </div>
            <div>
              <Input.Label>方向</Input.Label>
              <SegmentedControl
                fullWidth
                value={form.values.side}
                onChange={(v) => form.setFieldValue('side', v as 'buy' | 'sell')}
                data={[{ value: 'buy', label: '买入' }, { value: 'sell', label: '卖出' }]}
              />
            </div>
            <div>
              <Input.Label>委托类型</Input.Label>
              <SegmentedControl
                fullWidth
                value={form.values.order_type}
                onChange={(v) => form.setFieldValue('order_type', v as 'market' | 'limit')}
                data={[{ value: 'market', label: '市价' }, { value: 'limit', label: '限价' }]}
              />
            </div>
            {form.values.order_type === 'limit' && (
              <div>
                <Input.Label required>委托价</Input.Label>
                <NumberInput
                  min={0.01}
                  step={0.01}
                  placeholder="≥ 0.01"
                  value={form.values.price ?? ''}
                  onChange={(v) => {
                    const num: number | null = v === '' ? null : Number(v)
                    form.setFieldValue('price', num)
                  }}
                  error={form.errors.price}
                />
              </div>
            )}
            <div>
              <Input.Label required>数量（股）</Input.Label>
              <NumberInput
                min={100}
                step={100}
                placeholder="100 的整数倍"
                value={form.values.quantity ?? ''}
                onChange={(v) => {
                  const num: number | null = v === '' ? null : Number(v)
                  form.setFieldValue('quantity', num)
                }}
                error={form.errors.quantity}
              />
            </div>
            <div>
              <Group gap={4}>
                <Input.Label>bar 时点（可选）</Input.Label>
                <Tooltip label="缺省用最新 bar 收盘价撮合">
                  <IconInfoCircle size={14} style={{ color: 'var(--sr-text-3)', cursor: 'help' }} />
                </Tooltip>
              </Group>
              <DateInput
                clearable
                placeholder="YYYY-MM-DD"
                valueFormat="YYYY-MM-DD"
                value={form.values.bar_date}
                onChange={(v) => form.setFieldValue('bar_date', v ? dayjs(v).toDate() : null)}
              />
            </div>
            <Button leftSection={<IconChartLine size={14} />} loading={submitting} onClick={() => void onSubmit()}>提交委托</Button>
          </Stack>
        </div>
      </Flex>

      <CardShell title="持仓" extra={loading ? <Loader size="xs" /> : undefined}>
        <DataTable rowKey="code" columns={posColumns} dataSource={positions} pagination={TABLE_PAGINATION} fillWidth />
      </CardShell>

      <CardShell title="委托记录" extra={loading ? <Loader size="xs" /> : undefined}>
        <DataTable rowKey="id" columns={orderColumns} dataSource={orders} pagination={TABLE_PAGINATION} fillWidth sortScope="loaded" sortScopeUnit="条" />
      </CardShell>

      <CardShell title="成交记录" extra={loading ? <Loader size="xs" /> : undefined}>
        <DataTable rowKey="id" columns={tradeColumns} dataSource={trades} pagination={TABLE_PAGINATION} fillWidth sortScope="loaded" sortScopeUnit="条" />
      </CardShell>
    </div>
  )
}

/**
 * 账户视图（T-46 模拟盘合并：/paper 单页双视图之二）：
 * 原 PaperPortfolio 内容组件化迁入 paper/，由 PaperTrading 在账户视图渲染。
 * 自持账户/持仓/委托/成交/绩效状态（与项目视图互斥挂载，无需提升状态）；
 * 含账户选择/新建/删除与下单表单。L0 结论条在 PortfolioBody 顶部。
 */
export default function PortfolioView() {
  const [accounts, setAccounts] = useState<PaperAccount[]>([])
  const [accountId, setAccountId] = useState<number | null>(null)
  const [positions, setPositions] = useState<PaperPositionRow[]>([])
  const [orders, setOrders] = useState<PaperOrder[]>([])
  const [trades, setTrades] = useState<PaperTrade[]>([])
  const [perf, setPerf] = useState<PaperPerformance | null>(null)
  const [loading, setLoading] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const createForm = useForm<{ name: string; initial_cash: number | null }>({
    initialValues: { name: '', initial_cash: 1000000 },
    validate: {
      initial_cash: (v) => (v == null || Number(v) < 1000 ? '请输入初始资金' : undefined),
    },
  })
  const [creating, setCreating] = useState(false)
  /** 空态一键创建（无账户引导，T-74）：默认名称「模拟账户」+ 默认初始资金 1,000,000，跳过自定义 Modal */
  const [quickCreating, setQuickCreating] = useState(false)
  const quickCreateAccount = async () => {
    if (quickCreating) return
    setQuickCreating(true)
    try {
      const acc = await createPaperAccount({ name: '模拟账户', initial_cash: 1000000 })
      notifySuccess(`已创建账户：${acc.name}`)
      await loadAccounts(acc.id)
    } catch (e) {
      notifyError(errMsg(e))
    } finally {
      setQuickCreating(false)
    }
  }

  const account = accounts.find((a) => a.id === accountId) ?? null
  const [accountsErr, setAccountsErr] = useState<string | null>(null)

  const loadAccounts = useCallback(async (preselect?: number) => {
    try {
      const list = await getPaperAccounts()
      setAccounts(list)
      setAccountsErr(null)
      setAccountId((cur) => preselect ?? (list.some((a) => a.id === cur) ? cur : (list[0]?.id ?? null)))
    } catch (e) {
      // T-130:首拉失败显式错误态+重试,不再伪装成「暂无账户」引导创建
      setAccountsErr('账户列表加载失败: ' + errMsg(e))
    }
  }, [])

  // T-130 账户响应串写守卫：loadSeq 递增,慢响应(旧账户)不得覆盖后发请求(新账户)结果
  const loadSeq = useRef(0)
  const loadPortfolio = useCallback(async (id: number) => {
    const seq = ++loadSeq.current
    setLoading(true)
    try {
      const [p, o, t, pe] = await Promise.all([
        getPaperPositions(id),
        getPaperOrders(id),
        getPaperTrades(id, 200),
        getPaperPerformance(id),
      ])
      if (seq !== loadSeq.current) return
      setPositions(p)
      setOrders(o)
      setTrades(t)
      setPerf(pe)
    } catch (e) {
      if (seq === loadSeq.current) notifyError(errMsg(e))
    } finally {
      if (seq === loadSeq.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadAccounts()
  }, [loadAccounts])

  useEffect(() => {
    if (accountId == null) return
    void loadPortfolio(accountId)
  }, [accountId, loadPortfolio])

  // T-130:成交/撤单/全局刷新后账户对象(可用现金)不刷新——下单只重拉持仓等,账户资金仍在旧值;
  // 顶栏 sr-refresh 广播也需联动(此前账户页纯手写请求,Query 失效机制覆盖不到)
  useEffect(() => {
    const onRefresh = () => {
      if (accountId != null) {
        void loadPortfolio(accountId)
        void loadAccounts(accountId)
      }
    }
    window.addEventListener('sr-refresh', onRefresh)
    return () => window.removeEventListener('sr-refresh', onRefresh)
  }, [accountId, loadPortfolio, loadAccounts])

  const onCreateAccount = async () => {
    const { hasErrors } = createForm.validate()
    if (hasErrors) return
    const v = createForm.values
    setCreating(true)
    try {
      const acc = await createPaperAccount({ name: v.name, initial_cash: v.initial_cash ?? 1000000 })
      setCreateOpen(false)
      createForm.reset()
      notifySuccess(`已创建账户：${acc.name}`)
      await loadAccounts(acc.id)
    } catch (e) {
      notifyError(errMsg(e))
    } finally {
      setCreating(false)
    }
  }

  const onDeleteAccount = async () => {
    if (!account) return
    try {
      await deletePaperAccount(account.id)
      notifySuccess(`已删除账户：${account.name}`)
      await loadAccounts()
    } catch (e) {
      notifyError(errMsg(e))
    }
  }

  return (
    <div className="sr-paper-page">
      <PageHeader extra={
        <Flex gap="var(--sr-gap-ctl)" align="center" wrap="wrap" style={{ minWidth: 0 }}>
          <Select<number>
            style={{ minWidth: 220 }}
            placeholder="选择账户"
            value={accountId}
            data={accounts.map((a) => ({ value: a.id, label: `${a.name}（${fmtNum(a.initial_cash, 0)}）` }))}
            onChange={(id) => { if (id != null) setAccountId(id) }}
          />
          <Button leftSection={<IconPlus size={14} />} onClick={() => setCreateOpen(true)}>新建账户</Button>
          <Button leftSection={<IconRefresh size={14} />} onClick={() => { if (accountId != null) { void loadPortfolio(accountId); void loadAccounts(accountId) } }}>刷新</Button>
          {account && (
            <Button
              color="red"
              variant="outline"
              leftSection={<IconTrash size={14} />}
              onClick={() => openConfirmModal({
                title: '删除账户',
                children: `将删除「${account.name}」及其全部持仓/委托/成交记录`,
                labels: { confirm: '删除', cancel: '取消' },
                confirmProps: { color: 'red' },
                onConfirm: () => void onDeleteAccount(),
              })}
            >
              删除账户
            </Button>
          )}
        </Flex>
      } />
      {accountsErr && !account ? (
        <div className="sr-paper-state">
          <EmptyState text={accountsErr} onRetry={() => void loadAccounts()} />
        </div>
      ) : !account ? (
        <div className="sr-paper-state">
          <EmptyState
            description={
              <span>
                暂无模拟盘账户。
                <br />点击「一键创建模拟账户」立即生成默认账户（初始资金 100 万），或「自定义创建」设置名称与资金。
              </span>
            }
          />
          <Button
            leftSection={<IconPlus size={14} />}
            loading={quickCreating}
            onClick={() => void quickCreateAccount()}
          >
            一键创建模拟账户
          </Button>
          <Button variant="default" leftSection={<IconPlus size={14} />} onClick={() => setCreateOpen(true)}>
            自定义创建
          </Button>
        </div>
      ) : (
        <PortfolioBody
          account={account}
          positions={positions}
          orders={orders}
          trades={trades}
          perf={perf}
          loading={loading}
          onRefresh={() => { if (accountId != null) { void loadPortfolio(accountId); void loadAccounts(accountId) } }}
          onPlaced={() => { if (accountId != null) { void loadPortfolio(accountId); void loadAccounts(accountId) } }}
        />
      )}

      <Modal
        opened={createOpen}
        onClose={() => setCreateOpen(false)}
        title="新建模拟盘账户"
        centered
      >
        <Stack gap="var(--sr-gap-row)">
          <div>
            <Input.Label>账户名称</Input.Label>
            <TextInput
              placeholder="如 主力仓 / 实验仓（缺省「模拟账户」）"
              maxLength={64}
              value={createForm.values.name}
              onChange={(e) => createForm.setFieldValue('name', e.currentTarget.value)}
            />
          </div>
          <div>
            <Input.Label required>初始资金（元）</Input.Label>
            <NumberInput
              min={1000}
              max={1e12}
              step={10000}
              value={createForm.values.initial_cash ?? ''}
              onChange={(v) => {
                const num: number | null = v === '' ? null : Number(v)
                createForm.setFieldValue('initial_cash', num)
              }}
              error={createForm.errors.initial_cash}
            />
          </div>
          <Group justify="flex-end" gap="var(--sr-gap-ctl)">
            <Button variant="default" onClick={() => setCreateOpen(false)}>取消</Button>
            <Button loading={creating} onClick={() => void onCreateAccount()}>创建</Button>
          </Group>
        </Stack>
      </Modal>
    </div>
  )
}
