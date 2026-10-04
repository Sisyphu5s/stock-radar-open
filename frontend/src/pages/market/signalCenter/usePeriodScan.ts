import { useCallback, useEffect, useRef, useState } from 'react'
import { notifications } from '@mantine/notifications'
import { api, getJob, getScanStatus, triggerSignalScan } from '../../../api/client'
import type { ScanUniverse } from '../../../api/client'
import { invalidateScanStatus, useScanStatus } from '../../../data/scanStatus'
import { toMarketEpochMs } from '../../../utils/time'
import { guardedInterval } from '../../../utils/guardedInterval'
import { isMinutePeriod, periodLabel } from '../../../utils/periods'
import { useLatestRef, useStableSetter } from '../../../hooks/useStableSetter'
import { usePersistentState } from '../../../utils/stateMemory'

// ===== 通知（@mantine/notifications；模块级单例，不参与 useCallback 依赖，与页面级模式一致） =====
const notifySuccess = (message: string) => notifications.show({ color: 'green', message })
const notifyWarning = (message: string) => notifications.show({ color: 'yellow', message })
const notifyError = (message: string) => notifications.show({ color: 'red', message })

// 扫描新鲜度阈值回退（后端 scan_status.schedule 未返回/拉取失败时）：5min，与旧行为一致
const FALLBACK_INTERVAL_SEC = 5 * 60

// T-76 手动触发冷却（ms）：两次「立即扫描」最小间隔（120s，与自动重触发节流同频）——
// 仅约束手动触发（自动触发有新鲜度判定天然低频）；冷却中/扫描进行中不重复 POST
const MANUAL_SCAN_COOLDOWN_MS = 120_000

export interface UsePeriodScanOptions {
  /** 当前周期：非日线周期变化 → 检查新鲜度并自动触发；离开计算中周期 → 终止跟踪 */
  period: string
  /** 扫描完成回调（页面统一刷新事件列表：无限滚动从第 1 批重建）；hook 内部经 latest-ref 保持最新 */
  onScanDone: () => void
}

export interface PeriodScanApi {
  scanBusy: boolean
  /** 扫描任务进度百分比（0-100；经 GET /experiments/{job_id} 轮询 market_scan job 获取，
   *  未拿到时 null → 前端用确定进度条 + 阶段文案兜底） */
  scanProgress: number | null
  /** 各周期最近一次扫描时刻快照（轮询/触发共用） */
  periodScanAt: Record<string, string | null>
  /** 触发指定周期全市场扫描计算（POST /signals/scan；分钟周期携带 scanUniverse）；
   *  执行期间 scanBusy 置 true，结束后刷新 periodScanAt */
  triggerScan: (period: string) => Promise<boolean>
  /** 取消当前扫描（DELETE /experiments/{job_id}，后端协作取消） */
  cancelScan: () => void
  /** 指定周期是否已有进行中的扫描（恒稳回调；页面 reload 据此避免与自动触发重复 POST） */
  isScanPending: (period: string) => boolean
  /** T-76 手动触发「立即扫描」：绕过新鲜度判定直接触发当前周期扫描，携带当前 universe；
   *  遵守手动触发冷却（120s）+ 扫描互斥（进行中不重复 POST），冲突时由本函数提示并返回 false；
   *  成功触发返回 true（调用方补发 ETA 文案）。复用 triggerScan 既有 busy/进度/完成判定链路 */
  manualTriggerScan: () => Promise<boolean>
  /** T-54 分钟扫描范围（持久化 sr-scan-universe；分钟周期生效，驱动扫描触发与口径文案） */
  scanUniverse: ScanUniverse
  /** 设置扫描范围（写时归一化：非 top_n 一律 watchlist） */
  setScanUniverse: (v: ScanUniverse) => void
}

/**
 * 非日线周期扫描调度（P2-19 自 SignalCenter.tsx 抽出 + T-54 扩展）：
 * 自动触发（周期变化检查新鲜度）+ 轮询等待完成（scan_status 池订阅 + job 进度轮询）。
 * data/ 查询池调用（scanStatus.ts 共享池 / api client）保持原样，本 hook 只做编排。
 * T-54：分钟周期扫描范围 universe（watchlist|top_n）随 triggerScan 携带；
 * 新鲜度阈值 = 后端调度表该周期间隔（单一事实源 Settings.scan_schedule），
 * 后端不返回时回退 5min 常量（FALLBACK_INTERVAL_SEC）。
 */
