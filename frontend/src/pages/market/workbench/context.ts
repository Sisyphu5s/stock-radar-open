import { createContext, useContext, type RefObject } from 'react'
import type { KlineChartHandle } from '../../../components/KlineChart'
import type { KlineData } from '../../../utils/klineSeries'
import type { CombinedTuneResponse, IndicatorParamValues, IndicatorSpec, IndicatorStoredParams, TuneResponse } from '../../../api/client'
import type { TuneScope } from './persist'

// 周期常量统一在 utils/periods.ts（唯一事实源，含 k/l 结构的 K 线周期），此处 re-export 保持既有 import 方兼容
export { PERIODS } from '../../../utils/periods'

export const TUNE_LABEL: Record<string, string> = {
  rsi: 'RSI', ma: 'MA', ema: 'EMA', macd: 'MACD', kdj: 'KDJ', boll: 'BOLL', wr: 'WR', cci: 'CCI',
  roc: 'ROC', mtm: 'MTM', bias: 'BIAS', psy: 'PSY', trix: 'TRIX', cmo: 'CMO', volume_ratio: '量比',
}

/** 具名参数 → 紧凑展示串（按键插入顺序，后端按 spec 顺序返回）: {fast:12,slow:26,signal:9} → "12,26,9"。 */
export const fmtParamValues = (v: IndicatorParamValues | null | undefined): string => {
  if (!v) return ''
  const vals = Object.values(v).filter((x) => x != null && Number.isFinite(Number(x)))
  return vals.length ? vals.join(',') : ''
}

// ===== 工作台数据类型（宽松：以各消费方实际取值字段为准，未知字段走索引签名） =====

/** 实时行情（GET /stocks/{code}/quote，订阅层 60s SWR） */
export interface WorkbenchQuote {
  name?: string
  price?: number | string
  pct_change?: number
  source?: string
  /** 行情服务器上海时刻（naive ISO，秒级；替代浏览器本地钟，字符串直解禁 new Date） */
  timestamp?: string
  [k: string]: unknown
}

/** 基本面 · 估值（GET /stocks/{code}/fundamental） */
export interface WorkbenchFundamental {
  pe?: number
  pb?: number
  market_cap?: number
  float_cap?: number
  turnover_rate?: number
  source?: string
  [k: string]: unknown
}

/** 风险指标（GET /indicators/{code}/risk；数值字段允许 string，统一走 toFinite 清洗） */
export interface WorkbenchRisk {
  annual_volatility?: number | string
  max_drawdown?: number | string
  ret_series?: number[]
  explanations?: Record<string, string>
  max_loss_streak?: number
  [k: string]: unknown
}

/** 风险综合评级（四维打分，见 sections/shared.ts computeRiskGrade） */
export interface RiskGradeInfo {
  tag: string
  color: string
  desc: string
  dims: { l: string; s: number }[]
}

/**
 * 风险摘要（K 线叠加数据契约，供 fe-kline 等图表叠加消费；只读数据提供方，不画图）：
 * - grade：综合评级（低/中/高风险），null=数据不足以打分
 * - max_drawdown：最大回撤（负值比例），null=缺失
 * - var：VaR 单日预期损失（CF 修正优先，回退历史模拟；负值比例），null=缺失
 * 均为日线收盘口径，随 riskDays 窗口（0=全部历史）变化。
 */
export interface RiskSummary {
  grade: RiskGradeInfo | null
  max_drawdown: number | null
  var: number | null
}

/** 财务摘要（GET /stocks/{code}/financials；字段以「指标名」/「指标名@prev」动态键存放） */
export interface WorkbenchFinancials {
  period?: string
  prev_period?: string
  [k: string]: unknown
}

/** 新闻条目（GET /stocks/{code}/news） */
export interface WorkbenchNewsItem {
  title?: string
  url?: string
  date?: string
  time?: string
  source?: string
  [k: string]: unknown
}

/**
 * 时间线信号事件（GET /signals/events/{code}/timeline 的宽松视图）。
 * 注意：故意不加索引签名 —— 具体类型（如 client.ts 的 SignalEvent）无法赋给带索引签名的类型，
 * 会破坏 getStockTimeline → setTimeline 的自然赋值；字段全可选已足够宽松。
 */
export interface WorkbenchTimelineEvent {
  triggered_at?: string | null
  /** 数据截至时刻（naive ISO，可空；语义同 client.ts TimelineEvent.as_of，展示时刻 = as_of ?? triggered_at） */
  as_of?: string | null
  /** 扫描发现时刻（T-80 契约；旧数据 null） */
  scan_discovered_at?: string | null
  signals?: string[]
  status?: string
  evidence?: Record<string, unknown>
}

