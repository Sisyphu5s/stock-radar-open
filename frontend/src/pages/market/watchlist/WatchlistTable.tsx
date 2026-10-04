import { ActionIcon, Badge, Button, Popover, Text, Tooltip } from '@mantine/core'
import { IconStarFilled } from '@tabler/icons-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import DataTable from '../../../components/ui/DataTable'
import type { SrColumn, SrPaginationConfig, SrSorterResult } from '../../../components/ui/tableTypes'
import SignalTag from '../../../components/ui/SignalTag'
import { useElementSize } from '../../../hooks/useElementSize'
import { fmtNum, fmtPct } from '../../../utils/format'
import { toMarketEpochMs } from '../../../utils/time'
import { ageTagColor } from '../../../utils/signals'
import Sparkline from './Sparkline'
import { fmtClock, quoteTip } from './rowModel'
import type { WlRowView } from './rowModel'
import SignalMomentCell from '../../../components/ui/SignalMomentCell'
import WatchlistGroupMenu from './WatchlistGroupMenu'
import type { WatchlistGroup } from '../../../api/watchlistGroups'

interface WatchlistTableProps {
  rows: WlRowView[]
  current: number
  onPageChange: (p: number) => void
  /** 无限滚动模式：隐藏客户端分页（由页面 sentinel 分批灌入） */
  infinite?: boolean
  onOpen: (code: string) => void
  onUnfollow: (view: WlRowView) => void
  labelOf: (code: string) => { text: string; color: string }
  /** 无限滚动兜底：表格内部滚动容器滚到底部时回调（哨兵在表格外，IO 感知不到表格内部滚动） */
  onReachBottom?: () => void
  /** T-12 分组：行菜单（移入/移出分组）数据与回调 */
  groups: WatchlistGroup[]
  codeGroups: Map<string, ReadonlySet<number>>
  onToggleGroup: (code: string, groupId: number) => void
  onManageGroups: () => void
}

/** 触控目标（P1-a11y-5）：星标点击区 ≥28px，ActionIcon 内联定到 28px（见「标的」列） */

/** antd 色名（ageTagColor 出口）→ Mantine Badge 色名：'default' 非 Mantine 色名，落 gray 保视觉 */
const WL_TAG_COLOR: Record<string, string> = { red: 'red', orange: 'orange', blue: 'blue', default: 'gray' }
const wlTagColor = (c: string): string => WL_TAG_COLOR[c] ?? 'gray'

/** 表格滚动触底判定容差（px）：距底部 ≤ 该值视为触底 */
const REACH_BOTTOM_TOLERANCE = 48

/** 数值列排序：null（无数据）恒排最后（升序时 -Infinity） */
const numSorter = (pick: (r: WlRowView) => number | null) => (a: WlRowView, b: WlRowView) =>
  (pick(a) ?? Number.NEGATIVE_INFINITY) - (pick(b) ?? Number.NEGATIVE_INFINITY)

/** 无分组空集（行渲染复用，避免每次建 Set） */
const NO_GROUPS: ReadonlySet<number> = new Set()

/**
 * 列宽：容器宽按比例分配（等价百分比），关键列带 min 保底（等价 clamp）；
 * virtual 模式要求数值 px（rc 虚拟表用列宽计算偏移），故换算为 px 且恰好铺满容器。
 * 列序：标的/行情/走势/信号/触发/风险（操作列已并入整行点击，不再单列）。
 * P2-22：列宽取整后把余数（±几 px）摊平到前列，保证列宽和与 scrollX 严格一致，消除幽灵横滚。
 * min 保底按各列内容最小宽度核定（行情=价+涨跌幅 / 走势=迷你图 120 / 触发=双行时点）：
 * 防 nowrap 内容溢出 td 画到相邻列（行内重叠），窄容器兜底由 HScroll 承担（M1）。
 */
const COL_FRACS = [0.23, 0.14, 0.13, 0.18, 0.18, 0.14]
const COL_MINS = [140, 104, 120, 150, 120, 100]
const DEFAULT_TOTAL = 1000

/**
 * 虚拟行高估算（px，virtualRowEstimate）：按行内容实际高度取高值——标的(28 名称行+板块行)
 * 与信号(≤3 Tag 可两行)是最常见的最高单元格（≈54-56px）。估算贴近真实即可（测量会精确校正），
 * 但贴近可避免首帧/重测回退帧的行重叠；单行无信号行由 measureElement 实测收缩。
 */
