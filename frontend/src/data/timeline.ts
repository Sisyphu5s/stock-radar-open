import { getStockTimeline } from '../api/client'
import type { TimelineEvent } from '../api/client'
import { isCnTradingSession } from '../utils/time'
import { pollInterval, queryClient, useQueryState } from './queryBase'
import type { QueryState } from './queryBase'

/** 个股信号时间线池（工作台 TimelinePanel 消费）：60s SWR + 有订阅者时 60s 轮询；
 *  非交易时段降频 5min（收盘后信号更新频率低）——盘中 60s / 非交易 300s 与原手写 setTimeout
 *  链式轮询节奏一致；visibilitychange 停表 / 失败指数退避 / 惰性淘汰 / 退订停轮询由 queryBase
 *  池机制承担（替代 useWorkbenchData 手写轮询：页面隐藏时原实现依然在跑，P1-47 迁池）。 */
const TIMELINE_STALE = 60_000
const timelineInterval = pollInterval(60_000, {
  slowWhen: () => !isCnTradingSession(),
  slowPollMs: 300_000,
})

function timelineQuery(code: string, enabled?: boolean) {
  return {
    // queryKey 按 code 参数化（切股自动重拉）；前缀 ['timeline'] 供 invalidateTimeline 批量失效；
    // sr-refresh 全前缀一键全清（与其余池一致）
    queryKey: ['timeline', code],
    queryFn: () => getStockTimeline(code),
    enabled: enabled ?? !!code,
    staleTime: TIMELINE_STALE,
    refetchInterval: timelineInterval,
  }
}

/** 个股信号时间线完整状态（value/loading/error）：消费方感知首拉失败（错误空态）与
 *  轮询失败（保留旧值不闪烁，与 workbench 其余面板语义对齐）。
 *  enabled=false 停止订阅与轮询（工作台保活 tab 非活跃时停数据活动） */
export function useTimelineState(code: string, enabled?: boolean): QueryState<TimelineEvent[]> {
  return useQueryState(timelineQuery(code, enabled))
}

/** 时间线池失效：全局刷新（sr-refresh）/ 工作台重试调用 */
export function invalidateTimeline() {
  queryClient.invalidateQueries({ queryKey: ['timeline'] })
}
