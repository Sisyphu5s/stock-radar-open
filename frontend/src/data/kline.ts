import { api } from '../api/client'
import { pollInterval, queryClient, useQueryState } from './queryBase'
import type { QueryState } from './queryBase'
import type { KlineData } from '../utils/klineSeries'
import { isCnTradingSession } from '../utils/time'

/**
 * K 线查询（code:period:days 参数化，全站共享一份请求与缓存）：
 * 60s SWR + 有订阅者时 60s 轮询（与 quote 池同节奏；最后一个订阅者离开自动停表）；
 * 非交易时段降频至 5min 轮询（K 线收盘后基本静止）。
 *
 * 不经 persistedGet 持久缓存：查询层自身 SWR/TTL 承担缓存，持久缓存会把轮询架空
 * （命中旧值后不再发请求）。非交易时段后端 is_stale 判定不 stale，轮询请求仅 DB 读
 * 零网络，全天候 60s 轮询无实质负担。
 *
 * client.ts getKline（persistedGet 层，5min 短 TTL）仅服务关注页迷你 K线等低频读场景，
 * 工作台 K 线一律走本查询（T-08 后 persistedGet 不再服务任何轮询接口）。
 *
 * 错误语义与 KlineChart 对齐：首次失败且无数据 → error 呈现错误空态（可重试）；
 * 轮询失败保留旧 value → 图表静默保旧图。
 */
function klineQuery(code: string | undefined, period: string, days = 300, enabled?: boolean) {
  return {
    queryKey: ['mkt', 'kline', code ?? '', period, days],
    queryFn: async () => {
      const { data } = await api.get(`/stocks/${code}/kline`, { params: { period, days } })
      return data as KlineData
    },
    enabled: enabled ?? !!code,
    staleTime: 60_000,
    refetchInterval: pollInterval(60_000, { slowWhen: () => !isCnTradingSession(), slowPollMs: 300_000 }),
  }
}

/** 订阅 K 线完整状态（value=KlineData/loading/error/fetchedAt）；code 为空时不订阅，返回稳定空态。
 *  enabled=false 停止订阅与轮询（工作台保活 tab 非活跃时停数据活动） */
export function useKlineState(code: string | undefined, period: string, days = 300, enabled?: boolean): QueryState<KlineData> {
  return useQueryState(klineQuery(code, period, days, enabled))
}

/** K 线中央失效：sr-refresh / 手动重试调用（失效后立即重拉） */
export function invalidateKlines() {
  queryClient.invalidateQueries({ queryKey: ['mkt', 'kline'] })
}
