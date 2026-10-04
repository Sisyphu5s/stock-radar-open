import { ActionIcon, Button, Group, Tooltip } from '@mantine/core'
import { IconStar, IconStarFilled } from '@tabler/icons-react'
import { memo, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import type { SignalEvent } from '../../../api/client'
import type { SrColumn } from '../../../components/ui/tableTypes'
import SignalMomentCell from '../../../components/ui/SignalMomentCell'
import { useWatchlistStarFrom } from '../../../hooks/useWatchlistStar'
import { fmtPct } from '../../../utils/format'
import { toMarketEpochMs } from '../../../utils/time'
import { evPct } from './context'
import type { SignalSortState, StockGroup } from './context'

/** 证据明细行（tooltip）：排除价格/涨幅/量比主字段，其余键映射为「名称: 值」 */
const EVIDENCE_SKIP = new Set(['price', 'pct_change', 'volume_ratio'])
const evidenceLines = (ev: SignalEvent['evidence'] | undefined, labelOf: (c: string) => { text: string; color: string }) =>
  Object.entries(ev ?? {})
    .filter(([k]) => !EVIDENCE_SKIP.has(k))
    .map(([k, v]) => `${labelOf(k).text}: ${v}`)

// ===== 受控排序：列 key → 比较器（函数体与 groupColumns 原 sorter 完全一致，不改变排序规则） =====
// 排序作用于已加载集合（filteredGroups，排除浏览筛选后），不再按页切片
export const SORT_COLUMN_KEYS = {
  stock: 'stock',
  signals: 'signals',
  time: 'time',
  quote: 'quote',
} as const
const sorters: Record<string, (a: StockGroup, b: StockGroup) => number> = {
  [SORT_COLUMN_KEYS.stock]: (a, b) => (a.name || a.code).localeCompare(b.name || b.code, 'zh-Hans-CN'),
  [SORT_COLUMN_KEYS.signals]: (a, b) => a.signals.length - b.signals.length,
  [SORT_COLUMN_KEYS.time]: (a, b) =>
    toMarketEpochMs(a.as_of ?? a.triggered_at) - toMarketEpochMs(b.as_of ?? b.triggered_at),
  [SORT_COLUMN_KEYS.quote]: (a, b) => evPct(a.best.evidence) - evPct(b.best.evidence),
}
/** 供 SignalCenter 排序执行（sortedGroups）与列定义共用，避免双份比较器 */
export { sorters }

/**
 * 关注星标（T-41 收编）：统一走 useWatchlistStar 单一实现（乐观更新 + 失败回滚 + in-flight 锁）。
 * P2-68：watched 由 SignalCenter 页级单一订阅（useWatchlistCodes）派生传入（O(1) 成员查找），
 * 替代每行独立订阅 useWatchlist().some()；memo 浅比较 + 稳定 toggle → SSE tick 仅价格字段
 * 变化不改成员时星标不重渲染（按需渲染，消除 O(行数×N) 与全体重渲染）。
 * 触控目标 ≥32px（P1-a11y-5，antd small 按钮 24px 不达标）。
 */
export const StarButton = memo(function StarButton({ code, name, size = 22, watched }: {
  code: string
  name?: string
  /** 图标尺寸（px）；按钮实际点击区由 minWidth/minHeight 32px 保证 */
  size?: number
  /** 关注成员布尔（页级单一订阅派生，P2-68） */
  watched: boolean
}) {
  const { watched: shown, pending, toggle } = useWatchlistStarFrom(code, watched)
  return (
    <ActionIcon
      variant="subtle"
      color="gray"
      aria-label={shown ? '取消关注' : '添加关注'}
      aria-pressed={shown}
      disabled={pending}
      style={{ minWidth: 32, minHeight: 32, flexShrink: 0 }}
      onClick={(e) => {
        e.stopPropagation()
        void toggle(name)
      }}
    >
      {shown
        ? <IconStarFilled size={size} style={{ color: 'var(--sr-star)' }} />
        : <IconStar size={size} />}
    </ActionIcon>
  )
})

/** 信号时点渲染（统一 SignalMomentCell 双时组件）：groupColumns 与 detailColumns 信号时点列共用。
 *  主行=信号理论原始时刻（bar 标签 triggered_at，状态机周期精度 + 相对词），
 *  副行=扫描发现时刻（scan_discovered_at）+ 时间差；scan_discovered_at 为 null（旧数据）时仅单行，
 *  tooltip 三时间齐备（as_of 数据截止 · bar 标签 · 扫描时刻）。 */
function renderSignalMoment(
  period: string,
  triggeredAt: string | null,
  asOf: string | null,
  discoveredAt: string | null,
) {
  return <SignalMomentCell period={period} triggeredAt={triggeredAt} asOf={asOf} discoveredAt={discoveredAt} />
}

/**
 * 行情三要素渲染（价 · 涨跌幅着色 A股红涨绿跌 · 量比）：groupColumns 与 detailColumns 行情列共用。
 * T-49 拆三槽：价/涨幅/量比各占一槽，flex 比例分配（价 1 : 涨幅 1 : 量比 0.8，见
 * signalStreamPanel.css .sr-quote-slot），数值右对齐 + tabular-nums——与关注页 .sr-num-col
 * 对齐习惯统一；涨跌幅沿用 --sr-up/down 着色；tooltip 保留完整证据明细。
 */
function renderQuoteCell(ev: SignalEvent['evidence'] | undefined, labelOf: (c: string) => { text: string; color: string }) {
  const price = ev?.price
  const pct = evPct(ev)
  const isUp = pct >= 0
  const vr = ev?.volume_ratio
  return (
    <Tooltip label={evidenceLines(ev, labelOf).join('\n')} multiline w={280}>
      <div className="sr-quote-grid">
        <span className="sr-quote-slot">
          <span className="sr-quote-label">价</span>
          <span>{price != null ? price : '—'}</span>
        </span>
        <span className="sr-quote-slot sr-quote-pct" style={{ color: isUp ? 'var(--sr-up)' : 'var(--sr-down)' }}>
          {fmtPct(pct / 100)}
        </span>
        <span className="sr-quote-slot">
          <span className="sr-quote-label">量比</span>
          <span>{vr != null ? vr : '—'}</span>
        </span>
      </div>
    </Tooltip>
  )
}

/**
 * 信号流合并视图列定义（每股一行，多模板信号合并；展开查看明细）。
 * 列引用稳定（useMemo）→ SignalStreamPanel 内 streamCols useMemo 真正稳定，避免表格全量重建（D2）。
 * 星标列走 StarButton（useWatchlistStar 收编），watched 由页级订阅集合（watchedCodes）派生，
 * SignalCenter 不再持有关注集合状态；watchedCodes 引用仅随成员变化（useWatchlistCodes 保证），
 * 列定义不随 SSE tick 重建。
 */
export function useSignalColumns(opts: {
  period: string
  sortState: SignalSortState | null
  labelOf: (code: string) => { text: string; color: string }
  /** 关注成员集合（页级单一订阅派生，P2-68）：星标列 O(1) 成员查找，替代每行独立订阅 */
  watchedCodes: ReadonlySet<string>
}): { groupColumns: SrColumn<StockGroup>[]; detailColsBase: SrColumn<SignalEvent>[] } {
  const { period, sortState, labelOf, watchedCodes } = opts
  const navigate = useNavigate()

  const groupColumns = useMemo<SrColumn<StockGroup>[]>(() => [
    {
      // T-50 列宽不再在此声明：由 SignalStreamPanel 实测容器宽按比例注入
      // （CSS 变量 + nth-child，见 signalStreamPanel.css）；名称 maxWidth 140 省略号兜底，
      // code 参与整组收缩（组级 overflow hidden）
      key: SORT_COLUMN_KEYS.stock, title: '股票',
      sortOrder: sortState?.columnKey === SORT_COLUMN_KEYS.stock ? sortState.order : null,
      sorter: sorters[SORT_COLUMN_KEYS.stock],
      render: (_, g) => (
        // T-50 股票列收缩：整组 minWidth:0 允许随列宽收缩（不再被内容撑宽），
        // Button flex:1 + overflow:hidden 承接收缩，code 随整组裁剪（组级 ellipsis 兜底）
        <Group gap={4} wrap="nowrap" style={{ minWidth: 0 }}>
          <StarButton code={g.code} watched={watchedCodes.has(g.code)} />
          <Button variant="transparent" size="compact-sm"
            style={{ fontWeight: 500, padding: 0, height: 'auto', minWidth: 0, flex: 1, overflow: 'hidden' }}
            onClick={() => navigate(`/stocks/${g.code}`)}>
            <span style={{
              display: 'inline-block', maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis',
              whiteSpace: 'nowrap', verticalAlign: 'bottom',
            }}>{g.name || '—'}</span>{' '}
            <span style={{ color: 'var(--sr-text-2)', fontWeight: 400 }}>{g.code}</span>
          </Button>
        </Group>
      ),
    },
    {
      // 合并信号：列宽按比例注入（Tag wrap 可换行，不设固定内容宽）。
      // render 由 SignalStreamPanel 按 title 匹配 remap 统一覆盖（SignalTag 渲染），此处不再手写
      title: '合并信号',
      key: SORT_COLUMN_KEYS.signals,
      sortOrder: sortState?.columnKey === SORT_COLUMN_KEYS.signals ? sortState.order : null,
      sorter: sorters[SORT_COLUMN_KEYS.signals],
    },
    {
      // 统一 SignalMomentCell 双时组件：主=信号理论原始时刻（周期原生精度+相对词），
      // 副=扫描发现时刻+时间差（scan_discovered_at，旧数据 null 时仅单行），tooltip 三时间齐备；
      // T-49 右对齐（与关注页触发列对齐习惯统一）
      title: '信号时点', align: 'right',
      // 排序与展示对齐：as_of（数据时刻）优先，无则回退 bar 标签——盘中信号不再按未来 15:00 标签并排
      key: SORT_COLUMN_KEYS.time,
      sortOrder: sortState?.columnKey === SORT_COLUMN_KEYS.time ? sortState.order : null,
      sorter: sorters[SORT_COLUMN_KEYS.time],
      render: (_, g) => renderSignalMoment(period, g.triggered_at, g.as_of, g.discovered_at),
    },
    {
      // 行情三要素：价 / 涨幅着色（A股红涨绿跌）/ 量比 三槽（T-49，见 renderQuoteCell）；
      // 列宽按比例注入；align right 与槽内 flex-end 双保险（关注页 .sr-num-col 对齐习惯）
      title: '行情', ellipsis: true, align: 'right',
      key: SORT_COLUMN_KEYS.quote,
      sortOrder: sortState?.columnKey === SORT_COLUMN_KEYS.quote ? sortState.order : null,
      sorter: sorters[SORT_COLUMN_KEYS.quote],
      render: (_, g) => renderQuoteCell(g.best.evidence, labelOf),
    },
  ], [labelOf, navigate, period, sortState, watchedCodes])

  const detailColsBase = useMemo<SrColumn<SignalEvent>[]>(() => [
    {
      // 命中信号：列宽按比例注入（Tag 可换行）。
      // render 由 SignalStreamPanel 按 title 匹配 remap 统一覆盖，此处不再手写
      title: '命中信号',
    },
    {
      // 统一 SignalMomentCell 双时组件：同 groupColumns 时点列（主=理论原始，副=扫描发现）；
      // T-49 右对齐
      title: '信号时点', dataIndex: 'triggered_at', align: 'right',
      render: (_, r: SignalEvent) => renderSignalMoment(period, r.triggered_at, r.as_of, r.scan_discovered_at),
    },
    {
      // 行情三要素：同 groupColumns 行情列（T-49 三槽），列宽按比例注入
      title: '行情', ellipsis: true, align: 'right',
      render: (_, r) => renderQuoteCell(r.evidence, labelOf),
    },
  ], [labelOf, period])

  return { groupColumns, detailColsBase }
}
