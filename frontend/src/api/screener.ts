import { api } from './client'

// ===== 条件选股（T-17）API 契约：POST /screener/run =====
// 类型独立于此文件（不扩展 client.ts）；请求 = 快照条件组合查询，结果不轮询。

/** 条件字段白名单（与后端 FIELDS 一致）：来源 = 快照可得列。
 *  volume_ratio 为可选列（部分数据源不提供）：条件不命中 / 结果 null。 */
export type ScreenerField =
  | 'price'
  | 'pct_change'
  | 'turnover_rate'
  | 'volume_ratio'
  | 'pe'
  | 'pb'
  | 'market_cap'

export type ScreenerOp = 'gt' | 'gte' | 'lt' | 'lte'
export type ScreenerLogic = 'and' | 'or'
export type ScreenerUniverse = 'watchlist' | 'hs300' | 'all'

/** 叶子条件（T-75 条件树）：单个字段比较 */
export interface ScreenerCondition {
  field: ScreenerField
  op: ScreenerOp
  value: number
}

/** 条件分组（括号）：logic 归并 children，递归嵌套 */
export interface ScreenerConditionGroup {
  logic: ScreenerLogic
  children: ScreenerConditionNode[]
}

/** 条件树节点：叶子 或 分组（同构递归） */
export type ScreenerConditionNode = ScreenerCondition | ScreenerConditionGroup

/** 条件树根（顶层分组；logic = 顶层条件逻辑） */
export type ScreenerConditionTree = ScreenerConditionGroup

/** 判别节点是否为分组（含 children 键） */
export function isScreenerGroup(node: ScreenerConditionNode): node is ScreenerConditionGroup {
  return 'children' in node
}

export interface ScreenerRunPayload {
  universe: ScreenerUniverse
  conditions: ScreenerConditionTree
}

/** 选股结果行（后端输出契约，缺列字段为 null） */
export interface ScreenerResultRow {
  code: string
  name: string
  price: number | null
  pct_change: number | null
  turnover_rate: number | null
  volume_ratio: number | null
  pe: number | null
  pb: number | null
  market_cap: number | null
}

export interface ScreenerRunResponse {
  data: ScreenerResultRow[]
  count: number
  source: string
  /** 快照数据时刻（naive ISO；不可用为 null） */
  as_of: string | null
}

export async function runScreener(payload: ScreenerRunPayload): Promise<ScreenerRunResponse> {
  const { data } = await api.post('/screener/run', payload)
  return data
}

// ===== 字段/操作符展示元数据（单一事实源，条件编辑器与结果表共用） =====

/** 字段选择项（顺序 = 后端 FIELDS 顺序） */
export const SCREENER_FIELD_OPTIONS: { value: ScreenerField; label: string }[] = [
  { value: 'price', label: '最新价' },
  { value: 'pct_change', label: '涨跌幅(%)' },
  { value: 'turnover_rate', label: '换手率(%)' },
  { value: 'volume_ratio', label: '量比' },
  { value: 'pe', label: '市盈率' },
  { value: 'pb', label: '市净率' },
  { value: 'market_cap', label: '总市值(亿)' },
]

export const SCREENER_OP_OPTIONS: { value: ScreenerOp; label: string }[] = [
  { value: 'gt', label: '>' },
  { value: 'gte', label: '≥' },
  { value: 'lt', label: '<' },
  { value: 'lte', label: '≤' },
]

export const SCREENER_LOGIC_OPTIONS: { value: ScreenerLogic; label: string }[] = [
  { value: 'and', label: 'AND · 全部满足' },
  { value: 'or', label: 'OR · 任一满足' },
]

export const SCREENER_UNIVERSE_OPTIONS: { value: ScreenerUniverse; label: string }[] = [
  { value: 'watchlist', label: '我的关注' },
  { value: 'hs300', label: '沪深300' },
  { value: 'all', label: '全市场' },
]

export function screenerFieldLabel(field: ScreenerField): string {
  return SCREENER_FIELD_OPTIONS.find((o) => o.value === field)?.label ?? field
}
