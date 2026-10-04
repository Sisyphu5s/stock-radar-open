import { notifications } from '@mantine/notifications'
import { useCallback, useEffect, useRef, useState } from 'react'
import { cancelTuneJob, getIndicatorCatalog, getIndicatorParams, saveIndicatorParams, resetIndicatorParams, tuneIndicator, tuneIndicatorsCombined, waitForJob } from '../../../api/client'
import type { CombinedTuneResponse, IndicatorParamValues, IndicatorSpec, IndicatorStoredParams, TuneResponse } from '../../../api/client'
import { errMsg } from '../../../utils/format'
import { invalidateIndicators } from '../../../data/indicators'
import { TUNE_LABEL } from './context'
import { TUNE_OPTIONS_FALLBACK, loadManual, loadTuneScope, migrateLegacyManual, persistManual, persistTuneScope } from './persist'
import type { TuneScope } from './persist'

/** 评估周期默认值（InputNumber 空态回退） */
const TUNE_HORIZON_DEFAULT = 5

/** useTuneFlow 依赖注入（契约域/手动覆盖域由 useWorkbenchData 持有，经参数传入；禁止反向 import useWorkbenchData） */
export interface TuneFlowDeps {
  code: string
  /** 契约加载 → 写回契约域（specs/defaults/global/backend/effective/manual） */
  onContractLoaded: (data: {
    specs: Record<string, IndicatorSpec>
    defaults: Record<string, IndicatorParamValues>
    global: Record<string, IndicatorStoredParams>
    backendEffective: Record<string, IndicatorParamValues>
    manual: Record<string, IndicatorParamValues>
  }) => void
  /** 调优结果应用（chart 分支）：合并写入手动覆盖 + 生效参数 + 失效指标池 */
  applyManual: (paramsByInd: Record<string, IndicatorParamValues>) => void
  /** 清空手动覆盖并回退后端生效参数 + 失效指标池；返回被清除的指标 key 列表（clearTune manual 分支） */
  clearManualAll: () => string[]
  /** 保存/重置全局默认后刷新契约（specs/global/effective） */
  refreshIndicatorParams: () => Promise<void>
  /** 已覆盖系统默认的指标 key 列表（clearTune global 分支） */
  tunedInds: string[]
}

export interface TuneFlowApi {
  tuneOpen: boolean
  setTuneOpen: (v: boolean) => void
  tuneInd: string
  setTuneInd: (v: string) => void
  tuneHorizon: number | null
  setTuneHorizon: (v: number | null) => void
  tuneTarget: string
  setTuneTarget: (v: string) => void
  tuneAlgorithm: 'grid' | 'random' | 'fast'
  setTuneAlgorithm: (v: 'grid' | 'random' | 'fast') => void
  tuneOptions: { value: string; label: string }[]
  tuneCatalogErr: string | null
  tuneContractErr: string | null
  retryTuneContract: () => void
  tuneInds: string[]
  setTuneInds: (v: string[]) => void
  tuneMode: 'individual' | 'combined'
  setTuneMode: (v: 'individual' | 'combined') => void
  tuneScope: TuneScope
  setTuneScope: (v: TuneScope) => void
  tuneResults: Record<string, TuneResponse | CombinedTuneResponse>
  tuneErrors: Record<string, string>
  tuneProgress: { current: string; done: number; total: number } | null
  tuning: boolean
  cancelling: boolean
  runTune: () => Promise<void>
  retryTune: (key: string) => Promise<void>
  cancelTune: () => void
  applyBest: (key?: string) => Promise<void>
  applyAllBest: () => Promise<void>
  clearTune: (scope: 'manual' | 'global' | 'all') => Promise<{ ok: string[]; fail: string[] }>
  saveGlobalParams: (ind: string, values: IndicatorParamValues) => Promise<void>
  resetGlobalParams: (ind: string) => Promise<void>
  applyBestToChart: () => Promise<void>
  saveBestGlobal: () => Promise<void>
  /** 切股清理（clearStockData 调优部分）：清空调优结果/进度/取消标记，seq 递增作废在途响应 */
  resetTuneFlow: () => void
}

