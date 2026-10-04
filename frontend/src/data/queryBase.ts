import { QueryClient, useQuery } from '@tanstack/react-query'
import type { QueryKey, UseQueryResult } from '@tanstack/react-query'

/**
 * TanStack Query 5 基础设施——自研 SWR 轮询池（src/data/query.ts，已删除）的替代基座。
 *
 * 语义映射（07-tech-stack.md §2.1，逐条源码级验证）：
 * | 自研池                          | TanStack Query 5                        |
 * |---------------------------------|-----------------------------------------|
 * | 订阅计数轮询（有订阅者才轮询）     | refetchInterval（observer 级，末订阅者离开即停）|
 * | in-flight 去重                  | query 级去重（同 key 自动合并）          |
 * | TTL                             | staleTime                               |
 * | invalidateAll / invalidateKey   | invalidateQueries({ queryKey }) 前缀批量 |
 * | 惰性淘汰（REAP_GRACE_MS=100）    | gcTime: 5min（放宽保留，重订阅可命中） |
 * | visibilitychange 停表            | 默认 + refetchIntervalInBackground      |
 * | slowWhen 非交易降频              | pollInterval 函数式闭包（读取 isCnTradingSession）|
 * | 轮询指数退避（封顶 5min）         | pollInterval 内基于 fetchFailureCount 计算 |
 *
 * QueryClient 单例：全站唯一实例——main.tsx 的 Provider 与各 data 模块的
 * invalidateXxx 共用同一实例（若在 main.tsx 内 new 会形成 main→App→data→main
 * 的 import 环，故客户端单例落在本叶模块）。
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,                 // TTL（各池按需覆盖）
      gcTime: 5 * 60_000,                 // 惰性淘汰：订阅归零后保留 5min（放宽自研 REAP_GRACE_MS=100ms，重订阅命中缓存防连击重建）
      refetchIntervalInBackground: false, // visibility 停表（默认行为）
      retry: 2,                          // 失败重试（08-roadmap 接受：弱网失败重试频率略升，指数退避补充防御）
    },
  },
})

/** 查询完整状态（与自研池 QueryState 同形状：value/loading/error/fetchedAt，消费方零改动） */
export interface QueryState<T> {
  value: T | undefined
  loading: boolean
  error: unknown
  fetchedAt: number
}

/** 轮询失败指数退避上限：5min（对齐自研 POLL_MAX_BACKOFF） */
export const POLL_MAX_BACKOFF = 300_000

export interface PollIntervalOpts {
  /** 慢速轮询条件（如非交易时段）：为真时基础间隔改用 slowPollMs（缺省 pollMs×5）；每次调度重新求值 */
  slowWhen?: () => boolean
  slowPollMs?: number
}

/** 函数式 refetchInterval 签名（仅依赖 query.state.fetchFailureCount，结构类型避免引 Query 全类型） */
export type PollIntervalFn = (query: { state: { fetchFailureCount: number } }) => number | false

/**
 * 轮询调度（对齐自研 schedulePoll）：
 * 基础间隔 = slowWhen ? (slowPollMs ?? pollMs×5) : pollMs；
 * 连续失败 n 次 → 基础×2^n，封顶 5min。失败计数取 TanStack query.state.fetchFailureCount——
 * 源码级确认（query-core 5.101.4）：每次 fetch 开始复位为 0、失败 +1、成功归 0，
 * 恰为自研 pollFailStreak 语义；observer 在每次 fetch 结算后（onQueryUpdate → updateTimers）
 * 重新求值本函数，故退避/降频在下一调度生效。
 * 差异记账：初始（订阅首拉）失败即计入退避，自研首拉失败不计数——退避起点早一步，可接受。
 */
export function pollInterval(pollMs: number, opts: PollIntervalOpts = {}): PollIntervalFn {
  return (query) => {
    const slow = opts.slowWhen?.() ?? false
    const base = slow ? (opts.slowPollMs ?? pollMs * 5) : pollMs
    const fails = query.state.fetchFailureCount
    return Math.min(base * 2 ** fails, POLL_MAX_BACKOFF)
  }
}

export interface QueryStateOptions<T> {
  queryKey: QueryKey
  queryFn: () => Promise<T>
  /** false = 不订阅、不发请求、不轮询（等价自研空 store 语义：code/params 为空时避免哨兵请求） */
  enabled?: boolean
  staleTime?: number
  refetchInterval?: number | false | PollIntervalFn
}

/** TanStack 结果 → 自研 QueryState 形状：loading = 在途即 true（含轮询刷新，与自研 loading 语义一致） */
function mapResult<T>(q: UseQueryResult<T, Error>): QueryState<T> {
  return {
    value: q.data,
    loading: q.isFetching,
    error: q.error ?? undefined,
    fetchedAt: q.dataUpdatedAt,
  }
}

/** 订阅完整状态（SWR + TTL + 有订阅者才轮询），自研 useQueryState 的 TanStack 版 */
export function useQueryState<T>(opts: QueryStateOptions<T>): QueryState<T> {
  const q = useQuery<T, Error>({
    queryKey: opts.queryKey,
    queryFn: opts.queryFn,
    enabled: opts.enabled,
    staleTime: opts.staleTime,
    refetchInterval: opts.refetchInterval,
  })
  return mapResult(q)
}

/** 仅取最新值（自研 useQueryValue 语义；错误/加载态走 useXxxState） */
export function useQueryValue<T>(opts: QueryStateOptions<T>): T | undefined {
  return useQueryState(opts).value
}

// ===== sr-refresh 广播联动（顶栏刷新按钮 / 命令面板 dispatch，MainLayout） =====
// 保留 window 事件机制，监听器收敛为单点：queryClient.invalidateQueries() 全前缀失效
// （替代原 market/kline/jobs/indicators 各自注册的池失效监听器；disabled 查询与无观察者
// 查询不会被重拉——refetchQueries 过滤 isDisabled，等价自研「有订阅者才重拉」）。
// 页面级监听器（SignalCenter 无限列表重置 / WatchlistPage 页面清理 / useWorkbenchData 重载当前股）
// 属页面领地，不在此收敛。
if (typeof window !== 'undefined') {
  window.addEventListener('sr-refresh', () => queryClient.invalidateQueries())
}
