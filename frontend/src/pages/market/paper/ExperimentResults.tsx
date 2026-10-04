import { Badge, Loader, Text } from '@mantine/core'
import type { SrColumn } from '../../../components/ui/tableTypes'
import { useMemo, type ReactNode } from 'react'
import type { PaperExperimentResult, PaperMultiExperimentResult, PaperSignalResult, PaperTriggerDetail } from '../../../api/client'
import { DataTable, EmptyState, MetricStat } from '../../../components/ui'
import { fmtNum, fmtPct, fmtWinRate, pctColor } from '../../../utils/format'
import { periodLabel } from '../../../utils/periods'
import './paper-shared.css'

/** antd 色名 → Mantine 色名（labelOf/signalLabelMap 返回 antd 色域，Badge 需 Mantine 色名） */
const ANT_TO_MANTINE: Record<string, string> = {
  default: 'gray', blue: 'blue', green: 'green', red: 'red', orange: 'orange', gold: 'yellow',
  volcano: 'orange', purple: 'grape', cyan: 'cyan', magenta: 'pink', geekblue: 'indigo',
  success: 'teal', processing: 'blue', error: 'red', warning: 'yellow',
}
const badgeColor = (c: string): string => ANT_TO_MANTINE[c] ?? 'gray'

interface ExperimentResultsProps {
  result: PaperExperimentResult | PaperMultiExperimentResult | null
  days: number
  /** 运行发起中或任务运行中且无旧结果 → 展示加载态 */
  busy: boolean
  error: string
  labelOf: (code: string) => { text: string; color: string }
  onRetry: () => void
}

/** 单股信号统计卡 + 触发明细表（多股项目每股一个块，单股项目整体一块）。 */
function StockExperimentBlock({
  title, results, period, days, labelOf,
}: {
  title: string
  results: PaperSignalResult[]
  period: string
  days: number
  labelOf: (code: string) => { text: string; color: string }
}) {
  const detailRows = useMemo(() => {
    const rows: (PaperTriggerDetail & { key: string; signal: string; signalName: string })[] = []
    for (const r of results) {
      r.details?.forEach((d, i) => {
        // key 用 signal+date+内部序号：同一信号同一触发日可能多条明细，仅 signal:date 组合会碰撞
        rows.push({ key: `${r.signal}:${d.date}:${i}`, signal: r.signal, signalName: r.name ?? labelOf(r.signal).text, ...d })
      })
    }
    return rows.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0))
  }, [results, labelOf])

  const detailColumns = useMemo<SrColumn<(typeof detailRows)[number]>[]>(() => [
    {
      title: '信号', dataIndex: 'signal', minWidth: 150,
      render: (_, r) => <Badge color={badgeColor(labelOf(r.signal).color)} radius="sm" style={{ fontWeight: 500 }}>{r.signalName}</Badge>,
    },
    { title: '日期', dataIndex: 'date', minWidth: 130, render: (v: string) => <span style={{ fontVariantNumeric: 'tabular-nums' }}>{v}</span> },
    {
      title: '收盘', dataIndex: 'close', align: 'right', className: 'sr-num-col', minWidth: 90,
      render: (v: number | null) => (v == null || Number.isNaN(v) ? '—' : <span style={{ fontVariantNumeric: 'tabular-nums' }}>{fmtNum(v, 2)}</span>),
    },
    {
      title: '5日收益', dataIndex: 'r5', align: 'right', className: 'sr-num-col', minWidth: 90,
      render: (v: number | null) => <span style={{ color: pctColor(v ?? 0), fontVariantNumeric: 'tabular-nums' }}>{fmtPct(v, 2)}</span>,
    },
    {
      title: '10日收益', dataIndex: 'r10', align: 'right', className: 'sr-num-col', minWidth: 90,
      render: (v: number | null) => <span style={{ color: pctColor(v ?? 0), fontVariantNumeric: 'tabular-nums' }}>{fmtPct(v, 2)}</span>,
    },
  ], [labelOf])

  return (
    <div className="sr-paper-section">
      <div className="sr-paper-section-title">
        <Text fw={700} style={{ fontSize: 'var(--sr-font-title)' }}>{title}</Text>
        <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
          {periodLabel(period)} · 回溯 {days} 天 · 共 {results.length} 个信号
        </Text>
      </div>
      <div className="sr-paper-stats">
        {results.map((r: PaperSignalResult) => (
          <div key={r.signal} className="sr-paper-stat">
            <div className="sr-paper-stat-head">
              <Badge color={badgeColor(labelOf(r.signal).color)} radius="sm" style={{ fontWeight: 500 }}>
                {r.name ?? labelOf(r.signal).text}
              </Badge>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>{r.signal}</Text>
            </div>
            <div className="sr-paper-stat-rows">
              <MetricStat label="触发次数" value={fmtNum(r.triggers, 0)} />
              <MetricStat label="胜率" value={fmtWinRate(r.win_rate)} />
              <MetricStat label="平均5日收益" value={fmtPct(r.avg_r5, 2)} color={pctColor(r.avg_r5 ?? 0)} />
              <MetricStat label="平均10日收益" value={fmtPct(r.avg_r10, 2)} color={pctColor(r.avg_r10 ?? 0)} />
              <MetricStat label="最近触发" value={r.last_trigger ?? '—'} />
            </div>
          </div>
        ))}
      </div>
      <div className="sr-paper-section-title">
        <Text fw={700} style={{ fontSize: 'var(--sr-font-title)' }}>触发明细</Text>
        <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>共 {detailRows.length} 条触发记录</Text>
      </div>
      {detailRows.length === 0 ? (
        <EmptyState description="所选信号在回溯区间内无触发" padding="32px 0" />
      ) : (
        <div className="sr-paper-detail-table">
          {/* 触发明细：DataTable 基板（fillWidth 顶满容器，窄容器由基板 HScroll 兜底横滚）；
              sticky={false}：表在结果卡片内无垂直滚动容器，吸顶无意义 */}
          <DataTable
            rowKey="key"
            columns={detailColumns}
            dataSource={detailRows}
            pagination={{ pageSize: 10, hideOnSinglePage: true, size: 'small' }}
            sticky={false}
            fillWidth
          />
        </div>
      )}
    </div>
  )
}

