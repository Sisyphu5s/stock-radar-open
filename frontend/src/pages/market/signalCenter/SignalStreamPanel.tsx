import { Badge, Button, Drawer, Group, Indicator, Loader, Popover, Progress, SegmentedControl, Select, Switch, Text, TextInput, Tooltip } from '@mantine/core'
import { IconAdjustments, IconPlayerStop, IconRefresh, IconSearch, IconStar, IconTrash, IconX } from '@tabler/icons-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import MultiSelectGrid, { msgridDotColor, type MsGridOption } from '../../../components/MultiSelectGrid'
import DataTable from '../../../components/ui/DataTable'
import EmptyState from '../../../components/ui/EmptyState'
import { useElementSize } from '../../../hooks/useElementSize'
import IconTextButton from '../../../components/ui/IconTextButton'
import StatStrip from '../../../components/ui/StatStrip'
import InfiniteScrollSentinel from '../../../components/ui/InfiniteScrollSentinel'
import SignalTag from '../../../components/ui/SignalTag'
import TriStateGroup, { type TriStateOption } from '../../../components/ui/TriStateGroup'
import Toolbar from '../../../components/ui/Toolbar'
import type { SrColumn, SrExpandableConfig, SrPaginationConfig, SrSorterResult } from '../../../components/ui/tableTypes'
import type { ScanUniverse, SignalCatalogItem, SignalEvent } from '../../../api/client'
import { useScanStatus, scanCoverageText } from '../../../data/scanStatus'
import { formatFullTime, toMarketEpochMs } from '../../../utils/time'
import { isMinutePeriod, periodLabel } from '../../../utils/periods'
import { SIGNAL_CATEGORY_COLOR } from '../../../utils/signals'
import MobileSignalList from '../signals/MobileSignalList'
import IndexStrip from './IndexStrip'
import type { SignalDir, SignalMatch, StockGroup } from './context'
import { MATCH_OPTIONS, PERIOD_OPTIONS, SECTOR_OPTIONS, SECTOR_COLOR, TIME_RANGE_OPTIONS, TIME_RANGE_DEFAULT, useSignalCenter } from './context'
import './signalStreamPanel.css'

/** 排除方向控制（排除 / 无操作）：dir=1「只看」已废弃并入服务端 signalFilter（见 context.ts），
 * 仅保留 -1 排除 / 0 取消排除两态；排除=错误红 --sr-error（语义色，与涨跌色无关） */
const EXCLUDE_DIR_OPTIONS: TriStateOption[] = [
  { v: -1, icon: '×', tip: '排除：过滤掉命中该信号的股票', color: 'var(--sr-error)' },
  { v: 0, icon: '–', tip: '取消排除', color: 'var(--sr-text-3)' },
]

// ===== 列宽比例（T-50）：主表 4 数据列 + 展开箭头 32px；展开明细表 3 列 =====
/** 主表数据列相对占比（相对比例，layoutWidths 内部按和归一；基于任务卡建议 22/16/14/33） */
const GROUP_COL_FRACS = [0.22, 0.16, 0.14, 0.33] as const
/** 主表数据列 min 钳制（px）：窄容器（比例 < min）时列宽落到 min，表格超宽 → scrollX 兜底横滚 */
const GROUP_COL_MINS = [120, 110, 100, 180] as const
/** 展开明细表列相对占比（和 = 1） */
const DETAIL_COL_FRACS = [0.33, 0.3, 0.37] as const
/** 展开明细表列 min 钳制（px） */
const DETAIL_COL_MINS = [150, 140, 180] as const
/** 展开箭头列宽（px）：与 DataTable 内置 ARROW_COL_WIDTH 一致，布局计算单独扣除 */
const EXPAND_ARROW_COL = 32
/** 扫描调度表缺失时的新鲜度回退阈值（秒）：与 usePeriodScan FALLBACK_INTERVAL_SEC 同值 */
const FALLBACK_SCAN_INTERVAL_SEC = 5 * 60

/**
 * 列宽两档布局（T-50）：可用宽 total 充足 → 按 fracs 相对比例分配（内部按和归一，
 * 比例不因和≠1 变形）并摊平取整余数（列宽和 == total，无幽灵横滚）；
 * 可用宽不足（min 和 > total）→ min 档（列宽和 == min 和，表格超宽 → 由调用方 scrollX 兜底横滚）。
 */
function layoutWidths(fracs: readonly number[], mins: readonly number[], total: number): { widths: number[]; sum: number } {
  const minSum = mins.reduce((a, b) => a + b, 0)
  if (total <= 0 || minSum > total) return { widths: [...mins], sum: minSum }
  const fracSum = fracs.reduce((a, b) => a + b, 0)
  const rounded = fracs.map((f) => Math.round((f / fracSum) * total))
  const diff = total - rounded.reduce((a, b) => a + b, 0)
  const out = [...rounded]
  for (let i = 0; i < Math.abs(diff); i++) out[i % fracs.length] += Math.sign(diff)
  return { widths: out, sum: total }
}

