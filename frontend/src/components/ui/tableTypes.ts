import type { CSSProperties, MouseEvent, ReactNode } from 'react'

/**
 * 表格列契约与 antd 兼容期结构类型(T-45)。
 * ------------------------------------------------------------
 * antd 已从 src/ 代码引用中完全退出(仅作为 @ant-design/x 的 peer 依赖保留在 package.json,
 * 见 T-45 报告)。本节类型是 antd/rc-table 列结构接口的**本地复刻**,只保留 DataTable 薄壳
 * 与页面调用方之间契约所需形状:
 * - 页面仍可传 antd 形态的列定义(ColumnsType 结构)经 fromAntdColumns 转换;
 * - SrColumn 为中性契约(页面直通形态,不经转换)。
 * 兼容期说明:若未来页面全部切换 SrColumn,本节 SrColumnType/… 可随 antd peer 一并删除。
 */

export type SrSortOrder = 'ascend' | 'descend' | null
export type SrFixedType = 'start' | 'end' | 'left' | 'right' | boolean
export type SrAlignType = 'start' | 'end' | 'left' | 'right' | 'center' | 'justify' | 'match-parent'
export type SrCompareFn<T> = (a: T, b: T, sortOrder?: SrSortOrder) => number
/** React.Key 本地别名(antd Key 含 bigint,结构兼容需要) */
export type SrKey = string | number | bigint

export interface SrColumnSorter<T> {
  compare?: SrCompareFn<T>
  multiple?: number
}

export interface SrColumnFilterItem {
  text: ReactNode
  value: SrKey | boolean
  children?: SrColumnFilterItem[]
}

// 函数 title 形态参数用 any:antd ColumnTitleProps 的 sortColumn 递归引用 antd 列类型,结构
// 赋值下与本地 SrColumnType 双向逆变无法满足(递归互引用)。any 断开递归,函数 title 在
// fromAntdColumns 中被丢弃,本地只需保留"函数形态存在"的事实,不校验其参数结构。
export type SrColumnTitle = ReactNode | ((props: any) => ReactNode)

/** rc-table RenderedCell 本地复刻(render 返回值形态之一,兼容 antd 列直接赋值) */
export interface SrRenderedCell {
  props?: {
    key?: SrKey
    className?: string
    style?: CSSProperties
    children?: ReactNode
    colSpan?: number
    rowSpan?: number
  }
  children?: ReactNode
}

/** 中性列契约(T-38):页面可直传 SrColumn,或传 antd 列经 fromAntdColumns 自动转换 */
export interface SrColumn<T> {
  key?: string
  dataIndex?: keyof T | string
  title?: ReactNode
  // value 用 any 而非 unknown:TS 6.0.3 对 ColumnsType(value: any) 与 SrColumn(value: unknown)
  // 联合上下文推断 render 箭头函数参数时交集退化 implicit any(页面 TS7006 → tsc -b 失败);
  // any 与 antd 原语义一致且同为"值类型未知",调用侧传值无差。
  render?: (value: any, row: T, index: number) => ReactNode
  width?: number | string
  minWidth?: number
  align?: 'left' | 'right' | 'center'
  fixed?: 'left' | 'right'
  ellipsis?: boolean
  sorter?: boolean | ((a: T, b: T) => number) // 扩展:函数 sorter 保留精确语义(antd 客户端排序)
  sortOrder?: SrSortOrder // 扩展:受控排序(antd sortOrder 兼容)
  className?: string // 扩展:列样式挂载点(页面传 sr-num-col 等)
}

/** antd 特有列字段(仅存在于 antd 形态列,不存在于 SrColumn):用于形态判别与降级检测 */
export const ANTD_ONLY_COL_FIELDS = [
  'onHeaderCell', 'onCell', 'onCellClick',
  'filters', 'filterDropdown', 'filteredValue', 'filterMode', 'filterSearch',
  'onFilter', 'onFilterDropdownOpenChange',
  'filterMultiple', 'filterIcon', 'filterDropdownProps', 'filterOnClose', 'filterResetToDefaultFilteredValue',
  'children', 'responsive', 'hidden',
  'sortDirections', 'showSorterTooltip', 'defaultSortOrder',
  'defaultFilteredValue',
] as const

