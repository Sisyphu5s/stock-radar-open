import { EmptyState as MantineEmptyState, Loader, Pagination as MantinePagination, Table, TableTbody, TableTd, TableTh, TableThead, TableTr } from '@mantine/core'
import { IconArrowDown, IconArrowsSort, IconArrowUp } from '@tabler/icons-react'
import { useVirtualizer } from '@tanstack/react-virtual'
import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { useTablePagination } from '../../hooks/useTablePagination'
import type { SrPagination } from '../../hooks/useTablePagination'
import HScroll from './HScroll'
import { fromAntdColumns, isSrColumnArray, warnOnce } from './tableTypes'
import type { SrColumn, SrColumnsType, SrExpandableConfig, SrPaginationConfig, SrSorterResult, SrTableCurrentDataSource } from './tableTypes'
// 组件级样式(P2-21 由运行时注入迁入主样式体系):规则见 ../../styles/datatable.css
import '../../styles/datatable.css'

export interface DataTableProps<T> {
  // 声明为 SrColumnsType<T>(antd 兼容结构,tableTypes 本地复刻)而非联合 SrColumnsType<T> | SrColumn<T>[]:
  // TS 6.0.3 对联合上下文的函数字面量参数(render/sorter)推断退化 implicit any,页面(Alpha101 useMemo 上下文)
  // 会 TS7006 致 tsc -b 失败;SrColumn<T> 结构兼容 SrColumnType(字段全可选),单一类型
  // 同时接受 antd 形态列与 SrColumn 列,运行时按字段判别(直通/转换)。
  columns: SrColumnsType<T>
  dataSource: T[]
  rowKey: string | ((r: T) => string)
  loading?: boolean
  pagination?: false | SrPagination | SrPaginationConfig // 联合:页面 antd 特有字段(hideOnSinglePage/size/position 等)也能过类型
  scrollX?: 'max-content' | number | false
  scrollY?: number
  /** M1 宽度撑满(T-60):true 时组件内部 ResizeObserver 实测容器宽 W,
   *  计算 scrollX = max(W, min 列宽和),表格 width 100%(tableLayout 随数值 scrollX 自动 fixed,
   *  复用 T-59 A1 列宽落地),窄容器(min 列宽和放不下)兜底 HScroll。
   *  与显式 scrollX 互斥:fillWidth 优先,同时传入时告警并忽略 scrollX(默认值 max-content 除外)。
   *  默认 false = 旧行为零回归。 */
  fillWidth?: boolean
  /** 满高容器内自动占满剩余高度(T-52):viewport flex:1 + 内部 ResizeObserver 实测可视高自动设 scrollY
   *  (虚拟滚动需数值高度,实测值即其 scrollY);需父链纵向弹性(fill 页 PageShell fill 或等价 flex 链)。
   *  与显式 scrollY 互斥:grow 优先,同时传入时告警并忽略 scrollY。默认 false = 旧行为零回归。 */
  grow?: boolean
  className?: string
  /** 表头吸顶(跟随滚动容器);分页吸底由组件内联实现;默认开启 */
  sticky?: boolean
  /** 分页条容器 ref 出口:调用方需实测分页条高度(虚拟滚动 scrollY 扣除)时使用,
   *  避免 querySelector 依赖 Mantine Pagination 内部类名 */
  paginationRef?: React.RefObject<HTMLDivElement | null>
  /** 虚拟滚动(需配合 scrollY 数值高度);默认关闭 */
  virtual?: boolean
  /** 虚拟行高估算(px):首帧/未实测行的回退高度,按行内容实际高度传入(默认 40)。
   *  dataSource 变化时基板自动强制重测(measure()),估算值贴近真实即可不须精确;
   *  多行内容表(关注页)传实际高度,消除估算错位导致的行重叠/裁切。 */
  virtualRowEstimate?: number
  emptyText?: ReactNode
  expandable?: SrExpandableConfig<T> // 兼容结构(页面传 antd expandable 形态),运行时仅 expandedRowRender/rowExpandable 生效
  /** 排序作用域明示(M3 §3.1 基板能力):声明排序作用于全量/已加载集合/当前页;
   *  排序激活且作用域非 all 时,表头上方渲染 L3 提示("排序作用于已加载 N 只"/"排序作用于当前页"),
   *  N 取 dataSource.length(已加载集合大小),无需调用方另传。undefined=不提示,行为与旧版完全一致。
   *  消费示例:信号中心(无限滚动)= 'loaded' → "排序作用于已加载 N 只";
   *  任务中心(分页)= 'page' → "排序作用于当前页";关注页(虚拟全量)= 'all' → 全量排序,不提示。 */
  sortScope?: 'all' | 'loaded' | 'page'
  /** sortScope 提示的量词(默认「只」,股票场景;任务/订单等列表传「条」) */
  sortScopeUnit?: string
  /** 透传 antd Table 的 onChange(分页/排序统一回调;薄壳按 antd 兼容签名构造) */
  onChange?: (pagination: SrPaginationConfig, filters: Record<string, unknown>, sorter: SrSorterResult<T>, extra: SrTableCurrentDataSource<T>) => void
  onRow?: (r: T, index?: number) => React.HTMLAttributes<HTMLTableRowElement>
  rowClassName?: (r: T, index?: number) => string
  locale?: Record<string, unknown> // locale.emptyText 为 ReactNode,优先于 emptyText
}