/** 排除弹层：信号全量列表 + 搜索 + 每行 TriStateGroup 排除切换（dir=-1 浏览层过滤）。
 *  按分类分组展示（option.group，与筛选网格同源），搜索过滤保留；组标题仅在有匹配时出现。
 *  「仅看已排除」切换（P1-21）：只列出 dir=-1 的信号，便于复核已排除项。
 *  props 全部稳定引用（signalOpts/dir/setDirOf），弹层展开/搜索/仅看已排除为本地 state，不触发表格重建 */
function ExcludePopover({ options, dir, setDirOf }: {
  options: MsGridOption[]
  dir: Record<string, SignalDir>
  setDirOf: (code: string, v: SignalDir) => void
}) {
  const [q, setQ] = useState('')
  const [onlyExcl, setOnlyExcl] = useState(false)
  const kw = q.trim().toLowerCase()
  // 组顺序 = 选项出现顺序（signalOpts 已按分类分组）；搜索过滤在组内执行；仅看已排除过滤 dir=-1
  const groups = useMemo(() => {
    const byGroup = new Map<string, MsGridOption[]>()
    let total = 0
    for (const o of options) {
      if (kw && !o.label.toLowerCase().includes(kw) && !o.value.toLowerCase().includes(kw)) continue
      if (onlyExcl && dir[o.value] !== -1) continue
      const g = o.group ?? '其他'
      const arr = byGroup.get(g) ?? []
      arr.push(o)
      byGroup.set(g, arr)
      total++
    }
    return { groups: [...byGroup.entries()].map(([name, items]) => ({ name, items })), total }
  }, [options, kw, onlyExcl, dir])
  const exclTotal = useMemo(() => Object.values(dir).filter((v) => v === -1).length, [dir])
  return (
    <div className="sr-excl-pop">
      <TextInput size="sm" leftSection={<IconSearch size={14} />} placeholder="搜索信号"
        value={q} onChange={(e) => setQ(e.currentTarget.value)} className="sr-excl-pop-search"
        rightSection={q ? (
          <button type="button" aria-label="清空搜索" onClick={() => setQ('')}
            style={{ display: 'flex', border: 'none', background: 'none', cursor: 'pointer', color: 'var(--sr-text-3)', padding: 0 }}>
            <IconX size={14} />
          </button>
        ) : undefined}
        rightSectionPointerEvents="all" />
      <label className="sr-excl-pop-filter">
        <span className="sr-excl-pop-filter-label">
          仅看已排除{exclTotal > 0 && <Badge color="red" variant="light" size="xs" className="sr-excl-pop-filter-count">{exclTotal}</Badge>}
        </span>
        <Switch size="sm" checked={onlyExcl} onChange={(e) => setOnlyExcl(e.currentTarget.checked)} aria-label="仅看已排除" />
      </label>
      <div className="sr-excl-pop-list">
        {groups.total === 0 ? (
          <Text span style={{ fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-2)' }}>{onlyExcl ? '暂无已排除信号' : '无匹配信号'}</Text>
        ) : groups.groups.map(({ name, items }) => (
          <div key={name} className="sr-excl-pop-group">
            <div className="sr-excl-pop-group-title">
              <span className="sr-msgrid-dot" style={{ background: msgridDotColor(items[0]?.color) }} />
              <span className="sr-excl-pop-group-name">{name}</span>
            </div>
            {items.map((o) => (
              <div key={o.value} className="sr-excl-pop-row">
                <span className="sr-msgrid-dot" style={{ background: msgridDotColor(o.color) }} />
                <Text span style={{
                  fontSize: 'var(--sr-font-sm)', flex: 1, minWidth: 0,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{o.label}</Text>
                <Text span style={{ fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-2)', flexShrink: 0 }}>{o.value}</Text>
                <TriStateGroup value={dir[o.value] ?? 0} onChange={(v) => setDirOf(o.value, v)} options={EXCLUDE_DIR_OPTIONS} />
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}

export default function SignalStreamPanel() {
  const navigate = useNavigate()
  const c = useSignalCenter()
  const {
    period, setPeriod, sectors, setSectors, signalFilter, setSignalFilter,
    signalMatch, setSignalMatch,
    labelOf, watchlistOnly, setWatchlistOnly,
    timeRange, setTimeRange, excludeToday, setExcludeToday,
    pageLoading, refreshing, pageError, reload,
    sortedGroups, infStats,
    scanBusy, scanProgress, cancelScan, scanUniverse,
    groupCols, detailColumns, isMobile,
    infEvents, infTotal, infHasMore, infLoading, infError,
    infResetKey, loadInfiniteMore,
    dir, setDirOf, clearDir, catalog, signalCategories,
    schemes, activeSchemeId, saveScheme, applyScheme, removeScheme,
    isDesktop, watchlistPaneOpen, setWatchlistPaneOpen, watchlistCount,
    watchlistCodes,
    setSortState,
  } = c

  // ===== T-50 列宽自适应：容器实测宽 → 比例列宽（min 钳制两档，默认不横滚） =====
  // DataTable 薄壳不把列 width 应用到 th/td（仅 fixed 列偏移计算用），列宽经 CSS 变量 +
  // nth-child 注入（signalStreamPanel.css 的 .sr-stream-main/.sr-stream-detail 列序注释）；
  // scrollX = max(容器宽, min 列宽和) → 窄容器时表格超宽，横向滚动兜底（数值而非 max-content）
  const { ref: tableWrapRef, size: tableWrapSize } = useElementSize<HTMLDivElement>()
  // A2:容器实测宽 ≤0(挂载首帧/临时隐藏)时延用上次有效宽度,不再 clamp 到 320 触发
  // min 档(列宽和 542px)把宽屏表格钉成窄块;首帧无任何有效值时退 320 兜底
  const lastGoodWRef = useRef(320)
  const layout = useMemo(() => {
    const measured = Math.round(tableWrapSize.width)
    if (measured > 0) lastGoodWRef.current = measured
    const W = lastGoodWRef.current
    const g = layoutWidths(GROUP_COL_FRACS, GROUP_COL_MINS, W - EXPAND_ARROW_COL)
    const d = layoutWidths(DETAIL_COL_FRACS, DETAIL_COL_MINS, W)
    return {
      scrollX: g.sum + EXPAND_ARROW_COL,
      detailScrollX: d.sum,
      groupVars: {
        '--sr-cw2': `${g.widths[0]}px`,
        '--sr-cw3': `${g.widths[1]}px`,
        '--sr-cw4': `${g.widths[2]}px`,
        '--sr-cw5': `${g.widths[3]}px`,
      } as CSSProperties,
      detailVars: {
        '--sr-cd1': `${d.widths[0]}px`,
        '--sr-cd2': `${d.widths[1]}px`,
        '--sr-cd3': `${d.widths[2]}px`,
      } as CSSProperties,
    }
  }, [tableWrapSize.width])

  // 列渲染统一到模板组件：信号 Tag（保留各列 sorter/宽度等既有配置）
  const { streamCols, streamDetailCols } = useMemo(() => {
    const remap = (cols: SrColumn<any>[]) => cols.map((col) => {
      if (col.title === '合并信号' || col.title === '命中信号') {
        return {
          ...col,
          render: (_: unknown, r: { signals?: string[]; events?: unknown[] }) => (
            <Group gap={4} wrap="wrap">
              {(r.signals ?? []).map((s) => <SignalTag key={s} code={s} labelOf={labelOf} />)}
              {Array.isArray(r.events) && r.events.length > 1 && (
                <Badge color="blue" variant="light" style={{ borderRadius: 5, fontWeight: 600 }}>{r.events.length} 次命中</Badge>
              )}
            </Group>
          ),
        }
      }
      return col
    })
    return {
      streamCols: remap(groupCols) as SrColumn<StockGroup>[],
      streamDetailCols: remap(detailColumns) as SrColumn<SignalEvent>[],
    }
  }, [groupCols, detailColumns, labelOf])

  // ===== 页内搜索（前端过滤已加载列表）：代码/名称包含匹配；与顶栏全局搜索职责区分
  // （页内=过滤当前列表，顶栏=跳转个股，刷新按钮 tooltip 同述）；服务端筛选之上叠加，搜索结果不计入服务端 total
  const [query, setQuery] = useState('')
  const searchFiltered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return sortedGroups
    return sortedGroups.filter((g) =>
      g.code.toLowerCase().includes(q) || (g.name ?? '').toLowerCase().includes(q))
  }, [sortedGroups, query])
  const visibleGroups = searchFiltered
  const listCount = searchFiltered.length
  const statusText = infTotal > 0 || listCount > 0
    ? `已加载 ${listCount} 只 · ${infEvents.length}/${infTotal} 条${query.trim() ? '（搜索）' : ''}`
    : ''

  // 最后扫描新鲜度：30s 轮询共享单例（池化，全站只发一份；失败静默保留旧值，不打断列表）
  const scanStatus = useScanStatus() ?? null
  // T-129 新鲜度派生：按当前周期 periods[period]（不再用全局 last_scan_at，避免展示他周期口径），
  // 过期阈值 = 后端调度表 schedule[period]（单一事实源 Settings.scan_schedule；缺失回退 5min）
  const lastScanView = useMemo(() => {
    if (!scanStatus) return null
    const raw = scanStatus.periods?.[period] ?? null
    if (!raw) return { text: '从未扫描', status: 'default' as const, stale: false }
    const ms = toMarketEpochMs(raw)
    if (!ms) return { text: formatFullTime(raw), status: 'default' as const, stale: false }
    const diff = Date.now() - ms
    const ageMin = Math.floor(diff / 60e3)
    const intervalSec = scanStatus.schedule?.[period] ?? FALLBACK_SCAN_INTERVAL_SEC
    const stale = diff > intervalSec * 1000
    const text = diff < 60e3 ? '刚刚'
      : ageMin < 60 ? `${ageMin} 分钟前`
        : diff < 86400e3 ? `${Math.floor(diff / 3600e3)} 小时前`
          : formatFullTime(raw)
    const status: 'success' | 'warning' | 'default' = stale
      ? 'default'
      : scanStatus.in_trading_session ? 'success' : 'warning'
    return { text, status, stale }
  }, [scanStatus, period])

  // 信号多选筛选项：按分类分组（signalCategories 为单一事实源，组顺序 = 分类出现顺序，
  // 组内按常用度 rank 排序；无分类信号兜底归入「其他」组，不丢失筛选入口）；
  // 色点按分类（SignalTag 同源 SIGNAL_CATEGORY_COLOR），group 字段驱动网格/排除弹层分组展示（T-27）
  const signalOpts = useMemo<MsGridOption[]>(() => {
    const byCode = new Map(catalog.map((s) => [s.code, s]))
    const out: MsGridOption[] = []
    for (const { category, codes } of signalCategories) {
      const items = codes
        .map((code) => byCode.get(code))
        .filter((s): s is SignalCatalogItem => !!s)
        .sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999))
      for (const s of items) {
        out.push({ value: s.code, label: s.name, color: SIGNAL_CATEGORY_COLOR[s.category] ?? 'default', group: category })
      }
    }
    for (const s of catalog.filter((x) => !x.category).sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999))) {
      out.push({ value: s.code, label: s.name, color: 'default', group: '其他' })
    }
    return out
  }, [catalog, signalCategories])
  // 板块筛选项静态（SECTOR_OPTIONS/SECTOR_COLOR 模块常量）→ useMemo 稳定引用，避免每次渲染重建
  const sectorOpts = useMemo(() => SECTOR_OPTIONS.map((s) => ({ value: s, label: s, color: SECTOR_COLOR[s] })), [])

  // 排除指示条：dir 中存在 -1（排除）时展示，N = 排除信号数；清空按钮调用 clearDir（全置 0）
  const exclCount = useMemo(() => {
    let n = 0
    for (const v of Object.values(dir)) if (v === -1) n++
    return n
  }, [dir])

  // 刷新按钮 tooltip：页内=触发扫描+重拉（非日线，日线仅重拉），顶栏=仅重拉（职责区分，P0-31）
  const refreshTip = '页内刷新：触发扫描 + 重拉列表；顶栏刷新：仅重拉（不触发计算）'

  // 筛选抽屉开关：默认收起（桌面/移动一致，仅角标按钮可见；T-27 桌面不再默认展开，
  // 打开时 mask 仅移动端 → 列表不遮挡、行情列可横滚查看）；进入移动端自动收起
  const [filterOpen, setFilterOpen] = useState(false)
  useEffect(() => {
    if (isMobile) setFilterOpen(false)
  }, [isMobile])

  // 生效筛选数（驱动「筛选」按钮角标）：任一非缺省筛选项 + 搜索词；
  // 命中语义仅在多选（>1）时计入——与抽屉可见条件一致（≤1 时已自动重置为 any，T-27）
  const activeFilterCount = useMemo(() => {
    let n = 0
    if (sectors.length) n++
    if (signalFilter.length) n++
    if (signalMatch !== 'any' && signalFilter.length > 1) n++
    if (excludeToday) n++
    if (watchlistOnly) n++
    if (timeRange !== TIME_RANGE_DEFAULT) n++
    if (query.trim()) n++
    return n
  }, [sectors, signalFilter, signalMatch, excludeToday, watchlistOnly, timeRange, query])

  // 聚合摘要首屏：今日新增事件数（服务端 stats 全量聚合，不受分页截断）；
  // 命中数消费浏览层过滤后（排除 + 搜索）的实际展示行数 listCount——与列表所见一致（T-27 口径统一）；
  // 与服务端口径的差值由排除指示条（已排除 N 个信号）与状态文案（搜索标注）另行呈现
  const todayNew = infStats ? infStats.today_new : null

  // 重置全部筛选（抽屉 footer，T-27）：清空全部服务端筛选字段 + 页内搜索词；
  // dir 排除为浏览层（有独立清空入口与指示条），周期为展示粒度，均不在此重置
  const resetAllFilters = useCallback(() => {
    setTimeRange(TIME_RANGE_DEFAULT)
    setSectors([])
    setSignalFilter([])
    setSignalMatch('any')
    setExcludeToday(false)
    setWatchlistOnly(false)
    setQuery('')
  }, [setTimeRange, setSectors, setSignalFilter, setSignalMatch, setExcludeToday, setWatchlistOnly])

  // ===== 筛选方案（T-13）：命名保存当前筛选现场；应用经 ctx.applyScheme 写回（含 dir） =====
  const [schemeName, setSchemeName] = useState('')
  const saveCurrentScheme = useCallback(() => {
    if (!schemeName.trim()) return
    saveScheme(schemeName)
    setSchemeName('')
  }, [schemeName, saveScheme])

  const searchInput = (
    <TextInput
      size="sm"
      leftSection={<IconSearch size={14} />}
      placeholder="搜代码/名称"
      value={query} onChange={(e) => setQuery(e.currentTarget.value)}
      aria-label="按代码或名称过滤当前列表"
      style={{ width: '100%' }}
      rightSection={query ? (
        <button type="button" aria-label="清空搜索" onClick={() => setQuery('')}
          style={{ display: 'flex', border: 'none', background: 'none', cursor: 'pointer', color: 'var(--sr-text-3)', padding: 0 }}>
          <IconX size={14} />
        </button>
      ) : undefined}
      rightSectionPointerEvents="all"
    />
  )

  // ===== 分钟周期空态/计算中文案（T-54）：随扫描范围 universe 动态（watchlist 关注票 /
  // top_n 成交额前 N / codes 指定代码，前端 UI 只暴露前两项，codes 供后端直连等客户端）
  const MINUTE_BUSY_TEXT: Record<ScanUniverse, string> = {
    watchlist: '正在计算关注票分钟信号，请稍候…',
    top_n: '正在计算成交额 Top N 分钟信号，请稍候…',
    codes: '正在计算指定代码分钟信号，请稍候…',
  }
  const MINUTE_EMPTY_TEXT: Record<ScanUniverse, string> = {
    watchlist: '该周期暂无信号（分钟线仅覆盖关注列表股票，点刷新重算）',
    top_n: '该周期暂无信号（分钟线仅覆盖成交额前 N 股票，点刷新重算）',
    codes: '该周期暂无信号（分钟线仅覆盖指定代码股票，点刷新重算）',
  }

  // 统一空态文案（05 §5.1 三态语境）：日线=纯筛选结果空；分钟=仅覆盖所选范围（低功耗按需，
  // scanBusy 区分计算中）；周/月=计算未完成（口径提示，scanBusy 区分计算中）；
  // 页内搜索无匹配时优先提示搜索态。空态渲染复用 EmptyState 薄壳（移动端/桌面端 locale 两处）
  const emptyText = query.trim()
    ? '没有匹配的股票（按代码/名称过滤当前列表）'
    : period === 'daily'
    ? '当前筛选条件下暂无信号'
    : isMinutePeriod(period)
      ? scanBusy
        ? MINUTE_BUSY_TEXT[scanUniverse]
        : MINUTE_EMPTY_TEXT[scanUniverse]
      : scanBusy
        ? '正在计算该周期信号，请稍候…'
        : '该周期暂无信号（周/月信号需完成计算后才有结果，点刷新重算）'

  // ===== P2-69 DataTable memo：onChange/locale/expandable 内联对象每次渲染重建，
  // 击穿 DataTable memo 与列 useMemo → 全部提为稳定引用 =====
  const handleTableChange = useCallback((
    _p: SrPaginationConfig,
    _f: Record<string, unknown>,
    sorter: SrSorterResult<StockGroup>,
  ) => {
    // 多列排序时取第一个 sorter；columnKey 缺失或 order 为 null（取消排序）→ 清除排序状态
    const s = Array.isArray(sorter) ? sorter[0] : sorter
    const key = s.columnKey
    if ((s.order === 'ascend' || s.order === 'descend') && key != null) {
      setSortState({ columnKey: String(key), order: s.order })
    } else {
      setSortState(null)
    }
  }, [setSortState])

  const tableLocale = useMemo<{ emptyText: ReactNode }>(
    () => ({ emptyText: <EmptyState description={emptyText} /> }),
    [emptyText],
  )

  // 展开明细内嵌表依赖 layout.detailVars/streamDetailCols（均已 useMemo 稳定），
  // rowExpandable 只读 g.events.length → 整包 useMemo 稳定
  const expandableConfig = useMemo<SrExpandableConfig<StockGroup>>(() => ({
    expandedRowRender: (g) => (
      // 展开行内嵌表走基板 DataTable（scrollX 数值 + 比例列宽注入，T-50）：
      // 明细 3 列列宽按比例分配（detailVars），窄容器 scrollX=min 和 → 兜底横滚
      <div style={layout.detailVars}>
        <DataTable rowKey="id" className="sr-stream-detail" columns={streamDetailCols} dataSource={g.events}
          pagination={false} scrollX={layout.detailScrollX} />
      </div>
    ),
    rowExpandable: (g) => g.events.length > 1,
  }), [layout, streamDetailCols])

  return (
    <div className="sr-stream-panel" style={{ height: '100%', minHeight: 0, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {/* 功能栏（一级，常显）：周期切换 + 刷新；低频筛选收进右侧抽屉 */}
      <div style={{ flexShrink: 0, minWidth: 0 }}>
        <Toolbar style={{ flexShrink: 0 }}
          tail={
            <Group gap={4} wrap="nowrap">
              {/* T-53 窄屏关注流入口：<992 左栏转抽屉，入口按钮带关注计数 Badge */}
              {!isDesktop && (
                <Button size="xs" variant="default" leftSection={<IconStar size={14} />}
                  onClick={() => setWatchlistPaneOpen(true)}
                  aria-label={watchlistPaneOpen ? '关闭关注流' : '打开关注流'}
                  rightSection={watchlistCount > 0 ? (
                    <Badge color="blue" variant="light" size="xs" className="sr-wlp-entry-badge">{watchlistCount}</Badge>
                  ) : undefined}>
                  关注流
                </Button>
              )}
              <Indicator inline color="red" label={activeFilterCount} size={16} offset={2} showZero={false}>
                <Button size="xs" leftSection={<IconAdjustments size={14} />} onClick={() => setFilterOpen(!filterOpen)}>筛选</Button>
              </Indicator>
              <Tooltip label={refreshTip}>
                <IconTextButton icon={<IconRefresh />} text="刷新"
                  loading={refreshing || infLoading} onClick={reload} />
              </Tooltip>
            </Group>
          }>
          <div className="sr-period-scroll">
            <SegmentedControl size="xs" value={period} onChange={(v) => setPeriod(String(v))} data={PERIOD_OPTIONS} style={{ height: 'var(--sr-ctl-h)', flexShrink: 0 }}
              styles={{ label: { height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' } }} />
          </div>
        </Toolbar>
        {/* L0 结论条(T-47):今日新 · 命中 · 最后扫描(替代旧摘要行+新鲜度行) */}
        <StatStrip
          loading={infLoading}
          scrollable={isMobile}
          items={[
            { key: 'today', label: '今日新', value: todayNew ?? '—' },
            { key: 'hits', label: '命中', value: `${listCount} 只`, sub: statusText || undefined },
            {
              key: 'scan',
              label: '最后扫描',
              value: lastScanView && scanStatus
                ? lastScanView.text
                : '—',
              sub: lastScanView && scanStatus
                ? `${scanStatus.in_trading_session ? '盘中' : '已收盘'}${lastScanView.stale ? ' · 数据过期' : ''}`
                : undefined,
            },
          ]}
          style={{ marginBottom: 'var(--sr-gap-row)', flexShrink: 0 }}
        />
        {/* 全球核心指数条（T-73）：L0 结论条之下（结论条保持首屏顶部）;
            8 只指数横向条，数据失败静默降级，不阻塞页面 */}
        <IndexStrip />
        {/* 排除指示条：dir 存在排除时显示（N=排除信号数），清空调用 clearDir；桌面/移动共用 */}
        {exclCount > 0 && (
          <div className="sr-inbox-excl-bar">
            <Badge color="red" variant="light" style={{ fontWeight: 500 }}>已排除 {exclCount} 个信号</Badge>
            <Button size="compact-sm" variant="subtle" onClick={clearDir} aria-label="清除全部排除">清空</Button>
          </div>
        )}
      </div>
      {/* 滚动列表体 */}
      <div className="sr-stream-body" ref={tableWrapRef} style={{ flex: 1, minHeight: 0, minWidth: 0, overflowY: 'auto', display: 'flex', flexDirection: 'column' }}>
        {/* 周期扫描计算中提示条：列表上方一行（桌面+移动通用），完成后触发端自动刷新。
            进度 = GET /experiments/{job_id} 轮询百分比（scanProgress，未拿到时确定条 0 + 阶段文案兜底）；
            覆盖口径 = scanCoverageText（scanner 分支语义）；取消 = DELETE /experiments/{job_id} 协作取消 */}
        {scanBusy && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0, padding: '4px var(--sr-pad-lg)' }}>
            <Progress value={scanProgress ?? 0} size={6}
              style={{ width: 96, flexShrink: 0 }} />
            {/* T-129 窄屏裁剪修复:进度文案+覆盖口径合并单行 ellipsis,取消按钮恒可见 */}
            <Text span style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-2)', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              正在计算 {periodLabel(period)} 信号{scanProgress != null ? ` ${Math.round(scanProgress)}%` : ''}… · {scanCoverageText(period, scanUniverse)}
            </Text>
            <Button size="compact-sm" variant="subtle" color="red" onClick={cancelScan} style={{ flexShrink: 0 }}
              aria-label="取消当前扫描">取消</Button>
          </div>
        )}
        {pageError ? (
          <EmptyState text="信号加载失败，请重试" onRetry={reload} />
        ) : isMobile ? (
          pageLoading ? (
            <div className="sr-inbox-mstate">
              <Loader size="sm" />
              <Text span style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-2)' }}>加载信号…</Text>
            </div>
          ) : listCount === 0 ? (
            <EmptyState description={emptyText} />
          ) : (
            <MobileSignalList
              groups={visibleGroups}
              period={period}
              labelOf={labelOf}
              watchedCodes={watchlistCodes}
              onOpen={(code) => navigate(`/stocks/${code}`)}
            />
          )
        ) : (
          // 本表含 expandable 展开行：antd 已知 expandable+virtual 为弱项（展开行渲染异常），
          // 故不开启 virtual 虚拟滚动（DataTable virtual 默认关闭），保持现状
          // T-50：scrollX = max(容器宽, min 列宽和)（数值）——默认不横滚、窄容器兜底横滚；
          // 列宽经 CSS 变量（groupVars）+ nth-child 注入（signalStreamPanel.css）
          <div style={layout.groupVars}>
            <DataTable
              rowKey="code"
              className="sr-stream-main"
              loading={pageLoading}
              columns={streamCols}
              dataSource={visibleGroups}
              pagination={false}
              scrollX={layout.scrollX}
              // 排序作用于已加载集合（M3 §3.1 基板能力）：信号流为无限滚动，全量 unknown，
              // 排序只对 dataSource（visibleGroups=已加载集合，排除浏览层 dir 后）生效；
              // 激活排序时表头上方渲染 L3 提示「排序作用于已加载 N 只」（N 自动取 dataSource.length）
              sortScope="loaded"
              onChange={handleTableChange}
              locale={tableLocale}
              expandable={expandableConfig}
            />
          </div>
        )}

        {/* 无限模式哨兵：首拉失败已由 pageError 错误态呈现，此处不再重复渲染错误哨兵 */}
        {!pageError && (
          <InfiniteScrollSentinel
            hasMore={infHasMore}
            loadMore={loadInfiniteMore}
            enabled
            loading={infLoading}
            error={infError}
            retry={loadInfiniteMore}
            resetKey={infResetKey}
            loadingText={`加载中… 已加载 ${infEvents.length} 条事件`}
            doneText={infEvents.length ? `已加载 ${infEvents.length} / 共 ${infTotal} 条事件` : '暂无数据'}
            loadMoreText={`加载更多 · 已加载 ${infEvents.length} / 共 ${infTotal} 条事件`}
          />
        )}
      </div>

      {/* 筛选抽屉（低频筛选，功能栏第二级）：字段分三组——范围（时间范围/排除今日）· 信号
          （板块/信号/命中语义/排除）· 其他（仅看关注/搜索）；footer 常驻重置 + 生效数（T-27） */}
      <Drawer
        opened={filterOpen}
        onClose={() => setFilterOpen(false)}
        title="筛选"
        position="right"
        size="min(340px, 92vw)"
        withCloseButton
        overlayProps={{ opacity: isMobile ? 0.55 : 0, blur: 0 }}
      >
        <div className="sr-fd">
          {/* ① 范围 */}
          <section className="sr-fd-group" aria-label="范围">
            <div className="sr-fd-group-title" role="heading" aria-level={4}>范围</div>
            <div className="sr-fd-field">
              <Text span style={{ color: 'var(--sr-text-2)' }} className="sr-fd-label" id="sr-fd-label-timerange">时间范围</Text>
              <SegmentedControl size="sm" fullWidth value={timeRange} onChange={(v) => setTimeRange(String(v))}
                data={TIME_RANGE_OPTIONS} aria-labelledby="sr-fd-label-timerange" />
            </div>
            <div className="sr-fd-field">
              <label className="sr-fd-switch">
                <Tooltip label="过滤掉触发日为今天的信号（与时间范围「今日」互斥，切换时自动纠正）">
                  <span id="sr-fd-label-excl-today">排除今日</span>
                </Tooltip>
                <Switch size="sm" checked={excludeToday} onChange={(e) => setExcludeToday(e.currentTarget.checked)} aria-labelledby="sr-fd-label-excl-today" />
              </label>
            </div>
          </section>
          {/* ② 信号 */}
          <section className="sr-fd-group" aria-label="信号">
            <div className="sr-fd-group-title" role="heading" aria-level={4}>信号</div>
            <div className="sr-fd-field">
              <Text span style={{ color: 'var(--sr-text-2)' }} className="sr-fd-label" id="sr-fd-label-sector">板块</Text>
              <MultiSelectGrid placeholder="选择板块" value={sectors} onChange={setSectors} options={sectorOpts} ariaLabelledBy="sr-fd-label-sector" />
            </div>
            <div className="sr-fd-field">
              <Text span style={{ color: 'var(--sr-text-2)' }} className="sr-fd-label" id="sr-fd-label-signal">信号</Text>
              <MultiSelectGrid placeholder="选择信号" value={signalFilter} onChange={setSignalFilter} options={signalOpts} ariaLabelledBy="sr-fd-label-signal" />
            </div>
            {/* 命中语义（任一/全部）：仅多选（signalFilter.length > 1）时可见；单选/未选时值已在
                SignalCenter 自动重置为 any（T-27：不再静默保留，角标与可见生效条件一致） */}
            {signalFilter.length > 1 && (
              <div className="sr-fd-field">
                <Tooltip label="任一：命中其中一个信号即入选；全部：须同时命中所有已选信号">
                  <Text span style={{ color: 'var(--sr-text-2)' }} className="sr-fd-label" id="sr-fd-label-match">命中语义</Text>
                </Tooltip>
                <SegmentedControl size="sm" fullWidth data={MATCH_OPTIONS} value={signalMatch}
                  onChange={(v) => setSignalMatch(String(v) as SignalMatch)} aria-labelledby="sr-fd-label-match" />
              </div>
            )}
            <div className="sr-fd-field">
              <Text span style={{ color: 'var(--sr-text-2)' }} className="sr-fd-label" id="sr-fd-label-exclude">排除</Text>
              <Popover position="bottom">
                <Popover.Target>
                  <Button size="compact-sm" leftSection={<IconPlayerStop size={14} />} className="sr-fd-excl" aria-labelledby="sr-fd-label-exclude">排除信号</Button>
                </Popover.Target>
                <Popover.Dropdown>
                  <ExcludePopover options={signalOpts} dir={dir} setDirOf={setDirOf} />
                </Popover.Dropdown>
              </Popover>
            </div>
          </section>
          {/* ③ 其他 */}
          <section className="sr-fd-group" aria-label="其他">
            <div className="sr-fd-group-title" role="heading" aria-level={4}>其他</div>
            <div className="sr-fd-field">
              <label className="sr-fd-switch">
                <span id="sr-fd-label-wl-only">仅看关注</span>
                <Switch size="sm" checked={watchlistOnly} onChange={(e) => setWatchlistOnly(e.currentTarget.checked)} aria-labelledby="sr-fd-label-wl-only" />
              </label>
            </div>
            <div className="sr-fd-field">
              <Text span style={{ color: 'var(--sr-text-2)' }} className="sr-fd-label" id="sr-fd-label-search">搜索</Text>
              {searchInput}
            </div>
          </section>
          {/* ④ 筛选方案（T-13）：保存/复用完整筛选现场；方案含 dir（浏览层排除）一并应用，
              应用即写回筛选状态 → 触发现有 300ms 防抖重拉链路 */}
          <section className="sr-fd-group" aria-label="筛选方案">
            <div className="sr-fd-group-title" role="heading" aria-level={4}>筛选方案</div>
            {schemes.length === 0 ? (
              <Text span style={{ fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-2)' }}>
                暂无方案，保存当前筛选以便复用
              </Text>
            ) : (
              <div className="sr-fd-field">
                <Select
                  size="sm"
                  placeholder="选择方案应用"
                  data={schemes.map((s) => ({
                    value: s.id,
                    label: s.id === activeSchemeId ? `${s.name} · 已应用` : s.name,
                  }))}
                  value={activeSchemeId}
                  onChange={(v) => { if (v) applyScheme(v) }}
                  aria-label="选择筛选方案"
                />
              </div>
            )}
            <div className="sr-fd-field">
              <Group gap={6} wrap="nowrap">
                <TextInput
                  size="sm"
                  placeholder="方案名"
                  value={schemeName}
                  onChange={(e) => setSchemeName(e.currentTarget.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') saveCurrentScheme() }}
                  aria-label="方案名称"
                  style={{ flex: 1, minWidth: 0 }}
                />
                <Button size="compact-sm" onClick={saveCurrentScheme} disabled={!schemeName.trim()}>保存</Button>
                {activeSchemeId != null && (
                  <Tooltip label="删除当前方案">
                    <Button size="compact-sm" variant="subtle" color="red" px={6}
                      onClick={() => removeScheme(activeSchemeId)} aria-label="删除当前方案">
                      <IconTrash size={14} />
                    </Button>
                  </Tooltip>
                )}
              </Group>
            </div>
          </section>
        </div>
        <div className="sr-fd-footer">
          <Text span style={{ fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-2)' }}>
            {activeFilterCount > 0 ? `已生效 ${activeFilterCount} 项筛选` : '暂无生效筛选'}
          </Text>
          <Button size="compact-sm" onClick={resetAllFilters} disabled={activeFilterCount === 0}>重置全部筛选</Button>
        </div>
      </Drawer>
    </div>
  )
}