const ROW_HEIGHT = 56

/** 信号 Tag 折叠阈值：超过折叠为 +N（Popover 展开全部） */
const VISIBLE_SIGNALS = 3

/**
 * 桌面语义化表格：真实表头 + 固定标的列 + virtual 虚拟滚动（scroll.y 内部滚动，
 * 高度由容器实测驱动）。整行可点跳转个股工作台（操作列已并入行点击）；
 * 信号折叠展示（≤3 +N，Popover 展开全部）；
 * 触发列走 resolveSignalMoment 状态机（信号触发时点，行情时点在 tooltip）；
 * 无 K 线走势显示占位。关闭无限滚动时客户端分页（每页 25）。
 */
export default function WatchlistTable({ rows, current, onPageChange, infinite, onOpen, onUnfollow, labelOf, onReachBottom, groups, codeGroups, onToggleGroup, onManageGroups }: WatchlistTableProps) {
  const navigate = useNavigate()
  // 容器实测尺寸：仅驱动响应式列宽（宽度）；高度不再手测——scroll.y 由 DataTable grow(T-52)
  // 内部 ResizeObserver 实测 viewport 可视高自动回填（分页条为兄弟节点自然占位），
  // 原 wrap 高 − 分页高 的双目标手工测量逻辑已泛化入薄壳
  const { ref: wrapRef, size: wrapSize } = useElementSize<HTMLDivElement>()
  // M1 lastGoodWRef 模式：实测 ≤0（首帧/临时隐藏）延用上次有效宽，首个有效值前退 DEFAULT_TOTAL
  // （与 widths/scrollX 同源 1000，替换旧 Math.max(320,..) 窄块 + 幽灵横滚矛盾）
  const lastGoodWRef = useRef(DEFAULT_TOTAL)
  const boxW = useMemo(() => {
    const measured = Math.round(wrapSize.width)
    if (measured > 0) lastGoodWRef.current = measured
    return lastGoodWRef.current
  }, [wrapSize.width])

  // 列排序（客户端）：受控状态保留在组件内——筛选/刷新只换 dataSource 不卸载本组件，排序跨筛选/刷新保留
  const [sort, setSort] = useState<{ key: string; order: 'ascend' | 'descend' } | null>(null)

  // 无限模式：表格内部滚动容器（DataTable .sr-dt-viewport，overflow:auto + height=scrollY）滚到底部时回调页面续载。
  // 哨兵位于表格 DOM 外，IntersectionObserver 感知不到表格内部滚动，此为桌面无限滚动的真正驱动；
  // rows.length 入依赖：首次挂载 viewport 可能未渲染（虚拟表），首批数据到达后重试绑定
  useEffect(() => {
    if (!onReachBottom) return
    const holder = wrapRef.current?.querySelector<HTMLElement>('.sr-dt-viewport')
    if (!holder) return
    const onScroll = () => {
      if (holder.scrollHeight - holder.scrollTop - holder.clientHeight <= REACH_BOTTOM_TOLERANCE) onReachBottom()
    }
    holder.addEventListener('scroll', onScroll, { passive: true })
    return () => holder.removeEventListener('scroll', onScroll)
  }, [onReachBottom, rows.length])

  const widths = useMemo(() => {
    const total = boxW
    const clamped = COL_FRACS.map((f, i) => Math.max(f * total, COL_MINS[i]))
    const sum = clamped.reduce((a, b) => a + b, 0)
    const scale = Math.min(1, total / sum)
    const scaled = clamped.map((w) => w * scale)
    const raw = scaled.map((w) => Math.round(w))
    // 取整余数（±几个 px）摊到前列：列宽和 == scrollX，与 virtual 偏移计算完全一致，消除幽灵横滚
    const diff = total - raw.reduce((a, b) => a + b, 0)
    const out = [...raw]
    for (let i = 0; i < Math.abs(diff); i++) out[i % COL_FRACS.length] += Math.sign(diff)
    return out
  }, [boxW])

  // scrollX 与列宽和严格一致（widths 已把余数摊平到 total==boxW，P2-22 消除幽灵横滚）
  const totalW = useMemo(() => widths.reduce((a, b) => a + b, 0), [widths])

  // 触发列 tooltip：信号时点（状态机双时间+相对时间）+ 行情快照时点（P1-54 触发列信息修正），
  // 直接内联 trigTooltip(r.moment, r.quoteAt)，避免引入每渲染变化的闭包依赖

  // 列定义 useMemo：避免每次渲染重建 render 闭包（渲染闭包含 onOpen/onUnfollow/labelOf 引用）
  const columns: SrColumn<WlRowView>[] = useMemo(() => [
    {
      title: '标的', key: 'stock', width: widths[0], fixed: 'left',
      render: (_, r) => (
        <div className="sr-wl-stock">
          <div className="sr-wl-stock-line">
            <Tooltip label="取消关注">
              <ActionIcon
                variant="subtle" color="gray"
                className="sr-wl-star"
                style={{ minWidth: 28, minHeight: 28, flexShrink: 0 }}
                onClick={(e) => { e.stopPropagation(); onUnfollow(r) }}
                aria-label={`取消关注 ${r.name}`}
              >
                <IconStarFilled style={{ color: 'var(--sr-star)' }} />
              </ActionIcon>
            </Tooltip>
            <WatchlistGroupMenu
              code={r.code}
              memberGroupIds={codeGroups.get(r.code) ?? NO_GROUPS}
              groups={groups}
              onToggle={onToggleGroup}
              onManage={onManageGroups}
            />
            <Button variant="transparent" size="compact-sm" className="sr-wl-stock-name" onClick={(e) => { e.stopPropagation(); onOpen(r.code) }}>
              {r.name}
            </Button>
            <span className="sr-wl-stock-code">{r.code}</span>
          </div>
          {r.sector ? (
            <span className="sr-wl-stock-sector">{r.sector}</span>
          ) : <span className="sr-wl-dash">—</span>}
        </div>
      ),
    },
    {
      title: '行情', key: 'quote', width: widths[1], align: 'right', className: 'sr-num-col',
      sorter: numSorter((r) => r.pct),
      sortOrder: sort?.key === 'quote' ? sort.order : null,
      render: (_, r) => {
        const pctColor = r.pct == null ? undefined : r.pct >= 0 ? 'var(--sr-up)' : 'var(--sr-down)'
        return r.price != null ? (
          <span className="sr-wl-quote-cell">
            <span style={{ fontWeight: 600, color: 'var(--sr-text-1)', fontVariantNumeric: 'tabular-nums' }}>
              {fmtNum(r.price, 2)}
            </span>
            <span style={{ fontSize: 'var(--sr-font-xs)', color: pctColor }}>
              {r.pct != null ? fmtPct(r.pct / 100, 2) : '—'}
            </span>
          </span>
        ) : <span className="sr-wl-dash">—</span>
      },
    },
    {
      title: '走势', key: 'spark', width: widths[2],
      render: (_, r) => (
        r.spark
          ? <Sparkline closes={r.spark.closes} code={r.code} />
          : <span className="sr-spark-ph" role="img" aria-label="暂无走势数据">—</span>
      ),
    },
    {
      title: '信号', key: 'sigTmpl', width: widths[3],
      sorter: (a, b) => a.signals.length - b.signals.length,
      sortOrder: sort?.key === 'sigTmpl' ? sort.order : null,
      render: (_, r) => r.hasSig ? (
        <div className="sr-wl-sigcell">
          <div className="sr-wl-sigs">
            {r.signals.slice(0, VISIBLE_SIGNALS).map((s) => (
              <SignalTag key={s} code={s} labelOf={labelOf} size="small" />
            ))}
            {r.signals.length > VISIBLE_SIGNALS && (
              <Popover position="bottom" withArrow={false}>
                <Popover.Target>
                  <Badge className="sr-wl-fold" onClick={(e) => e.stopPropagation()}>+{r.signals.length - VISIBLE_SIGNALS}</Badge>
                </Popover.Target>
                <Popover.Dropdown>
                  <Text fw={600} size="sm" mb={6}>全部信号（{r.signals.length}）</Text>
                  <div className="sr-wl-sig-pop">
                    {r.signals.map((s) => (
                      <SignalTag key={s} code={s} labelOf={labelOf} size="small" />
                    ))}
                  </div>
                </Popover.Dropdown>
              </Popover>
            )}
          </div>
        </div>
      ) : <span className="sr-wl-dash">—</span>,
    },
    {
      // 列头与单元格对齐一致（单元格 flex-end 右对齐）→ column align 解决，不改 CSS
      title: '触发', key: 'trig', width: widths[4], align: 'right',
      // P2-65：latestAt/quoteAt 为 naive ISO，按 toMarketEpochMs 数值排序（与全站惯例一致，
      // 避免字符串字典序下「同日多事件」排序不稳）
      sorter: (a, b) => toMarketEpochMs(a.latestAt ?? a.quoteAt) - toMarketEpochMs(b.latestAt ?? b.quoteAt),
      sortOrder: sort?.key === 'trig' ? sort.order : null,
      render: (_, r) => (
        <div className="sr-wl-trigcell">
          {r.triggeredAt ? (
            <SignalMomentCell period={r.period} triggeredAt={r.triggeredAt} asOf={r.asOf} discoveredAt={r.discoveredAt} extraTip={quoteTip(r.quoteAt)} />
          ) : (
            <span className="sr-wl-time" title="最新行情时点">
              {fmtClock(r.quoteAt) ?? <span className="sr-wl-dash">—</span>}
            </span>
          )}
          <div className="sr-wl-trigrow">
            {r.age && <Badge color={wlTagColor(ageTagColor(r.age))} variant="light" size="sm" className="sr-wl-tag">{r.age}</Badge>}
            {r.isNew && <Badge color="red" variant="light" size="sm" className="sr-wl-tag">新</Badge>}
          </div>
        </div>
      ),
    },
    {
      title: '风险', key: 'risk', width: widths[5], align: 'right', className: 'sr-num-col',
      sorter: numSorter((r) => r.vol),
      sortOrder: sort?.key === 'risk' ? sort.order : null,
      render: (_, r) => (
        <div className="sr-wl-risk-cell">
          <span className="sr-wl-risk-pair">
            <span className="sr-wl-risk-label">波</span>
            {r.vol != null ? fmtPct(r.vol, 1) : <span className="sr-wl-dash">—</span>}
          </span>
          <span className="sr-wl-risk-pair">
            <span className="sr-wl-risk-label">撤</span>
            {r.dd != null ? fmtPct(r.dd, 1) : <span className="sr-wl-dash">—</span>}
          </span>
        </div>
      ),
    },
  ], [widths, onOpen, onUnfollow, labelOf, navigate, sort, groups, codeGroups, onToggleGroup, onManageGroups])

  // ===== P2-69 DataTable memo：onChange/onRow/pagination 内联对象/箭头每次渲染重建，
  // 击穿 DataTable memo → 全部提为稳定引用（分页对象含内联 onChange/showTotal，一并 memo 化） =====
  const handleTableChange = useCallback((
    _p: SrPaginationConfig,
    _f: Record<string, unknown>,
    sorter: SrSorterResult<WlRowView>,
  ) => {
    const s = Array.isArray(sorter) ? sorter[0] : sorter
    const key = s?.columnKey
    const order = s?.order
    if ((order === 'ascend' || order === 'descend') && key != null) {
      setSort({ key: String(key), order })
    } else {
      setSort(null)
    }
  }, [setSort])

  const handleRow = useCallback((r: WlRowView) => ({ onClick: () => onOpen(r.code) }), [onOpen])

  const tablePagination = useMemo(() => (infinite ? false : {
    current, pageSize: 25,
    total: rows.length, showSizeChanger: false,
    showTotal: (t: number) => `共 ${t} 只`,
    onChange: (p: number) => onPageChange(p),
  }), [infinite, current, rows.length, onPageChange])

  return (
    <div className="sr-wl-table-wrap" ref={wrapRef}>
      <DataTable<WlRowView>
        rowKey="code"
        columns={columns}
        dataSource={rows}
        virtual
        virtualRowEstimate={ROW_HEIGHT}
        grow
        scrollX={totalW}
        // 受控排序：点击表头 → onChange 更新本地 sort 状态（antd 客户端排序作用于 dataSource）
        onChange={handleTableChange}
        // virtual 双表结构(T-118):表头独立 .sr-dt-vhead sticky 吸顶,与行区绝对定位
        // 无冲突(基板 sticky 缺省 true),此处不再显式关闭——表头滚动时保持可见
        // 整行可点：点击任意单元格跳转个股工作台（内部按钮已 stopPropagation）
        onRow={handleRow}
        // 斑马纹由 DataTable 内建 zebra 承担（按真实 index 补 sr-dt-zebra,同源 --sr-zebra-bg 令牌）
        pagination={tablePagination}
        emptyText="暂无关注股票"
      />
    </div>
  )
}