export function usePeriodScan({ period, onScanDone }: UsePeriodScanOptions): PeriodScanApi {
  // ===== 状态 =====
  const [scanBusy, setScanBusy] = useState(false)
  const [scanProgress, setScanProgress] = useState<number | null>(null)
  const [periodScanAt, setPeriodScanAt] = useState<Record<string, string | null>>({})
  // 扫描时刻最新快照（轮询/触发共用；state 异步，触发时读 ref 拿基线）
  const periodScanAtRef = useLatestRef(periodScanAt)
  // T-54 分钟扫描范围（持久化 sr-scan-universe；读时校验，旧值/非法值归一为 watchlist）
  const [scanUniverseRaw, setScanUniverseRaw] = usePersistentState<ScanUniverse>('sr-scan-universe', 'watchlist')
  const scanUniverse: ScanUniverse = scanUniverseRaw === 'top_n' ? 'top_n' : 'watchlist'
  const setScanUniverseSt = useStableSetter(setScanUniverseRaw)
  const setScanUniverse = useCallback((v: ScanUniverse) => {
    setScanUniverseSt(v === 'top_n' ? 'top_n' : 'watchlist')
  }, [setScanUniverseSt])
  // 触发时读最新范围（triggerScan 依赖 [] 恒稳）
  const scanUniverseRef = useLatestRef(scanUniverse)
  // 扫描调度表（T-54）：scan_status.schedule（周期 → 间隔秒）ref 保存——新鲜度阈值按周期取间隔，
  // 不走 state，避免 30s 池刷新产生新对象引用触发依赖 effect 循环
  const scanScheduleRef = useRef<Record<string, number>>({})
  // 触发中任务：{ period 计算中周期, baseline 触发前该周期扫描时刻, jobId 后端任务 id }；
  // 计算完成后清除。同时充当「已为该 period 触发过且仍在计算」标记——自动触发/刷新按钮据此互斥，防重复 POST
  const pendingScanRef = useRef<{ period: string; baseline: string | null; jobId: number | null } | null>(null)
  // T-76 手动触发冷却：上次手动触发时刻（epoch ms；自动触发不走此冷却，保持既有新鲜度语义）
  const lastManualAtRef = useRef(0)
  // 完成回调 latest-ref：hook 内恒稳回调（finishScan / 轮询 effect）读取最新 onScanDone
  const onScanDoneRef = useLatestRef(onScanDone)

  // ===== 工具 =====
  // 扫描状态刷新（失败返回 null 供调用方区分「拉取失败」与「从未扫描」）：
  // 返回最新 periods 快照，供调用方判断陈旧度（state 异步，返回值为准）；
  // 同步把 schedule 调度表写进 ref（T-54，供自动触发新鲜度阈值按周期取间隔）
  const refreshScanStatus = useCallback(async (): Promise<Record<string, string | null> | null> => {
    try {
      const s = await getScanStatus()
      setPeriodScanAt(s.periods ?? {})
      scanScheduleRef.current = s.schedule ?? {}
      return s.periods ?? {}
    } catch {
      return null
    }
  }, [])

  // 触发指定周期全市场扫描：记录触发前基线（完成判定据此判断）+ job_id（进度轮询/取消用）；
  // POST 成功即失效池强制重拉一次；POST 失败立即收尾并提示，返回 false（手动触发据此不补发 ETA）。
  // T-129:每次触发重置探测/重试计数（跨任务不累积，后续扫描不再被历史计数过早终止）。
  const triggerScan = useCallback(async (p: string): Promise<boolean> => {
    setScanBusy(true)
    setScanProgress(null)
    scanProbeCountRef.current = 0
    scanRetryCountRef.current = 0
    pendingScanRef.current = { period: p, baseline: periodScanAtRef.current[p] ?? null, jobId: null }
    try {
      const res = await triggerSignalScan(p, isMinutePeriod(p) ? { universe: scanUniverseRef.current } : {})
      const cur = pendingScanRef.current
      // 同一 pending 才回填 jobId（期间可能被取消/周期切换清空）
      if (cur && cur.period === p) cur.jobId = res.job_id != null ? Number(res.job_id) : null
      // 失效扫描状态池 → 立即重拉最新快照（不等 30s 轮询），完成判定建立在池最新值上
      invalidateScanStatus()
      return true
    } catch (e: any) {
      pendingScanRef.current = null
      setScanBusy(false)
      setScanProgress(null)
      notifyError('触发计算失败: ' + (e?.message ?? e))
      return false
    }
  }, [])

  // T-76 手动触发「立即扫描」（「立即扫描」按钮）：绕过新鲜度判定直接触发当前周期，
  // 复用 triggerScan（POST /signals/scan 携带当前 universe + 完成判定/进度轮询链路）；
  // 手动触发冷却 120s 防高频 POST，扫描互斥（pendingScanRef 同步 ref）防重复触发。
  // 冲突时提示并返回 false，不重复 POST；成功返回 true（调用方补发 ETA 文案）。
  // T-129:POST 失败不再伪装成功（triggerScan 返回实际结果，失败不补发 ETA）
  const manualTriggerScan = useCallback(async (): Promise<boolean> => {
    if (pendingScanRef.current) {
      notifyWarning('已有扫描任务在进行中，请稍候')
      return false
    }
    const leftMs = lastManualAtRef.current + MANUAL_SCAN_COOLDOWN_MS - Date.now()
    if (leftMs > 0) {
      notifyWarning(`扫描触发过于频繁，请 ${Math.ceil(leftMs / 1000)} 秒后重试`)
      return false
    }
    lastManualAtRef.current = Date.now()
    return triggerScan(period)
  }, [triggerScan, period])

  // 扫描任务收尾（完成/失败/取消共用）：清 pending 与 busy/进度；done=true 时重拉事件列表
  // （页面无限重置从第 1 批重建）+ 完成轻提示
  const finishScan = useCallback((done: boolean) => {
    const p = pendingScanRef.current
    pendingScanRef.current = null
    setScanBusy(false)
    setScanProgress(null)
    if (done) {
      if (p) notifySuccess(`${periodLabel(p.period)} 扫描完成，列表已刷新`)
      onScanDoneRef.current()
    }
  }, [])

  // 取消当前扫描（DELETE /experiments/{job_id}）：后端协作取消——scan_once 的进度回调即协作
  // 控制点（_checkpoint 检测 _cancelled → 抛 JobCancelled 中止）；取消后 periods 不更新，主动收尾
  // （不重拉，事件列表保持现状；任务可能还需一小段时间停在后台控制点）
  const cancelScan = useCallback(() => {
    const p = pendingScanRef.current
    if (!p) return
    if (p.jobId != null) {
      api.delete(`/experiments/${p.jobId}`).catch((e: any) => {
        notifyError('取消失败: ' + (e?.message ?? e))
      })
    }
    finishScan(false)
  }, [finishScan])

  // 是否已有指定周期进行中的扫描（恒稳回调：页面 reload 据此避免与自动触发重复 POST）
  const isScanPending = useCallback((period: string) => pendingScanRef.current?.period === period, [])

  // ===== 调度 =====
  // scanStatus 全局池订阅（30s SWR + 有订阅者时 30s 轮询；SignalStreamPanel 同池，全站只发一份请求）
  const scanStatus = useScanStatus() ?? null
  // 池刷新计数（完成判定/无进展重触发节奏基于池刷新次数：4 次 × 30s = 120s）
  const scanProbeCountRef = useRef(0)
  const scanRetryCountRef = useRef(0)

  // 扫描完成判定（建立在池最新值上，无双路直连）：scanBusy 期间监听 scanStatus 变化，
  // periods[p] 非空且 ≠ 基线 → 完成：停跟踪、清标记、重拉事件列表（分页全量 / 无限重建）。
  // 无进展自动重触发：调度扫描占用互斥锁（busy）时任务可能静默丢失，等待将永不完成——
  // 每 4 次池刷新（120s）且任务仍挂起 → 重新 POST 触发一次（baseline 取 periodScanAtRef 当前值，
  // periods 未更新则 baseline 不变，完成判定语义不变）；重触发上限 5 次仍无更新 → 停止（静默：
  // 后台任务可能仍在跑，用户可再点刷新）；总上限 120 次池刷新（约 1h）后停止。卸载自动退订。
  useEffect(() => {
    if (!scanBusy) return
    const p = pendingScanRef.current
    if (!p) return
    const cur = scanStatus?.periods?.[p.period] ?? null
    if (scanStatus) setPeriodScanAt(scanStatus.periods ?? {})
    if (cur && cur !== p.baseline) {
      finishScan(true)
      return
    }
    scanProbeCountRef.current++
    if (scanProbeCountRef.current % 4 === 0 && pendingScanRef.current) {
      if (scanRetryCountRef.current >= 5) {
        finishScan(false)
      } else {
        scanRetryCountRef.current++
        // 重触发保持原 pending（baseline 不变），triggerScan 内部会 setScanBusy(true)（幂等）；
        // 仅 120s 一次，避免 POST 风暴
        void triggerScan(p.period)
      }
    }
    if (scanProbeCountRef.current >= 120) {
      finishScan(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scanBusy, scanStatus, finishScan])

  // 扫描任务进度轮询（5s GET /experiments/{job_id}）：scanBusy 且 jobId 已知时启动；
  // 更新进度条百分比；任务进入终态（done/failed/cancelled）→ 提前收尾——正常完成 periods 会更新
  // （上方池判定兜底），取消/失败路径 periods 可能不更新，靠此收尾避免挂满 120 次池刷新
  useEffect(() => {
    if (!scanBusy) return
    const p = pendingScanRef.current
    if (!p || p.jobId == null) return
    let disposed = false
    const poll = async () => {
      try {
        // 每次读取最新 pending（重触发会替换对象并回填新 jobId），不闭包捕获旧任务
        const cur = pendingScanRef.current
        if (!cur || cur.jobId == null) return
        const j = await getJob(cur.jobId)
        if (disposed || pendingScanRef.current !== cur) return
        if (typeof j.progress === 'number') setScanProgress(j.progress)
        if (j.status === 'done' || j.status === 'failed' || j.status === 'cancelled') {
          // T-76 后端 busy 检测：任务受理但 scan_once 拿不到扫描锁（source='busy'，另一轮
          // 扫描在进行）→ 未真正计算。按未完成收尾（不重拉、不提示「扫描完成」），明确提示。
          if (j.status === 'done' && j.result?.scan?.source === 'busy') {
            notifyWarning('已有其他扫描在进行，本次触发未执行，可在扫描结束后重试')
            finishScan(false)
            return
          }
          // T-129:source=error/empty/no-watchlist/no-codes 均非「真实计算完成」——
          // 原实现一律按成功收尾并播报「扫描完成」，误导用户
          if (j.status === 'done' && j.result?.scan?.source) {
            const src = j.result.scan.source
            if (src === 'error') {
              notifyError('扫描执行出错，未产生新信号，请稍后重试')
              finishScan(false)
              return
            }
            if (src === 'empty' || src === 'no-watchlist' || src === 'no-codes') {
              notifyWarning('扫描完成但未产生新信号（' + src + '）')
              finishScan(false)
              return
            }
          }
          finishScan(j.status === 'done')
        }
      } catch { /* 轮询失败静默：进度兜底为确定条，收尾仍由池判定 */ }
    }
    void poll()
    // 5s 进度轮询走 guardedInterval：后台隐藏跳过 tick（进度条不可见），回前台续上
    const timer = guardedInterval(() => { void poll() }, 5000)
    return () => { disposed = true; clearInterval(timer) }
  }, [scanBusy, finishScan])

  // 周期变化：非日线周期检查扫描新鲜度（从未扫描/超过该周期调度间隔 → 自动触发全市场计算）；
  // 状态拉取失败不自动触发（可手动刷新）；同一周期已在计算中则跳过（防与刷新按钮重复 POST）。
  // T-54 新鲜度阈值 = 后端调度表该周期间隔（scanScheduleRef，单一事实源 Settings.scan_schedule）；
  // 后端不返回（旧后端/拉取失败）时回退 FALLBACK_INTERVAL_SEC 常量
  useEffect(() => {
    if (period === 'daily') return
    let cancelled = false
    void (async () => {
      const periods = await refreshScanStatus()
      if (cancelled) return
      // T-129:状态拉取失败(null)不自动触发——原实现把失败当成「从未扫描」自动 POST
      if (periods === null) return
      const lastScan = periods[period] ?? null
      const intervalSec = scanScheduleRef.current[period] ?? FALLBACK_INTERVAL_SEC
      // 新鲜（≤调度间隔）不触发；从未扫描（null）或过期（toMarketEpochMs 容错 0 → 恒不新鲜）→ 触发
      const fresh = lastScan != null && Date.now() - toMarketEpochMs(lastScan) <= intervalSec * 1000
      if (fresh) return
      if (pendingScanRef.current?.period === period) return
      void triggerScan(period)
    })()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [period])

  // 周期切换离开计算中周期 → 终止该周期跟踪并清标记（新周期自动触发由上方 effect 负责）
  useEffect(() => {
    const p = pendingScanRef.current
    if (p && p.period !== period) {
      pendingScanRef.current = null
      setScanBusy(false)
    }
  }, [period])

  return {
    scanBusy, scanProgress, periodScanAt,
    triggerScan, cancelScan, isScanPending, manualTriggerScan,
    scanUniverse, setScanUniverse,
  }
}