/**
 * 调优域 hook（P2-19 拆分，原 useWorkbenchData 调优 state/回调整体搬移，行为零变化）：
 * 调优会话状态（open/ind/horizon/target/algorithm/indices/mode/scope）+ 异步任务执行
 * （runTune/retryTune/cancelTune，P1-38b 任务化 + waitForJob 轮询 + seq/取消守卫）+ 结果应用
 * （applyBest/applyAllBest/clearTune/saveGlobal/resetGlobal/applyBestToChart/saveBestGlobal）。
 * 契约域（specs/defaults/global/backend/manual/effective）与手动覆盖域由 useWorkbenchData 持有，
 * 经 deps 回调读写；本 hook 不反向 import useWorkbenchData。
 */
export function useTuneFlow(deps: TuneFlowDeps): TuneFlowApi {
  const { code, onContractLoaded, applyManual, clearManualAll, refreshIndicatorParams, tunedInds } = deps
  const [tuneOpen, setTuneOpen] = useState(false)
  const [tuneInd, setTuneInd] = useState('rsi')
  // 评估周期：InputNumber 空态可空（null），提交/计算时回退默认（模块级 TUNE_HORIZON_DEFAULT）
  const [tuneHorizon, setTuneHorizon] = useState<number | null>(TUNE_HORIZON_DEFAULT)
  const [tuneTarget, setTuneTarget] = useState<string>('ic_abs')
  const [tuneAlgorithm, setTuneAlgorithm] = useState<'grid' | 'random' | 'fast'>('grid')
  const [tuning, setTuning] = useState(false)
  const [tuneOptions, setTuneOptions] = useState<{ value: string; label: string }[]>(TUNE_OPTIONS_FALLBACK)
  // 多选调优指标列表(会话级,含整体/逐个模式共用);移除当前 tuneInd 时自动切换到列表首个
  const [tuneInds, setTuneIndsState] = useState<string[]>(['rsi'])
  const setTuneInds = useCallback((v: string[]) => {
    setTuneIndsState(v)
    if (!v.includes(tuneInd)) setTuneInd(v[0] || 'rsi')
  }, [tuneInd])
  const [tuneMode, setTuneMode] = useState<'individual' | 'combined'>('individual')
  // 应用范围(chart/global):持久化偏好,跨会话保持
  const [tuneScope, setTuneScopeState] = useState<TuneScope>(loadTuneScope)
  const setTuneScope = useCallback((v: TuneScope) => {
    setTuneScopeState(v)
    persistTuneScope(v)
  }, [])
  /** 调优结果: 单指标 key=indicator;整体 key='__combined__' */
  const [tuneResults, setTuneResults] = useState<Record<string, TuneResponse | CombinedTuneResponse>>({})
  const [tuneErrors, setTuneErrors] = useState<Record<string, string>>({})
  const [tuneProgress, setTuneProgress] = useState<{ current: string; done: number; total: number } | null>(null)
  // 取消进行中（请求在途）：UI 反馈「正在取消…」；请求返回后按 cancelRef 丢弃结果（seq 守卫防切股竞态）
  const [cancelling, setCancelling] = useState(false)
  // 调优取消与竞态守卫: cancel 仅停止后续发起;seq 防切股/关窗后旧响应覆盖新状态
  const cancelRef = useRef(false)
  const tuneSeq = useRef(0)
  // 异步任务(P1-38b)辅助:当前在途调优任务 id(供 cancelTune 调取消端点)+ 轮询中止信号(取消/重跑时终止 waitForJob)
  const activeJobRef = useRef<number | null>(null)
  const tuneAbortRef = useRef<AbortController | null>(null)

  // 加载可调优指标目录（/indicators/catalog tuneable）+ 指标参数契约（specs/global/effective）+ 迁移旧手动记忆。
  // 失败显性化：错误写入 tuneCatalogErr/tuneContractErr（调优面板 Alert 展示 + 重试按钮），不再静默「加载中」
  const [tuneCatalogErr, setTuneCatalogErr] = useState<string | null>(null)
  const [tuneContractErr, setTuneContractErr] = useState<string | null>(null)
  const loadTuneContract = useCallback(async () => {
    try {
      const r = await getIndicatorCatalog()
      const list = r.tuneable ?? []
      if (list.length) {
        setTuneOptions(list.map((k) => ({ value: k, label: TUNE_LABEL[k] ?? k.toUpperCase() })))
        if (!list.includes(tuneInd)) setTuneInd(list[0])
      }
      setTuneCatalogErr(null)
    } catch (e) {
      setTuneCatalogErr('指标目录加载失败: ' + errMsg(e))
    }
    try {
      const data = await getIndicatorParams()
      const specs = Object.fromEntries(data.specs.map((s) => [s.key, s]))
      let manual = loadManual()
      if (Object.keys(manual).length === 0) {
        const migrated = migrateLegacyManual(specs)
        if (migrated) {
          manual = migrated
          persistManual(manual)
          for (let i = localStorage.length - 1; i >= 0; i--) {
            const key = localStorage.key(i)
            if (key?.startsWith('sr-tuned-')) localStorage.removeItem(key)
          }
        }
      }
      onContractLoaded({ specs, defaults: data.defaults, global: data.global, backendEffective: data.effective, manual })
      setTuneContractErr(null)
    } catch (e) {
      setTuneContractErr('参数契约加载失败: ' + errMsg(e))
    }
  }, [tuneInd, onContractLoaded])
  useEffect(() => {
    void loadTuneContract()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ===== 手动覆盖 / 全局默认操作（具名参数，manual > backend global > default） =====
  const saveGlobalParams = useCallback(async (ind: string, values: IndicatorParamValues) => {
    try {
      await saveIndicatorParams(ind, { values, source: 'global', target: tuneTarget, horizon: tuneHorizon ?? TUNE_HORIZON_DEFAULT })
      notifications.show({ color: 'green', message: '已保存为全局默认' })
      await refreshIndicatorParams()
      invalidateIndicators()
    } catch (e: any) {
      notifications.show({ color: 'red', message: '保存失败: ' + (e?.response?.data?.detail ?? e?.message ?? e) })
    }
  }, [tuneTarget, tuneHorizon, refreshIndicatorParams])
  const resetGlobalParams = useCallback(async (ind: string) => {
    try {
      await resetIndicatorParams(ind)
      notifications.show({ color: 'green', message: '已重置全局默认' })
      await refreshIndicatorParams()
      invalidateIndicators()
    } catch (e: any) {
      notifications.show({ color: 'red', message: '重置失败: ' + (e?.response?.data?.detail ?? e?.message ?? e) })
    }
  }, [refreshIndicatorParams])
  const applyBestToChart = useCallback(async () => {
    const r = tuneResults[tuneInd]
    const best = r && !('combined' in r) ? r.best : null
    if (!best) return
    applyManual({ [tuneInd]: { ...best.params } })
    notifications.show({ color: 'green', message: '已应用到当前图表' })
  }, [tuneResults, tuneInd, applyManual])
  const saveBestGlobal = useCallback(async () => {
    const r = tuneResults[tuneInd]
    const best = r && !('combined' in r) ? r.best : null
    if (!best) return
    try {
      await saveIndicatorParams(tuneInd, { values: best.params, source: 'global', target: tuneTarget, horizon: tuneHorizon ?? TUNE_HORIZON_DEFAULT })
      notifications.show({ color: 'green', message: '已保存为全局默认' })
      await refreshIndicatorParams()
      invalidateIndicators()
    } catch (e: any) {
      notifications.show({ color: 'red', message: '保存失败: ' + (e?.response?.data?.detail ?? e?.message ?? e) })
    }
  }, [tuneResults, tuneInd, tuneTarget, tuneHorizon, refreshIndicatorParams])

  // ===== 多选/整体调优执行核心(串行 individual / 一次 combined,seq 守卫防切股竞态) =====
  // P1-38b 异步化：tune 提交返回 {job_id}（同步校验 400/404），计算在后台任务队列执行；
  // 结果经 GET /experiments/{job_id} 轮询（job.result 即原同步 TuneResponse/CombinedTuneResponse 结构）。
  // 取消语义：cancelTune 置 cancelRef + cancelling（UI「正在取消…」）+ abort 轮询 + DELETE /experiments/{job_id}
  // 取消后端任务；每个 await 返回后先查 cancelRef/seq——取消后即便请求在途返回，结果直接作废不写入（不再消费后续结果）。
  const runTune = useCallback(async () => {
    const seq = ++tuneSeq.current
    cancelRef.current = false
    setCancelling(false)
    setTuning(true)
    setTuneProgress({ current: '', done: 0, total: tuneInds.length })
    // B3:重跑不再清空 tuneResults——保留旧结果(用户拍板),完成后逐 key 覆盖;
    // 避免 useTuneResults 兜底 effect 因结果清空把 activeKey 拉回 '__overview__'(横跳)。
    // 错误仍清空:重跑期间旧错误不残留
    setTuneErrors({})
    const horizon = tuneHorizon ?? TUNE_HORIZON_DEFAULT
    // 清理上一次遗留轮询（若存在）并登记本次中止信号
    tuneAbortRef.current?.abort()
    const ac = new AbortController()
    tuneAbortRef.current = ac
    try {
      if (tuneMode === 'combined') {
        // 与 Select 裁剪语义一致（保留最近选择的 3 个）；UI 已受控裁剪，此处仅作一致性守卫
        const inds = [...new Set(tuneInds)].slice(-3)
        if (inds.length < 2) {
          notifications.show({ color: 'yellow', message: '整体优化需选择 2~3 个指标' })
          return
        }
        setTuneProgress({ current: inds.join('+'), done: 0, total: 1 })
        try {
          const { job_id } = await tuneIndicatorsCombined(code, inds, horizon, tuneTarget, tuneAlgorithm)
          if (cancelRef.current || seq !== tuneSeq.current) return
          activeJobRef.current = job_id
          const job = await waitForJob(job_id, {
            signal: ac.signal,
            onProgress: (j) => {
              // 守卫：seq 变化（切股/新任务）或已取消 → 立即终止轮询；cancelled 也须终止（waitForJob 仅认 done/failed 终态）
              if (seq !== tuneSeq.current || cancelRef.current) throw new Error('调优轮询已终止')
              if (j.status === 'cancelled') throw new Error(j.error || '调优任务已取消')
            },
          })
          if (cancelRef.current || seq !== tuneSeq.current) return
          if (job.status === 'failed') {
            setTuneErrors((prev) => ({ ...prev, __combined__: errMsg(job.error || '整体调优失败') }))
          } else {
            // B3:整体结果 merge 写入——单指标旧结果保留,__combined__ 覆盖(替换整表会灭掉全部单指标 key)
            setTuneResults((prev) => ({ ...prev, __combined__: job.result as unknown as CombinedTuneResponse }))
            setTuneErrors((prev) => { const out = { ...prev }; delete out['__combined__']; return out })
            setTuneProgress({ current: '__combined__', done: 1, total: 1 })
          }
        } catch (e) {
          if (cancelRef.current || seq !== tuneSeq.current) return
          setTuneErrors((prev) => ({ ...prev, __combined__: errMsg(e) }))
        } finally {
          if (seq === tuneSeq.current) activeJobRef.current = null
        }
        return
      }
      // individual:串行循环,每个指标提交独立任务并轮询至终态,每个完成立即写结果;cancel 或 seq 变化则停止(未执行的不写)
      const total = tuneInds.length
      let done = 0
      for (const ind of tuneInds) {
        if (cancelRef.current || seq !== tuneSeq.current) break
        setTuneProgress({ current: ind, done, total })
        try {
          const { job_id } = await tuneIndicator(code, ind, horizon, tuneTarget, tuneAlgorithm)
          if (cancelRef.current || seq !== tuneSeq.current) return
          activeJobRef.current = job_id
          const job = await waitForJob(job_id, {
            signal: ac.signal,
            onProgress: (j) => {
              if (seq !== tuneSeq.current || cancelRef.current) throw new Error('调优轮询已终止')
              if (j.status === 'cancelled') throw new Error(j.error || '调优任务已取消')
            },
          })
          if (cancelRef.current || seq !== tuneSeq.current) return
          if (job.status === 'failed') {
            setTuneErrors((prev) => ({ ...prev, [ind]: errMsg(job.error || `指标 ${ind} 调优失败`) }))
          } else {
            setTuneResults((prev) => ({ ...prev, [ind]: job.result as unknown as TuneResponse }))
            setTuneErrors((prev) => { const out = { ...prev }; delete out[ind]; return out })
          }
        } catch (e) {
          if (cancelRef.current || seq !== tuneSeq.current) return
          setTuneErrors((prev) => ({ ...prev, [ind]: errMsg(e) }))
        }
        done += 1
        if (seq === tuneSeq.current) setTuneProgress({ current: ind, done, total })
      }
    } finally {
      if (seq === tuneSeq.current) {
        const cancelled = cancelRef.current
        setTuning(false)
        setTuneProgress(null)
        setCancelling(false)
        activeJobRef.current = null
        if (cancelled) notifications.show({ color: 'blue', message: '已取消' })
      }
    }
  }, [code, tuneInds, tuneMode, tuneHorizon, tuneTarget, tuneAlgorithm])

  /** 单独重跑单个 key(individual 指标或 '__combined__'),成功从 tuneErrors 移除；取消语义同 runTune（P1-38b 异步任务+轮询） */
  const retryTune = useCallback(async (key: string) => {
    const seq = ++tuneSeq.current
    cancelRef.current = false
    setCancelling(false)
    setTuning(true)
    const horizon = tuneHorizon ?? TUNE_HORIZON_DEFAULT
    tuneAbortRef.current?.abort()
    const ac = new AbortController()
    tuneAbortRef.current = ac
    try {
      let inds: string[] | null = null
      if (key === '__combined__') {
        inds = [...new Set(tuneInds)].slice(-3)
        if (inds.length < 2) {
          notifications.show({ color: 'yellow', message: '整体优化需选择 2~3 个指标' })
          return
        }
      }
      const { job_id } = inds
        ? await tuneIndicatorsCombined(code, inds, horizon, tuneTarget, tuneAlgorithm)
        : await tuneIndicator(code, key, horizon, tuneTarget, tuneAlgorithm)
      if (cancelRef.current || seq !== tuneSeq.current) return
      activeJobRef.current = job_id
      const job = await waitForJob(job_id, {
        signal: ac.signal,
        onProgress: (j) => {
          if (seq !== tuneSeq.current || cancelRef.current) throw new Error('调优轮询已终止')
          if (j.status === 'cancelled') throw new Error(j.error || '调优任务已取消')
        },
      })
      if (cancelRef.current || seq !== tuneSeq.current) return
      if (job.status === 'failed') {
        setTuneErrors((prev) => ({ ...prev, [key]: errMsg(job.error || '调优失败') }))
      } else {
        setTuneResults((prev) => ({
          ...prev,
          [key]: job.result as unknown as TuneResponse | CombinedTuneResponse,
        }))
        setTuneErrors((prev) => { const out = { ...prev }; delete out[key]; return out })
      }
    } catch (e) {
      if (cancelRef.current || seq !== tuneSeq.current) return
      setTuneErrors((prev) => ({ ...prev, [key]: errMsg(e) }))
    } finally {
      if (seq === tuneSeq.current) {
        const cancelled = cancelRef.current
        setTuning(false)
        setTuneProgress(null)
        setCancelling(false)
        activeJobRef.current = null
        if (cancelled) notifications.show({ color: 'blue', message: '已取消' })
      }
    }
  }, [code, tuneInds, tuneHorizon, tuneTarget, tuneAlgorithm])

  /** 取消调优：置 cancelRef 作废在途结果 + abort 轮询 + 调任务取消端点 DELETE /experiments/{job_id} 中断后端计算；cancelling 驱动 UI「正在取消…」 */
  const cancelTune = useCallback(() => {
    cancelRef.current = true
    setCancelling(true)
    tuneAbortRef.current?.abort()
    const jobId = activeJobRef.current
    if (jobId != null) {
      void cancelTuneJob(jobId).catch(() => { /* 取消请求失败不阻塞 UI：cancelRef/abort 已生效，在途结果按取消语义作废 */ })
    }
  }, [])

  // ===== 调优结果应用(按 tuneScope: chart → manual/localStorage;global → 后端 PUT) =====
  const applyBest = useCallback(async (key?: string) => {
    const targetKey = tuneMode === 'combined' ? '__combined__' : (key ?? tuneInd)
    const r = tuneResults[targetKey]
    if (!r) { notifications.show({ color: 'yellow', message: '没有可应用的最优参数' }); return }
    let paramsByInd: Record<string, IndicatorParamValues>
    if ('combined' in r) {
      const best = r.best
      if (!best) { notifications.show({ color: 'yellow', message: '没有可应用的整体调优结果' }); return }
      paramsByInd = best.params_by_indicator
    } else {
      const best = r.best
      if (!best) { notifications.show({ color: 'yellow', message: '没有可应用的最优参数' }); return }
      paramsByInd = { [targetKey]: best.params }
    }
    const inds = Object.keys(paramsByInd)
    if (inds.length === 0) { notifications.show({ color: 'yellow', message: '没有可应用的参数' }); return }
    if (tuneScope === 'chart') {
      applyManual(paramsByInd)
      notifications.show({ color: 'green', message: `已应用 ${inds.length} 个指标到当前图表` })
    } else {
      const rs = await Promise.allSettled(
        inds.map((ind) => saveIndicatorParams(ind, { values: paramsByInd[ind], source: 'global', target: tuneTarget, horizon: tuneHorizon ?? TUNE_HORIZON_DEFAULT })),
      )
      const okCount = rs.filter((x) => x.status === 'fulfilled').length
      if (okCount === 0) notifications.show({ color: 'yellow', message: '应用失败:全部保存未成功' })
      else if (okCount < inds.length) notifications.show({ color: 'yellow', message: `已应用 ${okCount}/${inds.length} 个指标到全部股票(部分失败)` })
      else notifications.show({ color: 'green', message: `已应用 ${okCount} 个指标到全部股票` })
      await refreshIndicatorParams()
      invalidateIndicators()
    }
  }, [tuneMode, tuneInd, tuneResults, tuneScope, tuneTarget, tuneHorizon, refreshIndicatorParams, applyManual])

  /** 批量应用全部成功结果(按 tuneScope),汇总 message;combined 模式等价 applyBest('__combined__') */
  const applyAllBest = useCallback(async () => {
    if (tuneMode === 'combined') {
      await applyBest('__combined__')
      return
    }
    const keys = Object.keys(tuneResults).filter((k) => k !== '__combined__')
    const ready = keys.filter((k) => {
      const r = tuneResults[k]
      return !!r && !('combined' in r) && !!r.best
    })
    if (ready.length === 0) { notifications.show({ color: 'yellow', message: '没有可批量应用的结果' }); return }
    const paramsByInd: Record<string, IndicatorParamValues> = {}
    for (const k of ready) {
      const r = tuneResults[k] as TuneResponse
      paramsByInd[k] = r.best!.params
    }
    if (tuneScope === 'chart') {
      applyManual(paramsByInd)
      notifications.show({ color: 'green', message: `已应用 ${ready.length}/${keys.length} 个指标到当前图表` })
    } else {
      const rs = await Promise.allSettled(
        ready.map((k) => saveIndicatorParams(k, { values: paramsByInd[k], source: 'global', target: tuneTarget, horizon: tuneHorizon ?? TUNE_HORIZON_DEFAULT })),
      )
      const okCount = rs.filter((x) => x.status === 'fulfilled').length
      if (okCount === 0) notifications.show({ color: 'yellow', message: '应用失败:全部保存未成功' })
      else if (okCount < ready.length) notifications.show({ color: 'yellow', message: `已应用 ${okCount}/${keys.length} 个指标到全部股票(部分失败)` })
      else notifications.show({ color: 'green', message: `已应用 ${okCount}/${keys.length} 个指标到全部股票` })
      await refreshIndicatorParams()
      invalidateIndicators()
    }
  }, [tuneMode, tuneResults, tuneScope, tuneTarget, tuneHorizon, applyBest, refreshIndicatorParams, applyManual])

  // ===== 三档清除:manual 仅本图表 / global 仅全局默认 / all 全部(先 manual 后 global) =====
  const clearTune = useCallback(async (scope: 'manual' | 'global' | 'all') => {
    const ok: string[] = []
    const fail: string[] = []
    if (scope === 'manual' || scope === 'all') {
      const manualInds = clearManualAll()
      ok.push(...manualInds)
    }
    if (scope === 'global' || scope === 'all') {
      const inds = tunedInds
      if (inds.length) {
        const rs = await Promise.allSettled(inds.map((ind) => resetIndicatorParams(ind)))
        rs.forEach((r, i) => { (r.status === 'fulfilled' ? ok : fail).push(inds[i]) })
        if (ok.length) {
          await refreshIndicatorParams()
          invalidateIndicators()
        }
      }
    }
    const okUnique = [...new Set(ok)]
    const failUnique = [...new Set(fail)]
    if (failUnique.length === 0) {
      notifications.show({ color: 'green', message: okUnique.length ? `已清除 ${okUnique.length} 项:${okUnique.join(',')}` : '没有需要清除的参数' })
    } else {
      notifications.show({ color: 'yellow', message: `部分清除失败 ${failUnique.length} 项:${failUnique.join(',')}` })
    }
    return { ok: okUnique, fail: failUnique }
  }, [tunedInds, clearManualAll, refreshIndicatorParams])

  /** 切股清理调优域：清空结果/进度/取消标记，seq 递增使在途旧响应被守卫丢弃（clearStockData 调用） */
  const resetTuneFlow = useCallback(() => {
    setTuneResults({})
    setTuneErrors({})
    setTuneProgress(null)
    setTuning(false)
    setCancelling(false)
    tuneSeq.current += 1
    cancelRef.current = true
  }, [])

  return {
    tuneOpen, setTuneOpen,
    tuneInd, setTuneInd,
    tuneHorizon, setTuneHorizon,
    tuneTarget, setTuneTarget,
    tuneAlgorithm, setTuneAlgorithm,
    tuneOptions,
    tuneCatalogErr, tuneContractErr, retryTuneContract: loadTuneContract,
    tuneInds, setTuneInds, tuneMode, setTuneMode, tuneScope, setTuneScope,
    tuneResults, tuneErrors, tuneProgress, tuning, cancelling,
    runTune, retryTune, cancelTune,
    applyBest, applyAllBest, clearTune,
    saveGlobalParams, resetGlobalParams,
    applyBestToChart, saveBestGlobal,
    resetTuneFlow,
  }
}