/** antd 形态列类型(本地复刻,全可选;字段与 antd ColumnType 结构对齐,页面 antd 列定义可整体赋值) */
export interface SrColumnType<T> {
  title?: SrColumnTitle
  key?: SrKey
  className?: string
  hidden?: boolean
  fixed?: SrFixedType
  ellipsis?: boolean | { showTitle?: boolean }
  align?: SrAlignType
  colSpan?: number
  rowSpan?: number
  // dataIndex/onCell/onHeaderCell 用 any:antd 对应类型(DataIndex 的 DeepNamePath 递归、
  // GetComponentProps 引用自身列类型)结构无法双向复刻,且这些字段在 fromAntdColumns
  // 中全部被丢弃(不参与渲染),本地只需保留"字段存在"的事实以兼容页面 antd 列赋值。
  dataIndex?: any
  render?: (value: any, record: T, index: number) => ReactNode | SrRenderedCell
  width?: number | string
  minWidth?: number
  onCell?: (data: any, index?: number) => React.HTMLAttributes<unknown> & React.TdHTMLAttributes<unknown>
  onCellClick?: (record: T, e: MouseEvent<HTMLElement>) => void
  onHeaderCell?: (data: any, index?: number) => React.HTMLAttributes<unknown> & React.TdHTMLAttributes<unknown>
  sorter?: boolean | SrCompareFn<T> | SrColumnSorter<T>
  sortOrder?: SrSortOrder
  defaultSortOrder?: SrSortOrder
  sortDirections?: SrSortOrder[]
  showSorterTooltip?: boolean | object
  filtered?: boolean
  filters?: SrColumnFilterItem[]
  filterDropdown?: ReactNode | ((props: any) => ReactNode)
  filterOnClose?: boolean
  filterMultiple?: boolean
  filteredValue?: (SrKey | boolean)[] | null
  defaultFilteredValue?: (SrKey | boolean)[] | null
  filterIcon?: ReactNode | ((filtered: boolean) => ReactNode)
  filterMode?: 'menu' | 'tree'
  filterSearch?: boolean | ((input: string, record: SrColumnFilterItem) => boolean)
  onFilter?: (value: SrKey | boolean, record: T) => boolean
  filterDropdownOpen?: boolean
  onFilterDropdownOpenChange?: (visible: boolean) => void
}

export interface SrColumnGroupType<T> extends Omit<SrColumnType<T>, 'dataIndex'> {
  children: SrColumnsType<T>
}
export type SrColumnsType<T> = (SrColumnGroupType<T> | SrColumnType<T>)[]

/** DataTable.onChange 兼容签名所需结构(本地复刻 antd 第四参) */
export interface SrTableCurrentDataSource<T> {
  currentDataSource: T[]
  action: string
}

export interface SrSorterResult<T> {
  column?: SrColumnType<T>
  order?: SrSortOrder
  field?: SrKey | readonly SrKey[]
  columnKey?: SrKey
}

/** DataTable.expandable 兼容结构(本地复刻 antd ExpandableConfig;仅 expandedRowRender/rowExpandable 生效) */
export interface SrExpandableConfig<T> {
  expandedRowKeys?: readonly SrKey[]
  defaultExpandedRowKeys?: readonly SrKey[]
  expandedRowRender?: (record: T, index: number, indent: number, expanded: boolean) => ReactNode
  columnTitle?: ReactNode
  expandRowByClick?: boolean
  expandIcon?: (props: {
    prefixCls: string
    expanded: boolean
    record: T
    expandable: boolean
    onExpand: (record: T, event: MouseEvent<HTMLElement>) => void
  }) => ReactNode
  onExpand?: (expanded: boolean, record: T) => void
  onExpandedRowsChange?: (expandedKeys: readonly SrKey[]) => void
  defaultExpandAllRows?: boolean
  indentSize?: number
  expandIconColumnIndex?: number
  showExpandColumn?: boolean
  expandedRowClassName?: string | ((record: T, index: number, indent: number) => string)
  childrenColumnName?: string
  rowExpandable?: (record: T) => boolean
  columnWidth?: number | string
  fixed?: SrFixedType
  expandedRowOffset?: number
}

/** DataTable.pagination 兼容结构(本地复刻 antd TablePaginationConfig;索引签名承载 antd 特有字段透传) */
export interface SrPaginationConfig {
  current?: number
  pageSize?: number
  total?: number
  defaultCurrent?: number
  defaultPageSize?: number
  hideOnSinglePage?: boolean
  showSizeChanger?: boolean
  showQuickJumper?: boolean
  showTotal?: (total: number, range: [number, number]) => ReactNode
  size?: 'default' | 'small'
  simple?: boolean
  disabled?: boolean
  locale?: Record<string, unknown>
  onChange?: (page: number, pageSize: number) => void
  onShowSizeChange?: (current: number, size: number) => void
  pageSizeOptions?: (string | number)[]
  itemRender?: (page: number, type: string, originalElement: ReactNode) => ReactNode
  placement?: string[]
  position?: string[]
  /** 兼容期:允许 antd 特有分页字段透传(不逐字段校验) */
  [key: string]: unknown
}

