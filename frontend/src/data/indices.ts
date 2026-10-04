import { getIndices } from '../api/client'
import type { IndexQuote } from '../api/client'
import { pollInterval, queryClient, useQueryState, useQueryValue } from './queryBase'
import type { QueryState } from './queryBase'

/** 全球指数轮询节奏：60s SWR + 有订阅者时 60s 轮询。
 *  与行情池同节奏但不做交易时段降频——指数横跨 CN/US/HK，任意时刻都有市场
 *  在交易（美股北京时间夜间、A 股/港股白天），降频会错过活跃市场盘中变化。 */
const INDEX_STALE = 60_000
const indexInterval = pollInterval(60_000)

function indicesQuery() {
  return {
    queryKey: ['mkt', 'indices'],
    queryFn: () => getIndices(),
    staleTime: INDEX_STALE,
    refetchInterval: indexInterval,
  }
}

/** 全球核心指数（8 只）：完整状态（value/loading/error）。失败由组件静默降级，
 *  不阻塞页面（指数条为非关键展示通道）。 */
export function useIndicesState(): QueryState<IndexQuote[]> {
  return useQueryState(indicesQuery())
}

export function useIndices() {
  return useQueryValue(indicesQuery())
}

export function invalidateIndices() {
  queryClient.invalidateQueries({ queryKey: ['mkt', 'indices'] })
}
