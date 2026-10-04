import { Badge, SegmentedControl, Select, Tooltip } from '@mantine/core'
import { IconChevronDown, IconChevronRight, IconTarget } from '@tabler/icons-react'
import { useMemo, useState } from 'react'
import EmptyState from '../../../components/ui/EmptyState'
import SkeletonBlock from '../../../components/ui/SkeletonBlock'
import SignalMomentCell from '../../../components/ui/SignalMomentCell'
import { SIGNAL_CATEGORY_COLOR } from '../../../utils/signals'
import { resolveSignalMoment, toMarketEpochMs } from '../../../utils/time'
import { useWorkbench } from './context'
import type { WorkbenchTimelineEvent } from './context'
import { antdToMantine } from './tagColor'
import './TimelinePanel.css'

/**
 * evidence 非信号字段的中文兜底映射：信号名走 labelMap（catalog 单一事实源），
 * 此处只补 catalog 之外的行情摘要字段；仍未命中 → 「未识别字段」，禁止直接吐英文 key。
 */
const EVIDENCE_KEY_FALLBACK: Record<string, string> = {
  price: '收盘价',
  pct_change: '涨跌幅',
  volume_ratio: '量比',
  open: '开盘价',
  high: '最高价',
  low: '最低价',
  close: '收盘价',
  volume: '成交量',
  amount: '成交额',
}

/** SIGNAL_CATEGORY_COLOR 反查：antd 颜色名 → 信号类别（事件归类 / 类别筛选选项） */
const COLOR_TO_CATEGORY = Object.fromEntries(
  Object.entries(SIGNAL_CATEGORY_COLOR).map(([cat, color]) => [color, cat]),
)

/** 信号 Tag 单行最多展示数，超出折叠为「+N」（tooltip 显示全部） */
const MAX_SIGNAL_TAGS = 2