/** 列形态判别:任一列含 antd 特有字段 → antd 列数组(需转换);否则视为 SrColumn 直通 */
export function isSrColumnArray<T>(columns: SrColumnsType<T> | SrColumn<T>[]): columns is SrColumn<T>[] {
  return !(columns as Array<Record<string, unknown>>).some((c) =>
    ANTD_ONLY_COL_FIELDS.some((k) => c[k] !== undefined))
}

/** warn 收集器:Set 去重,同一 tag 只输出一次;前缀 [DataTable] */
const warned = new Set<string>()
export function warnOnce(tag: string, msg: string): void {
  if (warned.has(tag)) return
  warned.add(tag)
  console.warn(`[DataTable] ${msg}`)
}

/** 列名提取(仅用于 warn 消息可读性) */
function colLabel(col: SrColumnType<unknown> | SrColumnGroupType<unknown>): string {
  const t = col as SrColumnType<unknown>
  if (typeof t.title === 'string') return t.title
  if (t.key != null) return String(t.key)
  if (t.dataIndex != null) return String(t.dataIndex)
  return '?'
}

/**
 * antd 列 → 中性列契约转换器(T-38 薄壳隔离面核心;T-45 类型本地化后逻辑不变):
 * 精确映射 dataIndex/title/render/width/minWidth/align/fixed/key/className/ellipsis(boolean)/
 * sorter(boolean/函数)/sortOrder;拿不准的形态降级(见文件末尾清单)。
 */