/** 展开箭头列宽(px):fixed 列偏移占位与列宽计算共用 */
const ARROW_COL_WIDTH = 32
/** 虚拟行高估算(px):与现状 antd small 表行高接近,挂载后 measureElement 实测校正 */
const ROW_ESTIMATE = 40
/** fillWidth 首帧兜底(px,M1):容器实测宽 ≤0(挂载首帧/临时隐藏)且无上次有效宽时使用。
 *  取中等宽(与既有 DEFAULT_TOTAL=1000 语义一致)而非 320 之类小值,避免把宽屏表格钉成窄块。 */
const FILL_FALLBACK_W = 1000

/** 列 key 提取(key 优先,dataIndex 兜底):与 onChange sorter.columnKey 语义一致 */
const colKey = (c: { key?: unknown; dataIndex?: unknown }): string =>
  c.key != null ? String(c.key) : c.dataIndex != null ? String(c.dataIndex) : ''

/** 列对齐 → flex 主轴方向(表头标题 + 排序指示器容器) */
const alignToFlex = (a: SrColumn<never>['align']): CSSProperties['justifyContent'] =>
  a === 'right' ? 'flex-end' : a === 'center' ? 'center' : 'flex-start'

/** 排序指示器(Tabler 图标,替代原 Unicode ▲/▼/↕):激活 ▲/▼ accent、非激活 ↕ text-3,16px */
function SortIndicator({ order, active }: { order: 'ascend' | 'descend' | null; active: boolean }) {
  const Icon = order === 'ascend' ? IconArrowUp : order === 'descend' ? IconArrowDown : IconArrowsSort
  return (
    <span
      style={{
        display: 'inline-flex', lineHeight: 1, flexShrink: 0,
        color: active ? 'var(--sr-accent)' : 'var(--sr-text-3)',
      }}
    >
      <Icon size={16} stroke={2} />
    </span>
  )
}

/**
 * 统一数据表格(T-38 Mantine 薄壳):antd 列定义自动转中性契约,内部渲染 Mantine Table +
 * 客户端排序 + 分页(吸底)+ 斑马纹 + 横滚 HScroll + 表头吸顶 + 展开行自实现 +
 * @tanstack/react-virtual 虚拟滚动。旧 props 契约保持(页面零改动)。
 * memo:调用方保证 columns/dataSource 等 props 引用稳定(useMemo)时,可跳过无关 setState 引发的全表重渲染。
 */
