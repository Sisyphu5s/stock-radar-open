import { api } from '../api/client'
import type { FinStatementType, FinancialHistoryResponse, FinancialLatest } from '../api/client'
import { pollInterval, queryClient, useQueryState } from './queryBase'
import type { QueryState } from './queryBase'
import { isCnTradingSession } from '../utils/time'

/**
 * 财务历史查询池（T-06 基本面历史库）：
 * code:type 参数化，全站共享一份请求与缓存。财务为日频/季频数据——
 * 60s SWR + 有订阅者时 60s 轮询（与 quote/kline 池同节奏），非交易时段降频 5min；
 * 后端 SQLite 缓存兜底：轮询命中库内数据不触发 akshare 网络（仅库缺/过期才拉取）。
 * 切股 → code 变化 → 新查询自动重拉；末订阅者离开 100ms 后惰性淘汰（gcTime 机制）。
 * enabled=false（如 Tab 未激活）不订阅、不发请求、不轮询。
 */
function financialHistoryQuery(code: string | undefined, type: FinStatementType, limit: number) {
  return {
    queryKey: ['mkt', 'fin', code ?? '', type, limit],
    queryFn: async () => {
      const { data } = await api.get(`/stocks/${code}/financials/history`, { params: { type, limit } })
      return data as FinancialHistoryResponse
    },
    enabled: !!code,
    staleTime: 60_000,
    refetchInterval: pollInterval(60_000, { slowWhen: () => !isCnTradingSession(), slowPollMs: 300_000 }),
  }
}

/** 订阅财务历史完整状态（value/loading/error/fetchedAt）；code 为空或 enabled=false 时不订阅 */
export function useFinancialHistoryState(
  code: string | undefined,
  type: FinStatementType,
  limit = 40,
  enabled = true,
): QueryState<FinancialHistoryResponse> {
  return useQueryState({ ...financialHistoryQuery(code, type, limit), enabled: enabled && !!code })
}

/** 三表最近一期合并摘要查询（GET /stocks/{code}/financials/latest，60s SWR 轮询同 history 池） */
function financialLatestQuery(code: string | undefined) {
  return {
    queryKey: ['mkt', 'fin', code ?? '', 'latest'],
    queryFn: async () => {
      const { data } = await api.get(`/stocks/${code}/financials/latest`)
      return data as FinancialLatest
    },
    enabled: !!code,
    staleTime: 60_000,
    refetchInterval: pollInterval(60_000, { slowWhen: () => !isCnTradingSession(), slowPollMs: 300_000 }),
  }
}

/** 订阅三表最近一期摘要；code 为空时不订阅，返回稳定空态 */
export function useFinancialLatestState(code: string | undefined): QueryState<FinancialLatest> {
  return useQueryState(financialLatestQuery(code))
}

/** 财务池中央失效：sr-refresh / 手动重试调用（失效后立即重拉） */
export function invalidateFinancials() {
  queryClient.invalidateQueries({ queryKey: ['mkt', 'fin'] })
}