export function fromAntdColumns<T>(cols: SrColumnsType<T>): SrColumn<T>[] {
  return cols.map((col) => {
    const t = col as SrColumnType<T>
    const name = colLabel(col as SrColumnType<unknown>)
    const out: SrColumn<T> = {}

    // dataIndex:string | number | readonly (string|number)[];嵌套数组取第一段(深层取值页面 render 自理)
    if (t.dataIndex != null) {
      if (Array.isArray(t.dataIndex)) {
        if (t.dataIndex.length > 1) {
          warnOnce('dataIndex-array', `列「${name}」dataIndex 为嵌套路径数组,仅取第一段;深层字段请页面 render 自行取值`)
        }
        out.dataIndex = String(t.dataIndex[0]) as keyof T | string
      } else {
        out.dataIndex = String(t.dataIndex) as keyof T | string
      }
    }

    // title:ReactNode 直通;函数形态(ColumnTitle 渲染函数)无法中性化,丢弃
    if (typeof t.title === 'function') {
      warnOnce('title-function', `列「${name}」title 为函数形态(渲染函数)不支持,已丢弃`)
    } else if (t.title !== undefined) {
      out.title = t.title
    }

    if (t.render !== undefined) out.render = t.render as SrColumn<T>['render']
    if (t.width !== undefined) out.width = t.width
    if (t.minWidth !== undefined) out.minWidth = t.minWidth
    // align:rc AlignType 含 start/end/justify/match-parent,归一为 left/right/center(其余降级丢弃)
    if (t.align === 'left' || t.align === 'right' || t.align === 'center') out.align = t.align
    else if (t.align === 'start') out.align = 'left'
    else if (t.align === 'end') out.align = 'right'
    else if (t.align !== undefined) {
      warnOnce('align-exotic', `列「${name}」align 为 ${t.align}(文本对齐语义),表格列不支持,已忽略`)
    }
    if (t.className !== undefined) out.className = t.className

    // fixed:rc FixedType 含 boolean/'start'/'end',统一归一为 'left'|'right'(boolean true ≈ left)
    if (t.fixed === true) out.fixed = 'left'
    else if (t.fixed === 'left' || t.fixed === 'right') out.fixed = t.fixed
    else if (t.fixed === 'start') out.fixed = 'left'
    else if (t.fixed === 'end') out.fixed = 'right'

    // ellipsis:boolean 直通;对象 {showTitle} → true(去 Tooltip 提示)
    if (typeof t.ellipsis === 'object' && t.ellipsis !== null) {
      warnOnce('ellipsis-object', `列「${name}」ellipsis 为对象形态({showTitle}),已降级为纯截断(无 Tooltip 提示)`)
      out.ellipsis = true
    } else if (t.ellipsis !== undefined) {
      out.ellipsis = t.ellipsis
    }

    // sorter:boolean/函数直通;数组(多列排序) → true;对象 {compare,multiple} → 保留 compare(丢 multiple)
    const st = t.sorter
    if (Array.isArray(st)) {
      warnOnce('sorter-array', `列「${name}」sorter 为数组(多列排序)不支持,已降级为 true(服务端排序,仅指示器)`)
      out.sorter = true
    } else if (typeof st === 'function') {
      out.sorter = st
    } else if (typeof st === 'object' && st !== null) {
      warnOnce('sorter-multiple', `列「${name}」sorter 为对象 {compare,multiple} 的多列排序语义不支持,仅保留 compare 单列排序`)
      out.sorter = typeof st.compare === 'function' ? st.compare : true
    } else if (st !== undefined) {
      out.sorter = st
    }

    // sortOrder:直接映射(受控排序)
    if (t.sortOrder !== undefined) out.sortOrder = t.sortOrder

    // 分组表头:children 丢弃(降级为单层)
    if ('children' in t && t.children != null) {
      warnOnce('children-nested', `列「${name}」children 嵌套表头不支持,已降级为单层(丢弃子列)`)
    }

    // 筛选体系:全部丢弃(薄壳不实现列筛选)
    if (t.filters !== undefined) warnOnce('drop-filters', `列「${name}」filters(列筛选)不支持,已忽略`)
    if (t.filterDropdown !== undefined) warnOnce('drop-filterDropdown', `列「${name}」filterDropdown(自定义筛选)不支持,已忽略`)
    if (t.filteredValue !== undefined) warnOnce('drop-filteredValue', `列「${name}」filteredValue(受控筛选)不支持,已忽略`)
    if (t.filterMode !== undefined) warnOnce('drop-filterMode', `列「${name}」filterMode 不支持,已忽略`)
    if (t.filterSearch !== undefined) warnOnce('drop-filterSearch', `列「${name}」filterSearch 不支持,已忽略`)
    if (t.onFilter !== undefined) warnOnce('drop-onFilter', `列「${name}」onFilter 不支持,已忽略`)
    if (t.onFilterDropdownOpenChange !== undefined) warnOnce('drop-onFilterDropdownOpenChange', `列「${name}」onFilterDropdownOpenChange 不支持,已忽略`)

    // 单元格/表头钩子:丢弃(表头 12px/对齐由 DataTable 薄壳统一内置)
    if (t.onHeaderCell !== undefined) warnOnce('drop-onHeaderCell', `列「${name}」onHeaderCell(表头单元格钩子)不支持,已忽略`)
    if (t.onCell !== undefined) warnOnce('drop-onCell', `列「${name}」onCell(单元格钩子)不支持,已忽略`)
    if (t.onCellClick !== undefined) warnOnce('drop-onCellClick', `列「${name}」onCellClick(单元格点击)不支持,已忽略`)

    // key:直接映射(String 归一,React.Key 可能是 number)
    if (col.key != null) out.key = String(col.key)

    return out
  })
}

/**
 * fromAntdColumns 降级点完整清单(T-38):
 * 1. dataIndex 为嵌套数组(长度>1)→ 取第一段,深层取值页面 render 自理
 * 2. title 为函数形态(ColumnTitle 渲染函数)→ 丢弃标题
 * 3. ellipsis 为对象 {showTitle} → 降级为 true(纯截断,无 Tooltip)
 * 4. sorter 为数组(多列排序)→ 降级为 true(服务端排序,仅指示器)
 * 5. sorter 为对象 {compare,multiple} → 仅保留 compare 单列排序(multiple 多列语义丢弃)
 * 6. children(嵌套表头)→ 丢弃子列,降级为单层
 * 7. filters / filterDropdown / filteredValue / filterMode / filterSearch / onFilter /
 *    onFilterDropdownOpenChange → 丢弃(薄壳不实现列筛选)
 * 8. onHeaderCell / onCell / onCellClick → 丢弃(表头 12px/对齐由 DataTable 薄壳统一内置)
 * 精确映射(非降级):dataIndex 单值、title(ReactNode)、render、width、minWidth、align、
 *   fixed(boolean/start/end 归一为 left/right)、key、className、ellipsis(boolean)、
 *   sorter(boolean/函数)、sortOrder。
 * 注:fixed + 虚拟表冲突由 DataTable 层处理,转换器不关心。
 * 注(T-45):本文件类型已本地化,与 antd 不再有任何 import 关系;结构对齐 antd 仅为兼容期
 *   页面(尚未切换 SrColumn 的列定义)提供类型通道,antd peer 移除后此节可一并删除。
 */