export interface WorkbenchCtxApi {
  code: string
  /** K 线图表句柄（KlinePanel 挂载后由 KlineChart ref 注入;StockHeader 导出截图等消费;未就绪 null） */
  klineRef: RefObject<KlineChartHandle | null>
  quote: WorkbenchQuote | null
  errQuote: string | null
  /** 行情最近一次刷新时间（quote.timestamp 上海 naive ISO 秒级字符串，非浏览器本地钟；无则 null） */
  quoteTime: string | null
  fundamental: WorkbenchFundamental | null
  klineData: KlineData | null
  loading: boolean
  /** K 线数据独立加载态（T-134）：K 线遮罩只随 K 线数据加载,不被基本面/新闻/财务拖住 */
  klineLoading: boolean
  errKline: string | null
  /** 指标数据加载失败提示（K 线图顶部警告条；null=无错误） */
  errInd: string | null
  /** 重试加载指标（清错误提示 + 重新拉取） */
  retryIndicators: () => void
  period: string
  setPeriod: (p: string) => void
  overlays: string[]
  toggleOverlay: (k: string) => void
  onRetry: () => void
  /** 关注状态（乐观更新，失败自动回滚） */
  watchlisted: boolean
  toggleWatch: () => Promise<void>
  wlPending: boolean
  tuneOpen: boolean
  setTuneOpen: (v: boolean) => void
  /** 指标参数契约（GET /indicators/params specs，按 key 索引） */
  specs: Record<string, IndicatorSpec>
  /** 系统默认参数（spec.defaults 汇总） */
  defaults: Record<string, IndicatorParamValues>
  /** 后端全局默认（v2 ind:v2:{indicator}） */
  globalParams: Record<string, IndicatorStoredParams>
  /** 手动覆盖（版本化 localStorage，优先级 manual > global > default） */
  manualParams: Record<string, IndicatorParamValues>
  /** 实际生效参数（manual > backend global > default，含图表响应合并） */
  effectiveParams: Record<string, IndicatorParamValues>
  /** 已覆盖系统默认（手动或全局）的指标 key 列表（升序） */
  tunedInds: string[]
  /** 写入手动覆盖并应用到当前图表（persist + 重拉指标） */
  applyManualParams: (ind: string, values: IndicatorParamValues) => Promise<void>
  /** 清除手动覆盖（回退 global/default） */
  clearManualParams: (ind: string) => Promise<void>
  /** 保存为全局默认（PUT /indicators/params/{indicator}） */
  saveGlobalParams: (ind: string, values: IndicatorParamValues) => Promise<void>
  /** 重置全局默认（DELETE /indicators/params/{indicator}） */
  resetGlobalParams: (ind: string) => Promise<void>
  /** 调优最优参数 → 手动覆盖应用到当前图表 */
  applyBestToChart: () => Promise<void>
  /** 调优最优参数 → 保存为全局默认 */
  saveBestGlobal: () => Promise<void>
  tuneInd: string
  setTuneInd: (v: string) => void
  /** 评估周期（未来 N 日收益）：InputNumber 空态可空，提交/计算时 null 回退默认 5 */
  tuneHorizon: number | null
  setTuneHorizon: (v: number | null) => void
  tuneTarget: string
  setTuneTarget: (v: string) => void
  tuneAlgorithm: 'grid' | 'random' | 'fast'
  setTuneAlgorithm: (v: 'grid' | 'random' | 'fast') => void
  tuneOptions: { value: string; label: string }[]
  /** 调优指标目录加载错误（/indicators/catalog 失败；null=正常） */
  tuneCatalogErr: string | null
  /** 指标参数契约加载错误（/indicators/params 失败；null=正常） */
  tuneContractErr: string | null
  /** 重试加载调优契约（目录 + 参数契约） */
  retryTuneContract: () => void
  /** 多选调优指标列表(会话级,含整体/逐个模式共用) */
  tuneInds: string[]
  setTuneInds: (v: string[]) => void
  /** 优化模式: individual 逐个优化 / combined 整体优化(≤3 指标联合评分) */
  tuneMode: 'individual' | 'combined'
  setTuneMode: (v: 'individual' | 'combined') => void
  /** 应用范围: chart 本图表(manual,localStorage)/ global 全部股票(后端全局,持久化) */
  tuneScope: TuneScope
  setTuneScope: (v: TuneScope) => void
  /** 调优结果: 单指标 key=indicator;整体 key='__combined__' */
  tuneResults: Record<string, TuneResponse | CombinedTuneResponse>
  tuneErrors: Record<string, string>
  tuneProgress: { current: string; done: number; total: number } | null
  tuning: boolean
  /** 取消调优进行中（请求在途，UI 反馈「正在取消…」） */
  cancelling: boolean
  runTune: () => Promise<void>
  retryTune: (key: string) => Promise<void>
  cancelTune: () => void
  /** 按 tuneScope 应用单指标/组合最优到 chart(manual)或 global(后端);key 缺省=当前 tuneInd */
  applyBest: (key?: string) => Promise<void>
  /** 批量应用全部成功结果(按 tuneScope),汇总 message */
  applyAllBest: () => Promise<void>
  /** 三档清除: manual 仅本图表 / global 仅全局默认 / all 全部;返回 {ok: string[], fail: string[]}(指标 key 明细) */
  clearTune: (scope: 'manual' | 'global' | 'all') => Promise<{ ok: string[]; fail: string[] }>
  risk: WorkbenchRisk | null
  errRisk: string | null
  riskDays: number
  setRiskDays: (v: number) => void
  /** 风险摘要（K 线叠加数据契约：grade/max_drawdown/var，随 riskDays 窗口变化） */
  riskSummary: RiskSummary
  timelineEvents: WorkbenchTimelineEvent[]
  errSignal: string | null
  labelMap: Record<string, { text: string; color: string }>
  errFund: string | null
  financials: WorkbenchFinancials | null
  errFinancials: string | null
  news: WorkbenchNewsItem[]
  errNews: string | null
}

export const WorkbenchCtx = createContext<WorkbenchCtxApi | null>(null)

export const useWorkbench = (): WorkbenchCtxApi => {
  const ctx = useContext(WorkbenchCtx)
  if (!ctx) throw new Error('useWorkbench 必须在 WorkbenchCtx.Provider 内使用')
  return ctx
}
