import { api } from '../api/client'
import type { CapitalHistoryResponse, CapitalLatest, CapitalType } from '../api/capital'
import { pollInterval, queryClient, useQueryState } from './queryBase'
import type { QueryState } from './queryBase'
import { isCnTradingSession } from '../utils/time'

/**
 * 资金类历史查询池（T-08 资金流/龙虎榜/两融/北向历史持股）：
 * code:type 参数化，全站共享一份请求与缓存。资金为日频数据——60s SWR +
 * 有订阅者时 60s 轮询（与 quote/kline/financials 池同节奏），非交易时段降频 5min；
 * 后端 SQLite 缓存兜底：轮询命中库内数据不触发 akshare 网络（仅库缺/过期才拉取）。
 * 切股 → code 变化 → 新查询自动重拉；末订阅者离开 100ms 后惰性淘汰（gcTime 机制）。
 * enabled=false（如 Tab 未激活）不订阅、不发请求、不轮询。
 */
function capitalHistoryQuery(code: string | undefined, type: CapitalType, limit: number) {
  return {
    queryKey: ['mkt', 'capital', code ?? '', type, limit],
    queryFn: async () => {
      const { data } = await api.get(`/stocks/${code}/capital/history`, { params: { type, limit } })
      return data as CapitalHistoryResponse
    },
    enabled: !!code,
    staleTime: 60_000,
    refetchInterval: pollInterval(60_000, { slowWhen: () => !isCnTradingSession(), slowPollMs: 300_000 }),
  }
}

/** 订阅资金类历史完整状态（value/loading/error/fetchedAt）；code 为空或 enabled=false 时不订阅 */
export function useCapitalHistoryState(
  code: string | undefined,
  type: CapitalType,
  limit = 60,
  enabled = true,
): QueryState<CapitalHistoryResponse> {
  return useQueryState({ ...capitalHistoryQuery(code, type, limit), enabled: enabled && !!code })
}

/** 资金类各类型最新一行合并摘要查询（GET /stocks/{code}/capital/latest，60s SWR 轮询同 history 池） */
function capitalLatestQuery(code: string | undefined) {
  return {
    queryKey: ['mkt', 'capital', code ?? '', 'latest'],
    queryFn: async () => {
      const { data } = await api.get(`/stocks/${code}/capital/latest`)
      return data as CapitalLatest
    },
    enabled: !!code,
    staleTime: 60_000,
    refetchInterval: pollInterval(60_000, { slowWhen: () => !isCnTradingSession(), slowPollMs: 300_000 }),
  }
}

/** 订阅资金类最新摘要；code 为空时不订阅，返回稳定空态 */
export function useCapitalLatestState(code: string | undefined): QueryState<CapitalLatest> {
  return useQueryState(capitalLatestQuery(code))
}

/** 资金池中央失效：sr-refresh / 手动重试调用（失效后立即重拉） */
export function invalidateCapital() {
  queryClient.invalidateQueries({ queryKey: ['mkt', 'capital'] })
}
