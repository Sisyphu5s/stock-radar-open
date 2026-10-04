import { getWatchlistGroups } from '../api/watchlistGroups'
import type { WatchlistGroup } from '../api/watchlistGroups'
import { pollInterval, queryClient, useQueryState } from './queryBase'
import type { QueryState } from './queryBase'

/** 自选股分组池（T-12）：60s SWR + 有订阅者时 60s 轮询，与 watchlist 池同节奏 */
const GROUP_STALE = 60_000
const groupInterval = pollInterval(60_000)

function groupsQuery() {
  return {
    queryKey: ['mkt', 'watchlist:groups'],
    queryFn: () => getWatchlistGroups(),
    staleTime: GROUP_STALE,
    refetchInterval: groupInterval,
  }
}

/** 分组列表完整状态（value/loading/error）：供关注页感知首拉失败与分组筛选 */
export function useWatchlistGroups(): QueryState<WatchlistGroup[]> {
  return useQueryState(groupsQuery())
}

/** 分组池失效：增删改分组/成员后调用，订阅方 SWR 自动重拉 */
export function invalidateWatchlistGroups() {
  queryClient.invalidateQueries({ queryKey: ['mkt', 'watchlist:groups'] })
}