/** 单行时间线：顶部过滤器（全部/只看确认/信号类别）+ 紧凑单行列表 + 点击展开 evidence 明细 + 事件「定位到K线」 */
function SignalTimelineList({ events, labelMap, period }: {
  events: WorkbenchTimelineEvent[]
  labelMap: Record<string, { text: string; color: string }>
  period: string
}) {
  // T-117 程序化十字线定位：经工作台 context 的 klineRef 直达引擎（滚动至该 bar + 十字线钉住；失败静默）
  const klineRef = useWorkbench().klineRef
  const locateAt = (e: WorkbenchTimelineEvent) => {
    const t = e.triggered_at ?? e.as_of
    if (t) klineRef.current?.locateToTime(t)
  }
  // 统一时间轴：按展示时刻（as_of ?? triggered_at）倒序，与全站排序口径一致
  const sorted = useMemo(
    () => [...events].sort((a, b) => toMarketEpochMs(b.as_of ?? b.triggered_at) - toMarketEpochMs(a.as_of ?? a.triggered_at)),
    [events],
  )
  // 类别选项：来自事件中出现过的类别（按 SIGNAL_CATEGORY_COLOR 定义顺序），色点取类别色
  const categories = useMemo(() => {
    const seen = new Set<string>()
    for (const e of sorted) {
      for (const s of e.signals ?? []) {
        const cat = COLOR_TO_CATEGORY[labelMap[s]?.color]
        if (cat) seen.add(cat)
      }
    }
    return Object.keys(SIGNAL_CATEGORY_COLOR).filter((c) => seen.has(c))
  }, [sorted, labelMap])

  const [statusFilter, setStatusFilter] = useState('all')
  const [catFilter, setCatFilter] = useState('all')
  // 展开集合（组件本地态；刷新后 key 自然失效，不做持久化）
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const toggleExpand = (k: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(k)) next.delete(k)
      else next.add(k)
      return next
    })
  }

  const filtered = useMemo(() => sorted.filter((e) => {
    if (statusFilter !== 'all' && e.status !== statusFilter) return false
    if (catFilter !== 'all' && !(e.signals ?? []).some((s) => COLOR_TO_CATEGORY[labelMap[s]?.color] === catFilter)) return false
    return true
  }), [sorted, statusFilter, catFilter, labelMap])

  const sigText = (s: string) => labelMap[s]?.text ?? s
  const sigColor = (s: string) => labelMap[s]?.color

  const renderRow = (e: WorkbenchTimelineEvent, i: number) => {
    const key = e.triggered_at ?? e.as_of ?? String(i)
    // 展示时刻单一事实源：主行 label/hint 走状态机（as_of 不参与），tooltip 保留双时间
    const m = resolveSignalMoment(period, e.triggered_at, null)
    const tip = resolveSignalMoment(period, e.triggered_at, e.as_of).title
    const sigs = e.signals ?? []
    const shown = sigs.slice(0, MAX_SIGNAL_TAGS)
    const rest = sigs.slice(MAX_SIGNAL_TAGS)
    const ev = e.evidence ?? {}
    // 价格摘要：收 价 / 涨跌幅（有才显示，涨跌着色走令牌）
    const price = typeof ev.price === 'number' ? ev.price.toFixed(2) : (ev.price != null ? String(ev.price) : null)
    const pct = ev.pct_change
    const pctNum = typeof pct === 'number' ? pct : (typeof pct === 'string' && pct !== '' ? Number(pct) : NaN)
    const pctTxt = Number.isNaN(pctNum) ? null : `${pctNum > 0 ? '+' : ''}${pctNum.toFixed(1)}%`
    const pctColor = Number.isNaN(pctNum) ? undefined
      : pctNum > 0 ? 'var(--sr-up)' : pctNum < 0 ? 'var(--sr-down)' : 'var(--sr-text-2)'
    const vr = typeof ev.volume_ratio === 'number' ? ev.volume_ratio.toFixed(2) : (ev.volume_ratio != null ? String(ev.volume_ratio) : null)
    const isOpen = expanded.has(key)
    const labelOf = (k: string) => labelMap[k]?.text ?? EVIDENCE_KEY_FALLBACK[k] ?? '未识别字段'
    const detailKeys = Object.keys(ev).filter((k) => !['price', 'pct_change', 'volume_ratio'].includes(k))

    return (
      <div key={key}>
        <div className="sr-wb-tl-row" onClick={() => toggleExpand(key)}>
          <span className="sr-wb-tl-time" title={tip}>
            {m.label}
            {m.hint && (
              <span className="sr-wb-tl-time-hint" style={{ color: m.intraday ? 'var(--sr-warning)' : undefined }}>{m.hint}</span>
            )}
          </span>
          <span className="sr-wb-tl-sigs">
            {shown.map((sg) => (
              <Badge key={sg} variant="light" color={antdToMantine(sigColor(sg))} radius="var(--sr-radius-tag)" size="sm" className="sr-wb-tl-badge">{sigText(sg)}</Badge>
            ))}
            {rest.length > 0 && (
              <Tooltip label={rest.map(sigText).join('、')}>
                <Badge variant="light" color={antdToMantine('default')} radius="var(--sr-radius-tag)" size="sm" className="sr-wb-tl-badge">+{rest.length}</Badge>
              </Tooltip>
            )}
          </span>
          {(price != null || pctTxt != null || vr != null) && (
            <span className="sr-wb-tl-price">
              {price != null && <span>收 {price}</span>}
              {pctTxt != null && <span style={{ color: pctColor }}>{pctTxt}</span>}
              {vr != null && <span>量比 {vr}</span>}
            </span>
          )}
          {/* T-117 程序化十字线定位：点击在 K 线上滚动至该事件 bar 并钉住十字线（不展开明细） */}
          <Tooltip label="在K线上定位">
            <span
              role="button" tabIndex={0}
              aria-label="在K线上定位该事件"
              className="sr-wb-tl-locate"
              onClick={(ev) => { ev.stopPropagation(); locateAt(e) }}
              onKeyDown={(ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); ev.stopPropagation(); locateAt(e) } }}
            >
              <IconTarget size={14} />
            </span>
          </Tooltip>
          <span className="sr-wb-tl-arrow">{isOpen ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}</span>
        </div>
        {isOpen && (
          <div className="sr-wb-tl-detail">
            {/* 展开区顶部保留完整双时（信号/扫描），信息不丢失 */}
            <div className="sr-wb-tl-moment">
              <SignalMomentCell period={period} triggeredAt={e.triggered_at} asOf={e.as_of} discoveredAt={e.scan_discovered_at} />
            </div>
            {detailKeys.map((k) => (
              <div key={k} className="sr-wb-tl-ev">
                <span className="sr-wb-tl-ev-key">{labelOf(k)}</span>
                <span className="sr-wb-tl-ev-val">{String(ev[k] ?? '—')}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    )
  }

  return (
    <>
      <div className="sr-wb-tl-filters">
        <SegmentedControl
          size="xs"
          value={statusFilter}
          onChange={setStatusFilter}
          data={[
            { label: '全部', value: 'all' },
            { label: '只看确认', value: '确认' },
          ]}
          style={{ height: 'var(--sr-ctl-h)' }}
          styles={{ label: { height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' } }}
        />
        <Select
          size="xs"
          value={catFilter}
          onChange={(v) => setCatFilter(v ?? 'all')}
          data={[
            { value: 'all', label: '全部类别' },
            ...categories.map((c) => ({ value: c, label: c })),
          ]}
          renderOption={({ option }) => (
            option.value === 'all'
              ? <span>全部类别</span>
              : <Badge variant="light" color={antdToMantine(SIGNAL_CATEGORY_COLOR[option.value])} radius="var(--sr-radius-tag)" size="xs">{option.value}</Badge>
          )}
        />
      </div>
      {filtered.length ? (
        <div className="sr-wb-tl-list">{filtered.map(renderRow)}</div>
      ) : (
        <EmptyState description="无匹配当前筛选的信号事件" />
      )}
    </>
  )
}

/** 信号时间线视图（无 dock 依赖，工作台左侧信号区 / 窄屏堆叠信号区共用） */
export function SignalTimelineView() {
  const c = useWorkbench()
  const { timelineEvents, errSignal, labelMap, onRetry, loading, period } = c

  return (
    <div className="sr-wb-tl" style={{ height: '100%', padding: '8px 10px' }}>
      {errSignal && !timelineEvents.length ? (
        <EmptyState text={errSignal} onRetry={onRetry} />
      ) : timelineEvents.length ? (
        <SignalTimelineList events={timelineEvents} labelMap={labelMap} period={period} />
      ) : loading ? (
        <SkeletonBlock variant="text" rows={3} />
      ) : (
        // 后端 days=30 是「最大事件条数」行数语义（非自然日天数），空态文案不误导为天数
        <EmptyState description="最近 30 条信号事件" />
      )}
    </div>
  )
}

export default function TimelinePanel() {
  return <SignalTimelineView />
}