function DataTable<T extends object>({
  columns, dataSource, rowKey, loading, pagination, scrollX = 'max-content', scrollY, fillWidth = false, grow = false, className, sticky = true,
  virtual = false, virtualRowEstimate = ROW_ESTIMATE, emptyText = '暂无数据', expandable, sortScope, sortScopeUnit = '只', onChange, onRow, rowClassName, locale, paginationRef,
}: DataTableProps<T>) {
  // ===== A. 列转换:SrColumn 形态直通,antd 形态列自动转换 =====
  const cols = useMemo<SrColumn<T>[]>(
    () => (isSrColumnArray(columns) ? columns : fromAntdColumns(columns)),
    [columns],
  )

  // ===== J. 表头吸顶:外层 sr-sticky-tools(Alpha101 吸顶工具条)存在时关闭,镜像 layout.css 行为 =====
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const [insideStickyTools, setInsideStickyTools] = useState(false)
  useLayoutEffect(() => {
    setInsideStickyTools(!!wrapRef.current?.closest('.sr-sticky-tools'))
  }, [])
  const stickyHeader = sticky && !insideStickyTools

  // 虚拟滚动滚动容器 ref(viewport;grow 实测高度也观察它,故提前声明)
  const viewportRef = useRef<HTMLDivElement | null>(null)

  // ===== M. grow 满高模式(T-52):viewport flex:1 + ResizeObserver 实测可视高 → 自动 scrollY =====
  // 分页条/排序提示为 wrap 内兄弟节点,flex 布局自然占位,实测 clientHeight 即剩余可用高
  // (Tasks 页原手工测量逻辑已泛化入此;父链需纵向弹性,否则 flex:1 无生效空间)。
  const [growH, setGrowH] = useState(0)
  useEffect(() => {
    if (!grow) return
    const el = viewportRef.current
    if (!el) return
    const measure = () => {
      const h = Math.round(el.clientHeight)
      setGrowH((prev) => (prev === h ? prev : h))
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [grow])
  useEffect(() => {
    if (grow && scrollY != null) {
      warnOnce('grow-scrollY-conflict', 'grow 与显式 scrollY 同时传入:以 grow 实测高度为准,显式 scrollY 已忽略')
    }
  }, [grow, scrollY])

  // ===== N. fillWidth 宽度撑满(M1,T-60):wrap 容器实测宽 → effScrollX = max(W, min 列宽和) =====
  // 复用 grow 同一 ResizeObserver 测量链路(内部手写 RO,非 useElementSize——wrapRef 已存在
  // 且需在尺寸变化时即时落 state,不引入第二份测量);lastGoodWRef 模式:实测 ≤0(首帧/临时
  // 隐藏)延用上次有效宽,首个有效值前退 FILL_FALLBACK_W,禁止把首帧 0 钳到 320 之类小值。
  const [fillW, setFillW] = useState(0)
  useEffect(() => {
    if (!fillWidth) return
    const el = wrapRef.current
    if (!el) return
    const measure = () => {
      const w = Math.round(el.clientWidth)
      setFillW((prev) => (prev === w ? prev : w))
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [fillWidth])
  useEffect(() => {
    if (fillWidth && scrollX !== 'max-content') {
      warnOnce('fillWidth-scrollX-conflict', 'fillWidth 与显式 scrollX 同时传入:以 fillWidth 实测宽为准,显式 scrollX 已忽略')
    }
  }, [fillWidth, scrollX])

  // ===== K. 虚拟滚动开关:virtual 需配合 scrollY(数值高度),否则降级关闭 =====
  // grow 模式下高度由内部实测 growH 供给,等效于调用方传 scrollY
  const effScrollY = grow ? growH : scrollY
  const virtualEnabled = virtual && effScrollY != null && effScrollY > 0
  useEffect(() => {
    if (virtual && !virtualEnabled) {
      warnOnce('virtual-needs-scrollY', 'virtual 需配合 scrollY(数值高度)才生效,已关闭虚拟滚动')
    }
  }, [virtual, virtualEnabled])

  // ===== H. 展开行(自实现;仅支持 expandedRowRender/rowExpandable 两字段) =====
  const { expandedRowRender, rowExpandable } = expandable ?? {}
  useEffect(() => {
    if (!expandable) return
    const unsupported = Object.keys(expandable).filter((k) => k !== 'expandedRowRender' && k !== 'rowExpandable')
    for (const k of unsupported) warnOnce(`expandable-${k}`, `expandable.${k} 暂不支持,已忽略`)
  }, [expandable])
  useEffect(() => {
    if (expandedRowRender != null && virtual) {
      warnOnce('expandable-virtual', 'expandable 与 virtual 组合不支持,展开行被忽略')
    }
  }, [expandedRowRender, virtual])
  const expandableActive = expandedRowRender != null && !virtualEnabled
  const [expandedKeys, setExpandedKeys] = useState<ReadonlySet<string>>(new Set())

// effScrollX 实际值(M1,T-60):fillWidth 时 = max(容器实测宽 W, min 列宽和);
  // min 列宽和 = 数值列宽求和 + minWidth 求和取 max(数值列宽与 minWidth 同列取大者)
  // + 展开箭头列(字符串/百分比宽无法求和,不参与,窄容器兜底由 fixed 布局自动均分吸收,语义与 T-59 A1 一致)。
  // P1-78:minWidth 参与计算并落地到 th/td(fixed 布局下无显式宽的 minWidth 列不再被均分挤压,
  // 因子库/Alpha101 等窄表操作按钮/长文本列不被压成窄块)。
  const lastGoodWRef = useRef(0)
  const effScrollX: 'max-content' | number | false = useMemo(() => {
    if (!fillWidth) return scrollX
    if (fillW > 0) lastGoodWRef.current = fillW
    const W = lastGoodWRef.current > 0 ? lastGoodWRef.current : FILL_FALLBACK_W
    const minTotal = cols.reduce((s, c) => {
      const w = typeof c.width === 'number' ? c.width : 0
      const mw = typeof c.minWidth === 'number' ? c.minWidth : 0
      return s + Math.max(w, mw)
    }, 0)
      + (expandableActive ? ARROW_COL_WIDTH : 0)
    return Math.max(W, minTotal)
  }, [fillWidth, fillW, cols, expandableActive, scrollX])
  const toggleExpand = useCallback((key: string) => {
    setExpandedKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  // ===== E. 排序(手写在薄壳内) =====
  const [innerSort, setInnerSort] = useState<{ colKey: string; order: 'ascend' | 'descend' } | null>(null)
  // 受控检测:任意列 sortOrder 非 null → 受控模式(以该列为当前态,点击仍走 onChange 通知)
  const controlledSort = useMemo(() => {
    for (const c of cols) {
      if (c.sortOrder != null) return { colKey: colKey(c), order: c.sortOrder }
    }
    return null
  }, [cols])
  const sortState = controlledSort ?? innerSort

  // 排序(先排后切,与 antd 一致):sorter===true(服务端)不排数据,仅指示器
  const sorted = useMemo(() => {
    if (!sortState) return dataSource
    const c = cols.find((col) => colKey(col) === sortState.colKey)
    const fn = c?.sorter
    if (typeof fn !== 'function') return dataSource
    const dir = sortState.order === 'ascend' ? 1 : -1
    const arr = [...dataSource]
    arr.sort((a, b) => dir * fn(a, b))
    return arr
  }, [dataSource, cols, sortState])

  // ===== L. 排序作用范围明示(M3 §3.1):排序激活且 sortScope 非 all 时渲染 L3 提示;
  //  受控与非受控排序统一走 sortState(激活即提示),undefined/all 一律不提示 =====
  const sortScopeHint = (() => {
    if (sortScope == null || sortScope === 'all' || sortState == null) return null
    return sortScope === 'loaded' ? `排序作用于已加载 ${dataSource.length} ${sortScopeUnit}` : '排序作用于当前页'
  })()

  // ===== F. 分页(useTablePagination 归一化;内部渲染 Mantine Pagination) =====
  const { pagination: p } = useTablePagination({
    pagination: pagination === false || pagination == null ? pagination : (pagination as SrPagination),
  })
  const [innerPage, setInnerPage] = useState(1)
  const pageSize = p === false ? 10 : (p.pageSize ?? 10)
  // 受控 total（调用方传服务端/全量总数，如任务中心分页）优先于当前 dataSource 长度：
  // 仅凭 sorted.length 会把「每页只拉当前 20 条」的多页数据压成 1 页（桌面分页失效根因）
  const pageTotal = p !== false && p.total != null ? p.total : sorted.length
  const totalPages = Math.max(1, Math.ceil(pageTotal / pageSize))
  const current = p !== false && p.current != null ? p.current : innerPage
  const safeCurrent = Math.min(Math.max(1, current), totalPages)
  const rows = p === false ? sorted : sorted.slice((safeCurrent - 1) * pageSize, safeCurrent * pageSize)

  // ===== I. onChange 兼容(antd 签名,结构类型本地化) =====
  const currentSorter = sortState
    ? { columnKey: sortState.colKey, field: sortState.colKey, order: sortState.order }
    : { columnKey: undefined, field: undefined, order: null as 'ascend' | 'descend' | null }
  const emitChange = useCallback((
    page: number, size: number,
    sorter: { columnKey?: string; field?: string; order: 'ascend' | 'descend' | null },
  ) => {
    if (!onChange) return
    onChange(
      { current: page, pageSize: size, total: pageTotal } as SrPaginationConfig,
      {},
      { columnKey: sorter.columnKey, field: sorter.field, order: sorter.order } as SrSorterResult<T>,
      { currentDataSource: sorted } as SrTableCurrentDataSource<T>,
    )
  }, [onChange, sorted, pageTotal])

  // 表头点击:ascend → descend → null(取消);受控时不动内部 state
  const onHeaderClick = (c: SrColumn<T>) => {
    if (!c.sorter) return
    const key = colKey(c)
    const currentOrder = sortState && sortState.colKey === key ? sortState.order : null
    const next = currentOrder == null
      ? { colKey: key, order: 'ascend' as const }
      : currentOrder === 'ascend'
        ? { colKey: key, order: 'descend' as const }
        : null
    // 无条件同步内部 state:受控模式下被 controlledSort 覆盖(无副作用);取消(next=null)时
    // 受控页面 setSort(null) 后 controlledSort 归零,若内部残留旧值会导致排序无法取消(边界 bug)。
    setInnerSort(next)
    const sorter = next
      ? { columnKey: next.colKey, field: next.colKey, order: next.order }
      : { columnKey: key, field: key, order: null as 'ascend' | 'descend' | null }
    emitChange(safeCurrent, pageSize, sorter)
  }

  const onPageChange = (page: number) => {
    if (p !== false) {
      if (p.current == null) setInnerPage(page) // 非受控:内部状态;受控:只通知页面
      p.onChange?.(page, pageSize)
      emitChange(page, pageSize, currentSorter)
    }
  }

  // ===== D. fixed 列布局(非 virtual):sticky 偏移按列宽累加;字符串宽之后的 fixed 列降级丢弃 =====
  const fixedInfo = useMemo(() => {
    const info: Array<{ left?: number; right?: number }> = cols.map(() => ({}))
    if (cols.every((c) => c.fixed == null)) return info
    if (virtualEnabled) {
      warnOnce('fixed-virtual', 'fixed 列在虚拟滚动下不支持,已忽略')
      return info
    }
    let acc = expandableActive ? ARROW_COL_WIDTH : 0
    let broken = false
    for (let i = 0; i < cols.length; i++) {
      const c = cols[i]
      if (c.fixed === 'left') {
        if (!broken) {
          info[i].left = acc
          if (typeof c.width === 'number') acc += c.width
          else {
            broken = true
            warnOnce('fixed-left-unknown', 'fixed 列存在字符串/缺省宽度,偏移无法精确累加,其后 fixed 列已降级丢弃')
          }
        }
      } else if (!broken && typeof c.width === 'number') {
        acc += c.width
      } else if (!broken && c.width != null) {
        broken = true // 字符串宽(如 'max-content')之后的 fixed 列偏移无法计算
      }
    }
    let accR = 0
    let brokenR = false
    for (let i = cols.length - 1; i >= 0; i--) {
      const c = cols[i]
      if (c.fixed === 'right') {
        if (!brokenR) {
          info[i].right = accR
          if (typeof c.width === 'number') accR += c.width
          else {
            brokenR = true
            warnOnce('fixed-right-unknown', 'fixed 列存在字符串/缺省宽度,偏移无法精确累加,其后 fixed 列已降级丢弃')
          }
        }
      } else if (!brokenR && typeof c.width === 'number') {
        accR += c.width
      } else if (!brokenR && c.width != null) {
        brokenR = true
      }
    }
    return info
  }, [cols, expandableActive, virtualEnabled])

  // A1:fixed 布局(虚拟滚动或数值 scrollX)判定,与 tableStyle 的 tableLayout:'fixed' 共用
  // 单一条件,避免两处漂移;max-content 布局不落列宽,保持内容自适应(不截断)
  const fixedLayout = virtualEnabled || typeof effScrollX === 'number'

  /** th/td 行内样式:align + 列宽(fixed 布局下) + fixed(sticky/偏移/zIndex/背景) */
  const cellStyle = (c: SrColumn<T>, i: number, isTh: boolean): CSSProperties => {
    const s: CSSProperties = {}
    if (c.align) s.textAlign = c.align
    // A1:fixed 布局(虚拟滚动/数值 scrollX)下列宽必须落到 th/td 内联样式,否则 CSS
    // table-layout:fixed 下无显式宽的列均分剩余空间 → 40px 勾选列与 300px 摘要列同宽,
    // 表格"挤成一团"。数值 px 直接落;百分比字符串(含 %)也落(原生 fixed 布局支持);
    // 仅 minWidth 无 width 的列落 minWidth(防 min 列被均分压扁,窄容器由 HScroll 兜底总宽);
    // 其余字符串宽('max-content' 等)不落,避免 fixed 布局下语义失效。
    if (fixedLayout) {
      const w = c.width
      const mw = c.minWidth
      if (typeof w === 'number') s.width = w
      else if (typeof mw === 'number') s.width = mw
      else if (typeof w === 'string' && w.endsWith('%')) s.width = w
    }
    const fx = fixedInfo[i]
    if (fx?.left != null) {
      s.position = 'sticky'
      s.left = fx.left
      s.zIndex = isTh ? 3 : 1
      s.background = 'var(--sr-card-bg)'
    } else if (fx?.right != null) {
      s.position = 'sticky'
      s.right = fx.right
      s.zIndex = isTh ? 3 : 1
      s.background = 'var(--sr-card-bg)'
    }
    return s
  }

  const thKey = (c: SrColumn<T>, i: number) => colKey(c) || `col-${i}`

  const renderTh = (c: SrColumn<T>, i: number) => {
    const sortable = !!c.sorter
    const active = sortable && sortState != null && sortState.colKey === colKey(c)
    const cls = 'sr-dt-th'
      + (sortable ? ' sr-dt-sortable' : '')
      + (c.className ? ` ${c.className}` : '')
    return (
      <TableTh
        key={thKey(c, i)}
        className={cls}
        style={{
          ...cellStyle(c, i, true),
          ...(active ? { color: 'var(--sr-accent)' } : null),
        }}
        onClick={sortable ? () => onHeaderClick(c) : undefined}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: alignToFlex(c.align), gap: 4 }}>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.title}</span>
          {sortable && <SortIndicator order={active ? sortState!.order : null} active={active} />}
        </div>
      </TableTh>
    )
  }

  const renderTd = (c: SrColumn<T>, row: T, index: number, i: number) => {
    const raw = c.dataIndex != null ? (row as Record<string, unknown>)[String(c.dataIndex)] : undefined
    const node = c.render ? c.render(raw, row, index) : (raw as ReactNode)
    // T-51 激活排序列单元格着色:整列 td 加 sr-dt-sorted(样式见 datatable.css,accent 8% 强调底)
    const sorted = sortState != null && sortState.colKey === colKey(c)
    const cls = 'sr-dt-td'
      + (c.ellipsis ? ' sr-dt-ellipsis' : '')
      + (sorted ? ' sr-dt-sorted' : '')
      + (c.className ? ` ${c.className}` : '')
    return (
      <TableTd key={thKey(c, i)} className={cls} style={cellStyle(c, i, false)}>
        {node}
      </TableTd>
    )
  }

  const renderArrowTd = (key: string, row: T) => {
    const canExpand = rowExpandable?.(row) !== false
    const expanded = expandedKeys.has(key)
    return (
      <TableTd key="sr-expand" className="sr-dt-td sr-dt-expand-arrow" style={{ width: ARROW_COL_WIDTH, textAlign: 'center' }}>
        {canExpand ? (
          <button
            type="button"
            aria-label={expanded ? '收起' : '展开'}
            onClick={() => toggleExpand(key)}
            style={{
              background: 'none', border: 'none', cursor: 'pointer', padding: 0,
              color: 'var(--sr-text-2)', fontSize: 12, lineHeight: 1,
            }}
          >
            {expanded ? '▾' : '▸'}
          </button>
        ) : null}
      </TableTd>
    )
  }

  // ===== G. 空态 / 加载 =====
  const colSpan = cols.length + (expandableActive ? 1 : 0)
  const emptyNode = locale?.emptyText as ReactNode | undefined
  const emptyRow = rows.length === 0 ? (
    loading ? (
      <TableTr>
        <TableTd colSpan={colSpan} style={{ textAlign: 'center', padding: '24px 0' }}>
          <Loader size="sm" />
        </TableTd>
      </TableTr>
    ) : (
      <TableTr>
        <TableTd colSpan={colSpan} style={{ textAlign: 'center' }}>
          {emptyNode ?? <MantineEmptyState size="sm" title={emptyText} />}
        </TableTd>
      </TableTr>
    )
  ) : null

  // ===== 行 key(统一 String 化) =====
  const rk = useCallback(
    (r: T): string => (typeof rowKey === 'string' ? String((r as Record<string, unknown>)[rowKey]) : String(rowKey(r))),
    [rowKey],
  )

  const rowPropsOf = (r: T, index: number) => {
    const extra = rowClassName?.(r, index) ?? ''
    const props = onRow?.(r, index) ?? {}
    return {
      extra,
      props,
      zebra: index % 2 === 1 ? ' sr-dt-zebra' : '',
    }
  }

  // ===== K. 虚拟滚动(@tanstack/react-virtual) =====
  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => viewportRef.current,
    estimateSize: () => virtualRowEstimate,
    overscan: 8,
  })

  // 虚拟行动态高度机制:行内容(换行 Tag/多行单元格)高度随数据变化时,仅靠首帧
  // measureElement + RO 实测会留下陈旧尺寸 → 行与行重叠/内容裁切。数据变化(异步
  // 到达/排序/筛选/无限滚动追加)后强制 measure():清 itemSizeCache 回退 estimateSize,
  // 后续 RO 按真实内容精确校正——估算值贴近真实(virtualRowEstimate)可保回退帧不错位。
  useEffect(() => {
    if (!virtualEnabled) return
    rowVirtualizer.measure()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, virtualEnabled])

  // 固定可视高语义(antd scroll.y 等价):行少时容器也撑满,空白由容器背景承担——
  // maxHeight 会让行少的表格塌缩,页面底部露出空档(P1 列表未填满根因之一);
  // scrollY 缺省(非固定高表格)时 height 为 undefined,容器仍按内容自适应。
  // grow 模式:flex:1 + min-height:0 撑满父链,高度由 flex 布局决定(显式 scrollY 被忽略),
  // RO 实测 clientHeight 回填 scrollY 供虚拟滚动使用
  const viewportStyle: CSSProperties = {
    overflow: 'auto',
    position: 'relative',
    ...(grow ? { flex: 1, minHeight: 0 } : { height: scrollY }),
  }

  // ===== B. 表格渲染结构 =====
  const tableWidth = effScrollX === 'max-content' ? 'max-content' : effScrollX === false ? '100%' : effScrollX
  const tableStyle: CSSProperties = {
    width: tableWidth,
    ...(fixedLayout ? { tableLayout: 'fixed' as const } : null),
  }

  const thead = (
    <TableThead>
      <TableTr>
        {expandableActive && (
          <TableTh key="sr-expand" className="sr-dt-th" style={{ width: ARROW_COL_WIDTH, textAlign: 'center' }} />
        )}
        {cols.map((c, i) => renderTh(c, i))}
      </TableTr>
    </TableThead>
  )

  // virtual 行(absolute + translateY):包含块 = 行区 div .sr-dt-vbody(纯 div,
  // 所有引擎可靠)——见 B2 双表结构说明;数据变化后的强制 measure 由上方 effect 承担
  const virtualRows = virtualEnabled
    ? rowVirtualizer.getVirtualItems().map((v) => {
        const row = rows[v.index]
        const key = rk(row)
        const { extra, props, zebra } = rowPropsOf(row, v.index)
        return (
          <TableTr
            key={key}
            {...props}
            data-index={v.index}
            ref={rowVirtualizer.measureElement}
            className={'sr-dt-tr' + zebra + (extra ? ` ${extra}` : '') + (props.className ? ` ${props.className}` : '')}
            style={{ position: 'absolute', top: 0, left: 0, width: '100%', height: v.size, transform: `translateY(${v.start}px)`, ...props.style }}
          >
            {cols.map((c, ci) => renderTd(c, row, v.index, ci))}
          </TableTr>
        )
      })
    : null

  const tbody = (
    <TableTbody>
      {rows.map((row, i) => {
        const key = rk(row)
        const { extra, props, zebra } = rowPropsOf(row, i)
        const canExpand = rowExpandable?.(row) !== false
        const expanded = expandableActive && expandedKeys.has(key)
        return (
          <Fragment key={key}>
            <TableTr
              {...props}
              className={'sr-dt-tr' + zebra + (extra ? ` ${extra}` : '') + (props.className ? ` ${props.className}` : '')}
              style={props.style}
            >
              {expandableActive && renderArrowTd(key, row)}
              {cols.map((c, ci) => renderTd(c, row, i, ci))}
            </TableTr>
            {expandableActive && expanded && canExpand && expandedRowRender != null && (
              <TableTr className="sr-dt-tr sr-dt-expanded-row">
                <TableTd colSpan={colSpan}>{expandedRowRender(row, i, 0, true)}</TableTd>
              </TableTr>
            )}
          </Fragment>
        )
      })}
      {emptyRow}
    </TableTbody>
  )

  const table = (
    <Table
      className="sr-dt-table"
      style={tableStyle}
      stickyHeader={stickyHeader}
      stickyHeaderOffset={0}
      horizontalSpacing={8}
      verticalSpacing={4}
      withRowBorders
    >
      {thead}
      {tbody}
    </Table>
  )

  // ===== B2. virtual 双表结构(T-118):表头/行区拆分,行锚定纯 div =====
  // 单表结构下 virtual 行 absolute 锚定 tbody(position:relative)——CSS 规范对
  // table-*-group 的 position 行为未定义,WebKit/Safari 忽略(position 计算为 static),
  // 行包含块回退到内层 wrapper div,top:0 = 表头顶部 → 首行整排压到表头上(关注页/任务页
  // 表头与列表重叠,3b2ffb9 tbody 锚定法在 Safari 依旧复现)。拆为双表:
  //   .sr-dt-vhead:表头独立表格(sticky top:0 + 背景遮住滚过的行;stickyHeader=false 时
  //     转 static,表头随滚动消失,保持旧行为)
  //   .sr-dt-vbody:position:relative 纯 div(所有引擎可靠)承载行区,高度=totalSize
  // 两表共用同一列宽数组(数值 px + fixed 布局),横向滚动天然同步;总滚动高 = 表头 + 行区。
  const virtualBlock = virtualEnabled ? (
    <div style={{ minWidth: tableWidth }}>
      <div className="sr-dt-vhead" style={{ position: stickyHeader ? 'sticky' : 'static', top: 0, zIndex: 2 }}>
        <Table
          className="sr-dt-table"
          style={tableStyle}
          horizontalSpacing={8}
          verticalSpacing={4}
        >
          {thead}
        </Table>
      </div>
      <div className="sr-dt-vbody" style={{ position: 'relative', height: rowVirtualizer.getTotalSize() }}>
        <Table
          className="sr-dt-table"
          style={tableStyle}
          horizontalSpacing={8}
          verticalSpacing={4}
          withRowBorders
        >
          <TableTbody>
            {virtualRows}
            {emptyRow}
          </TableTbody>
        </Table>
      </div>
    </div>
  ) : null

  const viewport = virtualEnabled ? (
    <div className="sr-dt-viewport" ref={viewportRef} style={viewportStyle}>
      {virtualBlock}
    </div>
  ) : (
    <div className="sr-dt-viewport" ref={viewportRef} style={viewportStyle}>
      {table}
    </div>
  )

  const hidePag = p === false || pageTotal === 0 || (p.hideOnSinglePage && totalPages <= 1)
  const paginationBar = p !== false && !hidePag ? (
    <div
      ref={paginationRef}
      style={{
        position: 'sticky', bottom: 0, zIndex: 2, background: 'var(--sr-card-bg)',
        borderTop: '1px solid var(--sr-border)', padding: '8px 4px',
        display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 8,
      }}
    >
      {p.showTotal ? (
        <span style={{ marginRight: 'auto', fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-2)' }}>
          {p.showTotal(pageTotal, [(safeCurrent - 1) * pageSize + 1, Math.min(safeCurrent * pageSize, pageTotal)])}
        </span>
      ) : null}
      <MantinePagination total={totalPages} value={safeCurrent} onChange={onPageChange} size="sm" />
    </div>
  ) : null

  const wrapClass = 'sr-table-scroll' + (className ? ` ${className}` : '') + (grow ? ' sr-dt-grow' : '')
  const sortScopeHintNode = sortScopeHint != null ? (
    // L3 上下文(11px --sr-font-xs + --sr-text-3):nowrap + 省略兜底,窄屏不换行不溢出
    <div
      style={{
        fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-3)',
        padding: '4px 8px', whiteSpace: 'nowrap', overflow: 'hidden',
        textOverflow: 'ellipsis', maxWidth: '100%',
      }}
    >
      {sortScopeHint}
    </div>
  ) : null
  return (
    // data-testid="dt"(T-78 G7 运行时断言锚点:无 HScroll 时断言 table 宽 ≥ 容器宽)
    <div className={wrapClass} ref={wrapRef} data-testid="dt">
      {sortScopeHintNode}
      {effScrollX === false ? (
        <>
          {viewport}
          {paginationBar}
        </>
      ) : (
        <>
          {/* HScroll:横滚左右按钮(悬浮显现),↔ 提示文字保留并存 */}
          <HScroll>{viewport}</HScroll>
          {paginationBar}
          <span className="sr-scroll-x-hint-text">↔</span>
        </>
      )}
    </div>
  )
}

// memo 浅比较全部 props:columns/dataSource/分页等引用稳定时跳过重渲染;函数类 props(onRow 等)引用变化仍会正常重渲染
export default memo(DataTable) as typeof DataTable
