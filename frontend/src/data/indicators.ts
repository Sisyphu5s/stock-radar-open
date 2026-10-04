import { api, stableParamKey } from '../api/client'
import type { IndicatorParamValues } from '../api/client'
import { pollInterval, queryClient, useQueryState } from './queryBase'
import type { QueryState } from './queryBase'
import { isCnTradingSession } from '../utils/time'

/** 指标数据响应（GET /indicators/{code}）：indicators=序列数据，effective_params=最终生效参数（manual > backend global > default） */
export interface IndicatorDataResponse {
  indicators?: Record<string, number[]>
  effective_params?: Record<string, IndicatorParamValues>
  [k: string]: unknown
}

/**
 * 指标查询（参数化：code+fields+days+period+custom 全参数 key，全站共享一份请求与缓存）：
 * 60s SWR + 有订阅者时 60s 轮询（与 quote/kline 池同节奏；最后一个订阅者离开自动停表）；
 * 非交易时段降频至 5min 轮询（收盘后指标基本静止）。
 *
 * 替代旧 persistedGet('ind:') 持久缓存：轮询接口不能命中旧值后不再发请求（持久缓存会把轮询架空），
 * 查询层自身 SWR/TTL/惰性淘汰/指数退避/visibilitychange 停表承担缓存层；
 * 手动参数（custom）变化 → key 变化 → 新查询自动重拉，无需显式失效。
 */
function indicatorsQuery(params: Record<string, string | number> | undefined, enabled?: boolean) {
  return {
    queryKey: ['mkt', 'ind', params ? stableParamKey(params) : ''],
    // queryFn 引用原始 params（与 stableParamKey 同语义过滤空值后），不走 parseParamKey 往返——number 参数不再降级为 string
    queryFn: async () => {
      const p = Object.fromEntries(Object.entries(params ?? {}).filter(([, v]) => v != null && v !== ''))
      const code = String(p.code ?? '')
      const { code: _code, ...rest } = p
      const { data } = await api.get(`/indicators/${code}`, { params: rest })
      return data as IndicatorDataResponse
    },
    enabled: enabled ?? !!params,
    staleTime: 60_000,
    refetchInterval: pollInterval(60_000, { slowWhen: () => !isCnTradingSession(), slowPollMs: 300_000 }),
  }
}

/** 订阅指标完整状态（value/loading/error/fetchedAt）；params 为空时不订阅，返回稳定空态。
 *  enabled=false 停止订阅与轮询（工作台保活 tab 非活跃时停数据活动） */
export function useIndicatorsState(params: Record<string, string | number> | undefined, enabled?: boolean): QueryState<IndicatorDataResponse> {
  return useQueryState(indicatorsQuery(params, enabled))
}

/** 指标池中央失效：sr-refresh / 手动重试 / 保存全局参数后调用（失效后立即重拉） */
export function invalidateIndicators() {
  queryClient.invalidateQueries({ queryKey: ['mkt', 'ind'] })
}

// ===== 风险指标池（GET /indicators/{code}/risk） =====

/** 风险指标响应（GET /indicators/{code}/risk）：数值字段允许 string，消费方统一走 toFinite/Number 清洗 */
export interface RiskDataResponse {
  annual_volatility?: number | string
  max_drawdown?: number | string
  ret_series?: number[]
  explanations?: Record<string, string>
  [k: string]: unknown
}

/**
 * 风险指标查询（code:days 参数化，全站共享一份请求与缓存）：
 * 5min SWR + 有订阅者时 5min 轮询（风险指标日频计算，低频足够）；
 * 非交易时段降频至 15min 轮询（收盘后指标完全静止）。
 *
 * 统一收敛 WatchlistPage 组件级 riskCache（5min TTL 手写 Map 缓存）与
 * useWorkbenchData 手写 loadRisk（days 参数化：days=0 不传参、>0 传参）——
 * 前者只取 annual_volatility/max_drawdown，后者取完整风险指标集；
 * 参数变化 → key 变化 → 新查询自动重拉，无需显式失效。
 */
function riskQuery(code: string | undefined, days = 0, enabled?: boolean) {
  return {
    queryKey: ['mkt', 'risk', code ?? '', days],
    queryFn: async () => {
      const { data } = await api.get(`/indicators/${code}/risk`, days ? { params: { days: Number(days) } } : undefined)
      return data as RiskDataResponse
    },
    enabled: enabled ?? !!code,
    staleTime: 300_000,
    refetchInterval: pollInterval(300_000, { slowWhen: () => !isCnTradingSession(), slowPollMs: 900_000 }),
  }
}

/** 订阅风险指标完整状态（value=RiskDataResponse/loading/error/fetchedAt）；code 为空时不订阅，返回稳定空态。
 *  days=0 走后端缺省窗口（同 getRisk）；days>0 传参拉取指定窗口。
 *  enabled=false 停止订阅与轮询（工作台保活 tab 非活跃时停数据活动） */
export function useRiskState(code: string | undefined, days = 0, enabled?: boolean): QueryState<RiskDataResponse> {
  return useQueryState(riskQuery(code, days, enabled))
}

/** 订阅风险指标最新值（错误/加载态走 useRiskState）；供 WatchlistPage 收敛组件级 riskCache */
export function useRisk(code: string | undefined, days = 0): RiskDataResponse | undefined {
  return useRiskState(code, days).value
}

/** 风险池中央失效：sr-refresh / 手动重试调用（失效后立即重拉；WatchlistPage 收敛后可删组件级 riskCache.clear） */
export function invalidateRisk() {
  queryClient.invalidateQueries({ queryKey: ['mkt', 'risk'] })
}
