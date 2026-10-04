import { api, callWithPanelRebuild } from './client'
import type { PanelRebuildProgress } from './client'

/**
 * 因子生命周期分析(T-09)API 契约:相关性矩阵 / IC 衰减 / 风格归因。
 * 端点挂在 /factors/analysis/*,纯计算在 backend/app/lib/alpha/factor_analysis.py。
 */

/** 因子相关性矩阵 + 冗余聚类(POST /factors/analysis/correlation) */
export interface FactorCorrelationResult {
  /** 两两 Pearson 相关矩阵(n×n,与 names 对齐;数据不足的配对为 null) */
  matrix: (number | null)[][]
  /** 因子名列表(与 matrix 行列对齐) */
  names: string[]
  /** 冗余聚类分组(单链:相关 ≥ threshold 归组),每组为因子名成员 */
  clusters: { members: string[] }[]
  threshold: number
  horizon: number
}

/** IC 衰减曲线 + 半衰期(POST /factors/analysis/ic-decay) */
export interface FactorIcDecayResult {
  horizons: number[]
  /** 与 horizons 等长;无效 horizon 为 null */
  ic_means: (number | null)[]
  /** |IC| 首次跌破峰值一半的 horizon;未衰减/无有效 IC 为 null */
  half_life: number | null
}

/** 风格归因(POST /factors/analysis/attribution) */
export interface FactorAttributionResult {
  /** 行业名 → 回归系数跨期均值(暴露) */
  industry_exposures: Record<string, number | null>
  /** 市值暴露(系数均值) */
  size_exposure: number | null
  /** 残差 IC:残差与未来收益的秩相关均值 */
  residual_ic: number | null
  /** 有效回归期数 */
  n_periods: number
}

export async function getFactorCorrelation(payload: {
  factor_ids: number[]
  dataset_id: number
  threshold?: number
  horizon?: number
}, onProgress?: (job: PanelRebuildProgress) => void): Promise<FactorCorrelationResult> {
  // 面板冷缓存 miss → 202 受理：自动轮询 panel_build 后重试本端点（C11b）
  return callWithPanelRebuild(
    () => api.post('/factors/analysis/correlation', payload).then((r) => r.data),
    { onProgress },
  )
}

export async function getFactorIcDecay(payload: {
  expr: string
  dataset_id: number
  horizons?: number[]
}, onProgress?: (job: PanelRebuildProgress) => void): Promise<FactorIcDecayResult> {
  return callWithPanelRebuild(
    () => api.post('/factors/analysis/ic-decay', payload).then((r) => r.data),
    { onProgress },
  )
}

export async function getFactorAttribution(payload: {
  expr: string
  dataset_id: number
}, onProgress?: (job: PanelRebuildProgress) => void): Promise<FactorAttributionResult> {
  return callWithPanelRebuild(
    () => api.post('/factors/analysis/attribution', payload).then((r) => r.data),
    { onProgress },
  )
}
