/**
 * 行情 SSE 事件类型(与后端 GET /api/v1/market/stream 契约对齐;client.ts 不改,
 * 类型放本模块作为单一事实源)。
 *
 * 事件协议:
 * - event: snapshot  全量子集快照(关注列表+沪深300),首连/断线重连后经 ring 速发;
 * - event: tick      增量(阈值过滤后的变化行:涨跌 >0.1% 或量能变化 >5%);
 * - event: signal    自选股新信号事件增量(复用信号事件流字段契约);
 * - 15s 心跳 comment(: heartbeat),前端不消费。
 */

/** 单条行情(与 GET /stocks/{code}/quote 响应字段同形状,便于直灌 quote 缓存) */
export interface StreamQuote {
  code: string
  name?: string
  price: number
  pct_change: number
  volume?: number
  amount?: number
  turnover_rate?: number
  industry?: string
  source?: string
  /** 服务器上海时刻 naive ISO(秒级) */
  timestamp?: string
}

/** 快照事件:全量子集,data 含 type/ts/source/count/items */
export interface StreamSnapshotEvent {
  type: 'snapshot'
  ts: number
  source?: string
  count?: number
  items: StreamQuote[]
}

/** 增量事件:data = { type, ts, items[] } */
export interface StreamTickEvent {
  type: 'tick'
  ts: number
  items: StreamQuote[]
}

/** 新信号事件:字段契约同 GET /market/watchlist/notifications 返回项 */
export interface StreamSignalEvent {
  type: 'signal'
  ts: number
  events: Array<{
    id: number
    stock_code: string
    stock_name?: string
    signals?: string[]
    status?: string
    triggered_at?: string | null
    as_of?: string | null
    scan_discovered_at?: string | null
    period?: string
  }>
}

export type MarketStreamEvent = StreamSnapshotEvent | StreamTickEvent | StreamSignalEvent
