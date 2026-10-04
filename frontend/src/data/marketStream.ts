import { queryClient } from './queryBase'
import type { WatchlistItem } from '../api/client'
import type { MarketStreamEvent, StreamQuote } from '../api/marketStream'

/**
 * 行情推送驱动(SSE 单例):connect/disconnect 引用计数,任一订阅者存在即保持连接;
 * visibilitychange 隐藏时暂停(close),恢复可见且仍有订阅者时立即重连;
 * 断线指数退避重连 1s→2s→…→30s 封顶(接管浏览器固定 ~3s 自动重连,防死循环)。
 *
 * 事件直灌缓存(不依赖轮询时效):
 * - snapshot/tick → setQueryData(['mkt','quote',code]) 与 ['mkt','watchlist'] 相关条目;
 * - signal → invalidateQueries(['mkt','events'])(重新拉取事件流)。
 *
 * 轮询保留兜底:推送活跃时 data/market.ts 的池 interval 放宽到 5min,断开回 60s。
 * 挂载点:本模块只提供 connect/disconnect,由消费方(页面/布局)按需接入。
 */

const STREAM_URL = '/api/v1/market/stream'
const RETRY_BASE_MS = 1_000
const RETRY_MAX_MS = 30_000

/** 重连退避:第 attempt 次重连的等待毫秒(1s 起指数翻倍,30s 封顶)。纯函数,便于单测/审查。 */
export function nextStreamRetryDelayMs(attempt: number): number {
  return Math.min(RETRY_BASE_MS * 2 ** Math.max(0, attempt), RETRY_MAX_MS)
}

let es: EventSource | null = null
let refCount = 0
let retryAttempt = 0
let retryTimer: ReturnType<typeof setTimeout> | null = null

/** 推送是否活跃(连接已建立且处于 OPEN 态;data/market.ts 据此放宽轮询间隔) */
export function isMarketStreamActive(): boolean {
  return es !== null && es.readyState === EventSource.OPEN
}

/** 订阅行情推送(引用计数 +1;首个订阅者触发建连) */
export function connectMarketStream(): void {
  refCount += 1
  open()
}

/** 退订行情推送(引用计数 -1;归零时关闭连接并清空退避计时) */
export function disconnectMarketStream(): void {
  if (refCount > 0) refCount -= 1
  if (refCount === 0) {
    if (retryTimer !== null) {
      clearTimeout(retryTimer)
      retryTimer = null
    }
    es?.close()
    es = null
  }
}

/** 页面是否隐藏(SSR/非浏览器环境视为不隐藏,避免引用未定义 document) */
function isHidden(): boolean {
  return typeof document !== 'undefined' && document.hidden
}

function open(): void {
  if (refCount <= 0 || es !== null || isHidden()) return
  es = new EventSource(STREAM_URL)
  es.onopen = () => {
    retryAttempt = 0 // 连接成功,退避归零
  }
  es.onerror = () => {
    // 浏览器 EventSource 断线会自动重连;手动 close 接管为指数退避(防固定 ~3s 死循环)
    es?.close()
    es = null
    scheduleReconnect()
  }
  es.addEventListener('snapshot', onEvent)
  es.addEventListener('tick', onEvent)
  es.addEventListener('signal', onEvent)
}

function scheduleReconnect(): void {
  if (refCount <= 0 || isHidden()) return
  if (retryTimer !== null) return // 已排程,防重复
  const delay = nextStreamRetryDelayMs(retryAttempt)
  retryAttempt += 1
  retryTimer = setTimeout(() => {
    retryTimer = null
    open()
  }, delay)
}

function onEvent(ev: Event): void {
  let parsed: MarketStreamEvent
  try {
    parsed = JSON.parse((ev as MessageEvent).data) as MarketStreamEvent
  } catch {
    return // 坏数据跳过(心跳 comment 不走 addEventListener,不会到这)
  }
  if (!parsed || typeof parsed.type !== 'string') return
  if (parsed.type === 'signal') {
    // 新信号 → 事件流失效重拉(SSE 只负责时效通知,数据以轮询/重拉为准)；
    // T-129:信号中心主列表为手写 offset 状态机(不订阅 Query 池),经窗口事件桥接触发无限重置
    queryClient.invalidateQueries({ queryKey: ['mkt', 'events'] })
    window.dispatchEvent(new CustomEvent('sr-signal-event'))
    return
  }
  applyQuotes(parsed.items)
}

/** 直灌 quote/watchlist 缓存:仅更新已存在缓存(未订阅的 key 不创建,轮询兜底) */
function applyQuotes(items: StreamQuote[]): void {
  if (!items || items.length === 0) return
  for (const q of items) {
    const key = ['mkt', 'quote', q.code] as const
    const old = queryClient.getQueryData<Record<string, unknown>>(key)
    if (old === undefined) continue
    // 以 SSE 实时字段覆盖,保留轮询缓存的其余字段(industry/source 等)
    queryClient.setQueryData(key, { ...old, ...q })
  }
  // watchlist 缓存条目更新(仅更新已存在条目,保持轮询结果完整性;新对象触发结构共享)
  queryClient.setQueryData<WatchlistItem[]>(['mkt', 'watchlist'], (prev) => {
    if (!prev || prev.length === 0) return prev
    const byCode = new Map(items.map((i) => [i.code, i]))
    let changed = false
    const next = prev.map((w) => {
      const q = byCode.get(w.code)
      if (!q) return w
      changed = true
      return {
        ...w,
        last_price: q.price,
        pct_change: q.pct_change,
        updated_at: q.timestamp ?? w.updated_at,
      }
    })
    return changed ? next : prev
  })
}

// visibilitychange 暂停/恢复(与 queryBase refetchIntervalInBackground: false 同语义):
// 页面隐藏时推送无展示价值 → 断开;恢复可见且有订阅者时立即重连(退避归零)。
if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      es?.close()
      es = null
    } else if (refCount > 0 && es === null) {
      retryAttempt = 0
      open()
    }
  })
}
