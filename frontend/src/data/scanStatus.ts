import { getScanStatus } from '../api/client'
import type { ScanStatus, ScanUniverse } from '../api/client'
import { pollInterval, queryClient, useQueryValue } from './queryBase'
import { isCnTradingSession } from '../utils/time'
import { isMinutePeriod } from '../utils/periods'

/**
 * 扫描状态共享查询：30s SWR + 有订阅者时 30s 轮询，全站只发一份请求。
 * 非交易时段降频至 5min 轮询（收盘后无增量扫描，日线扫描语义低频足够）。
 * 原 SignalStreamPanel 组件内 setInterval 轮询与 SignalCenter 触发流程各自拉取，
 * 现统一收敛到本单例——多消费方（新鲜度条 / 触发流程手动刷新）复用同一份缓存与轮询。
 * 轮询失败静默保留旧值（查询层语义），首次失败无值返回 undefined。
 */
function scanStatusQuery() {
  return {
    queryKey: ['sys', 'scan-status'],
    queryFn: () => getScanStatus(),
    staleTime: 30_000,
    refetchInterval: pollInterval(30_000, { slowWhen: () => !isCnTradingSession(), slowPollMs: 300_000 }),
  }
}

/** 订阅扫描状态（undefined = 未加载/首次加载失败） */
export function useScanStatus(): ScanStatus | undefined {
  return useQueryValue(scanStatusQuery())
}

/** 手动失效并立即重拉（SignalCenter 触发扫描完成后调用，不等 30s 轮询 / 收盘后 5min 慢速轮询） */
export function invalidateScanStatus() {
  queryClient.invalidateQueries({ queryKey: ['sys', 'scan-status'] })
}

// ===== 扫描覆盖口径文案（T-54：随 universe+周期动态生成，不再手写分支） =====
// 后端 scan_once 分支语义：
// - daily：成交额聚焦 Top N + 自选池（雷达快照仍覆盖全市场；full_universe=true）
// - weekly/monthly：全量快照全部股票（不按成交额截断）
// - 分钟（1/5/15/30/60）：universe 可配置——watchlist=自选（缺省）/ top_n=成交额前 N
//   / codes=指定代码；前端 UI 暴露 watchlist/top_n 两项，codes 供后端直连等其他客户端
const SCAN_COVERAGE: Record<string, string> = {
  daily: '覆盖：成交额 Top N + 自选',
  weekly: '覆盖：全市场',
  monthly: '覆盖：全市场',
}
const MINUTE_COVERAGE: Record<ScanUniverse, string> = {
  watchlist: '覆盖：仅自选（关注列表）',
  top_n: '覆盖：成交额 Top N',
  codes: '覆盖：指定代码',
}

/** 指定周期的扫描覆盖口径文案（分钟周期按 universe 动态取表，其余按表查询；未知周期兜底全市场） */
export function scanCoverageText(period: string, universe: ScanUniverse = 'watchlist'): string {
  if (isMinutePeriod(period)) return MINUTE_COVERAGE[universe]
  return SCAN_COVERAGE[period] ?? '覆盖：全市场'
}
