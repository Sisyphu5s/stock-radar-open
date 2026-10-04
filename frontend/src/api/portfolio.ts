/** 组合优化(T-10) + 多因子合成(T-11)契约(独立于 client.ts,后端 /alpha/optimize、/alpha/combine)。 */
import { api, callWithPanelRebuild } from './client'
import type { PanelRebuildProgress } from './client'

// ===== 组合优化 =====

export type OptimizeMethod = 'mv' | 'risk_parity' | 'min_var'

export const OPTIMIZE_METHOD_LABEL: Record<string, string> = {
  mv: '均值方差',
  risk_parity: '风险平价',
  min_var: '最小方差',
}

export const OPTIMIZE_METHOD_OPTIONS: { label: string; value: OptimizeMethod }[] = [
  { label: '均值方差', value: 'mv' },
  { label: '风险平价', value: 'risk_parity' },
  { label: '最小方差', value: 'min_var' },
]

export interface OptimizeWeightItem {
  code: string
  weight: number
}

export interface OptimizePerf {
  annual_return: number | null
  volatility: number | null
  sharpe: number | null
}

/** POST /alpha/optimize 载荷 */
export interface OptimizePayload {
  expression: string
  dataset_id: number
  horizon?: number
  method: OptimizeMethod
  max_weight: number
  risk_aversion?: number
  /** 按信号排名选出的股票数(默认 20) */
  top_n?: number
}

export interface OptimizeResult {
  method: string
  max_weight: number
  top_n: number
  weights: OptimizeWeightItem[]
  perf: OptimizePerf
  turnover: number | null
}

export async function runOptimize(payload: OptimizePayload): Promise<OptimizeResult> {
  const { data } = await api.post('/alpha/optimize', payload)
  return data
}

// ===== 多因子合成 =====

export type CombineMethod = 'score' | 'ic_weight' | 'equal'

export const COMBINE_METHOD_LABEL: Record<string, string> = {
  score: '打分法(等权秩和)',
  ic_weight: 'IC 加权',
  equal: '等权平均(信号)',
}

export const COMBINE_METHOD_OPTIONS: { label: string; value: CombineMethod }[] = [
  { label: '打分法(等权秩和)', value: 'score' },
  { label: 'IC 加权', value: 'ic_weight' },
  { label: '等权平均(信号)', value: 'equal' },
]

export interface CombineWeightItem {
  /** 因子标识:factor_id 模式为因子名,exprs 模式为表达式字符串 */
  name: string
  weight: number
}

export interface CombineSignalPoint {
  code: string
  value: number
}

/** POST /alpha/combine 载荷:factor_ids(因子库)与 exprs(表达式)二选一 */
export interface CombinePayload {
  factor_ids?: number[]
  exprs?: string[]
  dataset_id: number
  horizon?: number
  method: CombineMethod
  save_to_library?: boolean
  /** 入因子库时的因子名(缺省自动生成) */
  factor_name?: string
}

export interface CombineResult {
  method: string
  /** 合成信号与 fwd 的 RankIC */
  ic: number | null
  ic_series: (number | null)[]
  ic_positive_ratio: number | null
  weights: CombineWeightItem[]
  /** 最近截面按信号值降序的股票(上限 30 只) */
  combined_signal: CombineSignalPoint[]
  /** 生成的可入库合成表达式(compile 可解析) */
  expression: string
  /** save_to_library=true 时返回 create_factor 结果 */
  factor?: { id: number; name: string; duplicate?: boolean } | null
}

export async function runCombine(payload: CombinePayload, onProgress?: (job: PanelRebuildProgress) => void): Promise<CombineResult> {
  // 面板冷缓存 miss → 202 受理：自动轮询 panel_build 后重试本端点（C11b）
  return callWithPanelRebuild(
    () => api.post('/alpha/combine', payload).then((r) => r.data),
    { onProgress },
  )
}
