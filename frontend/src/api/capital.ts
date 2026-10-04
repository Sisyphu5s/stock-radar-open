/**
 * 资金类 API 契约（T-08 个股资金流 / 龙虎榜 / 两融 / 北向历史持股）。
 *
 * 独立于 client.ts（并行任务领地隔离）：类型 + fetch 函数集中于本文件，
 * 数据层（data/capital.ts）与工作台「资金」section 从此处导入。
 * 后端契约：GET /stocks/{code}/capital/history?type=…&limit=… 与
 * GET /stocks/{code}/capital/latest（详见 API-CONTRACT.md 主会话登记）。
 */

import { api } from './client'

/** 资金类类型（GET /stocks/{code}/capital/history type 参数） */
export type CapitalType = 'moneyflow' | 'lhb' | 'margin' | 'northbound'

/** 资金类历史单行：date + 类型字段（金额单位元；北向持股量=股、占比=百分数值） */
export interface CapitalHistoryRow {
  date: string
  /** moneyflow: 主力净流入(元) */
  main_net?: number
  super_net?: number
  large_net?: number
  medium_net?: number
  small_net?: number
  /** lhb: 上榜原因/解读 */
  reason?: string
  summary?: string
  buy_amount?: number
  sell_amount?: number
  net_amount?: number
  deal_amount?: number
  turnover_rate?: number
  /** margin: 融资余额(元) / 融券余额(元, 沪市无) / 融资净买入(元, 余额环比) */
  margin_balance?: number
  short_balance?: number
  net_buy?: number
  /** northbound: 持股数量(股) / 持股占A股比例(%) */
  hold_shares?: number
  hold_ratio?: number
}

/** GET /stocks/{code}/capital/history 响应：data 恒为日期倒序（新在前） */
export interface CapitalHistoryResponse {
  code: string
  type: string
  data: CapitalHistoryRow[]
}

/** 资金类历史（日期倒序）；后端 SQLite 缓存，网络失败降级返回库内数据（北向失效 503） */
export async function getCapitalHistory(code: string, type: CapitalType, limit = 60): Promise<CapitalHistoryResponse> {
  const { data } = await api.get(`/stocks/${code}/capital/history`, { params: { type, limit } })
  return data
}

/** 资金类各类型最新一行合并摘要（GET /stocks/{code}/capital/latest）；各类型无数据为 null */
export interface CapitalLatest {
  code: string
  moneyflow?: CapitalHistoryRow | null
  lhb?: CapitalHistoryRow | null
  margin?: CapitalHistoryRow | null
  northbound?: CapitalHistoryRow | null
}

export async function getCapitalLatest(code: string): Promise<CapitalLatest> {
  const { data } = await api.get(`/stocks/${code}/capital/latest`)
  return data
}