/** 实验结果区：单股 = 信号统计网格 + 触发明细表；多股 = 跨股 summary + 每股明细块（自 PaperTrading 原 Tab1 渲染迁移）。 */
export default function ExperimentResults({ result, days, busy, error, labelOf, onRetry }: ExperimentResultsProps) {
  const isMulti = result != null && 'multi' in result && result.multi
  const multi = isMulti ? (result as PaperMultiExperimentResult) : null
  const single = !isMulti && result != null ? (result as PaperExperimentResult) : null

  if (error) {
    return <EmptyState text={error} onRetry={onRetry} padding="40px 0" />
  }
  if (busy) {
    return (
      <div className="sr-paper-state">
        <Loader size="sm" />
        <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>正在运行实验…</Text>
      </div>
    )
  }
  if (!result) {
    return (
      <EmptyState
        description={
          <span>
            确认参数后点击「运行实验」，查看每个信号的触发统计与明细。
            <br />
            修改参数并保存后旧结果会失效，需重新运行。
          </span>
        }
        padding="40px 0"
      />
    )
  }

  const renderSummary = (): ReactNode => {
    if (!multi) return null
    const s = multi.summary
    return (
      <div className="sr-paper-section">
        <div className="sr-paper-section-title">
          <Text fw={700} style={{ fontSize: 'var(--sr-font-title)' }}>跨股汇总</Text>
          <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
            {multi.stocks.length} 只股票 · {periodLabel(multi.period)} · 回溯 {multi.days ?? days} 天
          </Text>
        </div>
        <div className="sr-paper-stats">
          <div className="sr-paper-stat">
            <div className="sr-paper-stat-head">
              <Badge color="blue" radius="sm" style={{ fontWeight: 500 }}>股票数量</Badge>
            </div>
            <div className="sr-paper-stat-rows">
              <MetricStat label="覆盖股票" value={fmtNum(s.stock_count, 0)} />
            </div>
          </div>
          <div className="sr-paper-stat">
            <div className="sr-paper-stat-head">
              <Badge color="blue" radius="sm" style={{ fontWeight: 500 }}>信号触发</Badge>
            </div>
            <div className="sr-paper-stat-rows">
              <MetricStat label="总触发次数" value={fmtNum(s.total_triggers, 0)} />
              <MetricStat label="平均胜率" value={fmtWinRate(s.avg_win_rate)} />
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="sr-paper-results-inner">
      {renderSummary()}
      {multi ? (
        multi.stocks.map((st) => (
          <StockExperimentBlock
            key={st.code}
            title={`${st.name || st.code}（${st.code}）`}
            results={st.results ?? []}
            period={multi.period}
            days={multi.days ?? days}
            labelOf={labelOf}
          />
        ))
      ) : (
        <StockExperimentBlock
          title={single?.code ? `${single.code} · 信号统计` : '信号统计'}
          results={single?.results ?? []}
          period={single?.period ?? ''}
          days={single?.days ?? days}
          labelOf={labelOf}
        />
      )}
    </div>
  )
}
