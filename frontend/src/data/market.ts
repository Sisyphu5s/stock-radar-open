import { getSignalEvents, getSignalEventsPage, getQuote, getWatchlist, stableParamKey } from '../api/client'
import type { PageResult, SignalEvent, SignalEventsPageParams, WatchlistItem } from '../api/client'
import { isCnTradingSession } from '../utils/time'
import { pollInterval, queryClient, useQueryState, useQueryValue } from './queryBase'
import type { QueryState } from './queryBase'
import { isMarketStreamActive } from './marketStream'

/** 行情域轮询节奏：60s SWR + 有订阅者时 60s 轮询；非交易时段降频至 5min（收盘后事件流/行情静止，低频即可）。
 *  T-04：SSE 推送活跃（isMarketStreamActive）时轮询同样放宽到 5min——推送直灌缓存
 *  已在 60s 轮询间隙内更新行情,轮询保留为推送断开时的兜底(断开后回 60s)。 */
const MARKET_STALE = 60_000
const marketInterval = pollInterval(60_000, {
  slowWhen: () => !isCnTradingSession() || isMarketStreamActive(),
  slowPollMs: 300_000,
})

/** 信号事件流查询（参数化池：stableParamKey 排序保证同参数同 key → 全站共享一份请求与缓存；
 *  queryKey 前缀 ['mkt','events'] 供 invalidateSignalEvents 批量失效；sr-refresh 一键全清）。
 *  关注页一次拉 5000 条，轮询已从 30s 收敛到 60s（低频全量窗口）。 */
function eventsQuery(params: Record<string, unknown>) {
  // 空值过滤（与 stableParamKey 同语义）后直接以原始类型传参：number/boolean 不再经 stable key 往返降级为 string
  const clean = Object.fromEntries(Object.entries(params).filter(([, v]) => v != null && v !== ''))
  return {
    // queryKey 身份仍用 stableParamKey 稳定字符串（同参数必得同 key）
    queryKey: ['mkt', 'events', stableParamKey(clean)],
    // queryFn 引用原始 params（过滤空值后），不走 parseParamKey 往返——参数类型保真
    queryFn: () => getSignalEvents(clean),
    staleTime: MARKET_STALE,
    refetchInterval: marketInterval,
  }
}
export function useSignalEvents(params: Record<string, unknown>) {
  return useQueryValue(eventsQuery(params))
}
/** 信号事件流完整状态（value/loading/error）：供关注页感知首拉失败（错误不再伪装成加载中/空态） */
export function useSignalEventsState(params: Record<string, unknown>): QueryState<SignalEvent[]> {
  return useQueryState(eventsQuery(params))
}
export function invalidateSignalEvents() {
  queryClient.invalidateQueries({ queryKey: ['mkt', 'events'] })
}

/** 信号事件分页查询（筛选+分页各自独立缓存 key；60s SWR + 有订阅者时 60s 轮询，与 events 池同节奏）。
 *  分页模式单次拉 1000 条，已从 30s 降频到 60s。 */
function eventsPageQuery(params: SignalEventsPageParams = {}) {
  const merged = { limit: 50, offset: 0, ...params }
  // 空值过滤（与 stableParamKey 同语义）后直接以原始类型传参：number/boolean 不再经 stable key 往返降级为 string
  const clean = Object.fromEntries(Object.entries(merged).filter(([, v]) => v != null && v !== ''))
  return {
    // queryKey 身份仍用 stableParamKey 稳定字符串（同参数必得同 key）
    queryKey: ['mkt', 'events:page', stableParamKey(clean)],
    // queryFn 引用原始 merged（过滤空值后），不走 parseParamKey 往返——参数类型保真（limit/offset 缺省自动补齐）
    queryFn: () => getSignalEventsPage(clean as SignalEventsPageParams),
    staleTime: MARKET_STALE,
    refetchInterval: marketInterval,
  }
}
/** 分页信号事件：返回完整 QueryState（value=PageResult/loading/error/fetchedAt）。
 *  筛选（含模板/信号类型服务端执行）与分页任一参数变化即独立缓存；limit/offset 缺省自动补齐（limit=50, offset=0）。 */
export function useSignalEventsPage(params: SignalEventsPageParams = {}): QueryState<PageResult<SignalEvent>> {
  return useQueryState(eventsPageQuery(params))
}
export function invalidateSignalEventsPage() {
  queryClient.invalidateQueries({ queryKey: ['mkt', 'events:page'] })
}

/** 个股实时行情（60s SWR + 有订阅者时 60s 轮询，按 code 参数化；工作台/关注页共享）。
 *  非交易时段降频至 5min 轮询（收盘后行情静止，低频即可）。 */
function quoteQuery(code: string | undefined, enabled?: boolean) {
  return {
    queryKey: ['mkt', 'quote', code ?? ''],
    queryFn: () => getQuote(code as string),
    // code 为空时不订阅、不发请求（避免哨兵请求 /stocks/__none__/quote；invalidate 时 disabled 查询亦被过滤）；
    // enabled=false(工作台非活跃 tab 停订阅)时不发请求、不轮询，保留停前缓存
    enabled: enabled ?? !!code,
    staleTime: MARKET_STALE,
    refetchInterval: marketInterval,
  }
}
/** 个股实时行情完整状态（value/loading/error）：供消费方感知行情失败（useQuote 仅返回 value）。
 *  enabled=false 停止订阅与轮询（工作台保活 tab 非活跃时停数据活动） */
export function useQuoteState(code: string | undefined, enabled?: boolean): QueryState<Awaited<ReturnType<typeof getQuote>>> {
  return useQueryState(quoteQuery(code, enabled))
}
export function useQuote(code: string | undefined) {
  return useQuoteState(code).value
}
export function invalidateQuotes() {
  queryClient.invalidateQueries({ queryKey: ['mkt', 'quote'] })
}

/** 关注列表（60s SWR + 有订阅者时 60s 轮询；非交易时段降频至 5min，收盘后行情静止） */
function watchlistQuery() {
  return {
    queryKey: ['mkt', 'watchlist'],
    queryFn: () => getWatchlist(),
    staleTime: MARKET_STALE,
    refetchInterval: marketInterval,
  }
}
export function useWatchlist() {
  return useQueryValue(watchlistQuery())
}
/** 关注列表完整状态（value/loading/error）：供关注页感知首拉失败 */
export function useWatchlistState(): QueryState<WatchlistItem[]> {
  return useQueryState(watchlistQuery())
}
export function invalidateWatchlist() {
  queryClient.invalidateQueries({ queryKey: ['mkt', 'watchlist'] })
}

/** 市场数据中央失效：顶栏刷新按钮 / sr-refresh 广播调用 */
export function invalidateMarket() {
  invalidateSignalEvents()
  invalidateSignalEventsPage()
  invalidateQuotes()
  invalidateWatchlist()
}
