import { notifications } from '@mantine/notifications'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { KlineChartHandle } from '../../../components/KlineChart'
import { getFinancials, getFundamental, getIndicatorParams, getNews, getSignalCatalog } from '../../../api/client'
import type { IndicatorParamValues, IndicatorSpec, IndicatorStoredParams, TimelineEvent } from '../../../api/client'
import type { KlineData } from '../../../utils/klineSeries'
import { invalidateKlines, useKlineState } from '../../../data/kline'
import { invalidateIndicators, useIndicatorsState, useRiskState } from '../../../data/indicators'
import { invalidateQuotes, useQuoteState } from '../../../data/market'
import { invalidateTimeline, useTimelineState } from '../../../data/timeline'
import { signalLabelMap } from '../../../utils/signals'
import { usePersistentState } from '../../../utils/stateMemory'
import { useStableSetter } from '../../../hooks/useStableSetter'
import type { RiskSummary, WorkbenchCtxApi, WorkbenchFinancials, WorkbenchFundamental, WorkbenchNewsItem, WorkbenchRisk } from './context'
import { useWorkbenchCopilot } from './copilot'
import { FALLBACK_SIGNAL_LABEL, loadManual, persistManual } from './persist'
import { computeRiskGrade, toFinite } from './sections/shared'
import { useWatchlistStar } from '../../../hooks/useWatchlistStar'
import { useTuneFlow } from './useTuneFlow'

/** 时间线空数组（模块级常量）：池 value 未就绪时占位，引用稳定防 ctx useMemo 无谓重建 */
const EMPTY_TIMELINE: TimelineEvent[] = []

/**
 * 工作台数据层 hook：state + 数据加载 + 指标参数/调优操作 + ctx 构造，全部逻辑与
 * 拆分前 StockWorkbench 组件体等价（纯搬移）。调优域（tune* state/回调/异步任务）已拆分
 * 至 useTuneFlow（P2-19），本文件仅消费其返回值并透传，ctx 对外结构不变。
 * 返回的 ctx 由调用方注入 WorkbenchCtx.Provider。
 *
 * active(T-55 多股 tab 保活):false 表示本实例处于非活跃保活态(display:none)——
 * 各数据池传 enabled=false 停止订阅与轮询(停数据活动),sr-refresh/切股 effect 不执行;
 * 切回 active=true 时池按 staleTime 自动恢复(超时重拉/未超时用缓存),面板状态不丢。
 */
export function useWorkbenchData(code: string, active: boolean): WorkbenchCtxApi {
  const [loading, setLoading] = useState(false)
  // K 线图表句柄（T-15 导出）：KlinePanel 挂载时经 ref 转发注入;StockHeader 消费（截图等）
  const klineRef = useRef<KlineChartHandle | null>(null)
  const [labelMap, setLabelMap] = useState<Record<string, { text: string; color: string }>>(FALLBACK_SIGNAL_LABEL)
  // 行情走统一订阅层（60s SWR，按 code 全站共享；卸载后缓存保留，无需处理）;
  // 非活跃(enabled=false)停订阅停轮询,保留停前缓存
  const quoteState = useQuoteState(code, active)
  const quote = quoteState.value
  // 行情失败可见化：订阅层 error 非空即提示（首次失败无数据；轮询失败保留旧图不闪烁）
  const errQuote = quoteState.error ? '行情加载失败' : null
  // 更新时间：行情服务器上海时刻（quote.timestamp，naive ISO 秒级字符串；字符串直解展示，
  // 替代浏览器本地钟——时区/时钟漂移不可信）。行情失败/缺 timestamp 时为空。
  const quoteTime: string | null = quote?.timestamp ?? null
  const [fundamental, setFundamental] = useState<WorkbenchFundamental | null>(null)
  const [news, setNews] = useState<WorkbenchNewsItem[]>([])
  // 周期与指标显隐：usePersistentState 记忆（key 'wb:state'，对象 {period, overlays}，刷新/前进后退恢复）
  const [wbState, setWbState] = usePersistentState<{ period: string; overlays: string[] }>(
    'wb:state',
    { period: 'daily', overlays: ['ma5', 'ma10', 'ma20', 'macd'] },
  )
  const period = wbState.period
  const overlays = wbState.overlays

  // K 线池订阅（60s SWR + 有订阅者时 60s 轮询，按 code:period 全站共享；切股/切周期自动重拉）
  const klineQ = useKlineState(code, period, 300, active)
  // 指标订阅池：indParams 依赖 manualParams（下方声明），订阅/klineData 合并见 manualParams 之后
  // 池错误派生（首次失败且无数据 → 错误空态；轮询失败保留旧图，与 KlineChart「error && !data」语义对齐）
  const errKline = klineQ.error ? `K 线数据加载失败（${period} 周期）` : null
  // 仅首载透传 loading（避免轮询刷新时 Spin 遮罩每 60s 闪烁）
  const klineLoading = klineQ.loading && !klineQ.value
  // usePersistentState 的 setter 每次渲染新建：useStableSetter 经 latest-ref 保持
  // setPeriod/toggleOverlay 稳定（D4 ctx useMemo 依赖）；替代原手写 setWbStateRef 三行
  const setWbStateStable = useStableSetter(setWbState)
  // 各面板独立错误标注
  // 指标错误（errInd）由订阅池派生，见 manualParams 后
  const [errFund, setErrFund] = useState<string | null>(null)
  const [errNews, setErrNews] = useState<string | null>(null)
  const [financials, setFinancials] = useState<WorkbenchFinancials | null>(null)
  const [errFinancials, setErrFinancials] = useState<string | null>(null)
  const [risk, setRisk] = useState<WorkbenchRisk | null>(null)
  const [errRisk, setErrRisk] = useState<string | null>(null)
  const [riskDays, setRiskDays] = useState(0)

  // 指标参数契约（GET /indicators/params）：specs 白名单 + v2 全局 + 生效参数（manual > backend global > default）
  const [specsMap, setSpecsMap] = useState<Record<string, IndicatorSpec>>({})
  const [defaultsMap, setDefaultsMap] = useState<Record<string, IndicatorParamValues>>({})
  const [globalParams, setGlobalParams] = useState<Record<string, IndicatorStoredParams>>({})
  const [backendEffective, setBackendEffective] = useState<Record<string, IndicatorParamValues>>({})
  const [effectiveParams, setEffectiveParams] = useState<Record<string, IndicatorParamValues>>({})
  // 手动覆盖（manual > global > default）：ref 与 state 同步，供异步闭包读取最新值
  const manualRef = useRef<Record<string, IndicatorParamValues>>(loadManual())
  const [manualParams, setManualParams] = useState<Record<string, IndicatorParamValues>>(manualRef.current)
  const setManual = useCallback((m: Record<string, IndicatorParamValues>) => {
    manualRef.current = m
    setManualParams(m)
    persistManual(m)
  }, [])

  // 指标订阅池：参数化 key（含 custom 手动参数）全站共享一份请求与缓存；60s 轮询由池持有，
  // 非交易时段降频 5min；手动参数/period/overlays 变化 → key 变化 → 新 store 自动重拉，无需显式失效
  const indParams = useMemo(() => {
    const fields = overlays.join(',') || 'ma5,ma10,ma20,ma60,boll,macd,rsi,kdj,volume_ratio'
    const p: Record<string, string | number> = { code, fields, days: 300, period }
    const manual = manualRef.current
    if (Object.keys(manual).length > 0) p.custom = JSON.stringify(manual)
    return p
  }, [code, overlays, period, manualParams])
  const indState = useIndicatorsState(indParams, active)
  const indicators = indState.value?.indicators
  // 指标加载失败可见化：首次失败无数据 → 警告条（可重试）；轮询失败保留旧值不闪烁（与 KlineChart 语义对齐）
  const errInd = indState.error ? '指标加载失败' : null
  // K 线图数据 = K 线池 + 指标合并（K 线接口不含 indicators）
  const klineData = useMemo<KlineData | null>(
    () => (klineQ.value ? { ...klineQ.value, indicators } : null),
    [klineQ.value, indicators],
  )
  // 指标响应驱动生效参数（仅手动覆盖传后端，backend global/legacy 由后端合并进 effective_params）：
  // 60s 轮询刷新引用但值未变时不触发 setState，防止下游表单（tune 参数编辑器）未提交输入被重置
  useEffect(() => {
    const eff = indState.value?.effective_params
    if (eff) setEffectiveParams((prev) => (JSON.stringify(prev) === JSON.stringify(eff) ? prev : eff))
  }, [indState.value])

  // 关注星标：统一走 useWatchlistStar（乐观更新 + 失败回滚 + in-flight 锁），
  // 与信号中心/关注页共用同一实现；ctx 导出名 watchlisted/toggleWatch/wlPending 保持不变
  const star = useWatchlistStar(code)
  const watchlisted = star.watched
  const wlPending = star.pending
  const toggleWatch = star.toggle

  useEffect(() => {
    getSignalCatalog().then((items) => {
      const m = signalLabelMap(items)
      if (Object.keys(m).length) setLabelMap({ ...FALLBACK_SIGNAL_LABEL, ...m })
    }).catch(() => {})
  }, [])

  /** 调优域（useTuneFlow）依赖注入：契约加载 → 写回契约域 state（specs/defaults/global/backend/manual/effective） */
  const onContractLoaded = useCallback((data: {
    specs: Record<string, IndicatorSpec>
    defaults: Record<string, IndicatorParamValues>
    global: Record<string, IndicatorStoredParams>
    backendEffective: Record<string, IndicatorParamValues>
    manual: Record<string, IndicatorParamValues>
  }) => {
    setSpecsMap(data.specs)
    setDefaultsMap(data.defaults)
    setGlobalParams(data.global)
    setBackendEffective(data.backendEffective)
    setManual(data.manual)
    setEffectiveParams({ ...data.backendEffective, ...data.manual })
  }, [setManual])
  /** 调优结果应用（chart 分支）：合并写入手动覆盖 + 生效参数 + 失效指标池 */
  const applyManual = useCallback((paramsByInd: Record<string, IndicatorParamValues>) => {
    setManual({ ...manualRef.current, ...paramsByInd })
    setEffectiveParams((prev) => ({ ...prev, ...paramsByInd }))
    invalidateIndicators()
  }, [setManual])
  /** 清空手动覆盖并回退后端生效参数 + 失效指标池；返回被清除的指标 key 列表 */
  const clearManualAll = useCallback((): string[] => {
    const manualInds = Object.keys(manualRef.current)
    if (!manualInds.length) return []
    setManual({})
    setEffectiveParams({ ...backendEffective })
    invalidateIndicators()
    return manualInds
  }, [setManual, backendEffective])
  /** 刷新指标参数契约（specs/global/effective）：保存/重置全局后调用 */
  const refreshIndicatorParams = useCallback(async () => {
    try {
      const data = await getIndicatorParams()
      const specs = Object.fromEntries(data.specs.map((s) => [s.key, s]))
      setSpecsMap(specs)
      setDefaultsMap(data.defaults)
      setGlobalParams(data.global)
      setBackendEffective(data.effective)
      setEffectiveParams({ ...data.effective, ...manualRef.current })
    } catch { /* 静默：下次加载/操作再刷新 */ }
  }, [])

  // 已覆盖系统默认（手动或全局）的指标列表，工具条「生效参数」显示
  const tunedInds = useMemo(() => {
    return Object.keys(specsMap)
      .filter((k) => {
        const eff = effectiveParams[k]
        const def = defaultsMap[k]
        if (!eff || !def) return false
        return JSON.stringify(eff) !== JSON.stringify(def)
      })
      .sort()
  }, [effectiveParams, specsMap, defaultsMap])

  // 调优域 state/回调全部来自 useTuneFlow（P2-19 拆分，行为与拆分前逐字段等价）：
  // 依赖（契约加载/手动覆盖写回/契约刷新/tunedInds）经参数注入，反向依赖由 useWorkbenchData 提供
  const tune = useTuneFlow({
    code,
    onContractLoaded,
    applyManual,
    clearManualAll,
    refreshIndicatorParams,
    tunedInds,
  })

  // 重试加载指标：失效 ind 池（订阅 store 收到 invalidate 后立即重拉；成功清 error → 警告条消失）
  const retryIndicators = useCallback(() => {
    invalidateIndicators()
  }, [])

  // 风险池订阅（data/indicators.ts）：参数化 key（code:riskDays）全站共享一份请求与缓存，
  // 5min SWR + 有订阅者时 5min 轮询（非交易时段降频 15min）、sr-refresh 联动失效；
  // riskDays 变化 → key 变化 → 新 store 自动重拉，无需手写 effect 触发
  const riskQ = useRiskState(code, riskDays, active)
  // 池结果同步到本地 state（消费方 risk/errRisk 不变）：成功写值并清错；池 error → 文案错误；
  // loading 不动已有值（轮询失败保留旧值不闪烁，与 errKline「error && !data」语义对齐）
  useEffect(() => {
    if (riskQ.value) setRisk(riskQ.value)
    setErrRisk(riskQ.error ? '风险指标暂不可用' : null)
  }, [riskQ.value, riskQ.error])

  // 风险摘要（K 线叠加数据契约，供 fe-kline 消费；只做数据装配不画图）：
  // grade 由 computeRiskGrade 唯一计算（与 risk.tsx「综合」tab 共用），max_drawdown/var 取日线口径
  const riskSummary = useMemo<RiskSummary>(() => ({
    grade: computeRiskGrade(risk),
    max_drawdown: toFinite(risk?.max_drawdown),
    var: toFinite(risk?.var95_cf) ?? toFinite(risk?.var95),
  }), [risk])

  const loadSeq = useRef(0)
  const load = useCallback(async (c: string) => {
    const seq = ++loadSeq.current
    setLoading(true)
    setErrFund(null); setErrNews(null); setErrFinancials(null); setErrRisk(null)
    try {
      // K 线/指标/时间线由池订阅独立负责（挂载/切股自动重拉），此处仅处理其余面板
      const [f, n] = await Promise.allSettled([
        getFundamental(c),
        getNews(c, 8),
      ])
      if (seq !== loadSeq.current) return
      if (f.status === 'fulfilled') setFundamental(f.value)
      else setErrFund('基本面数据加载失败')
      if (n.status === 'fulfilled') setNews(n.value)
      else setErrNews('新闻加载失败')
      try {
        const fin = await getFinancials(c)
        if (seq !== loadSeq.current) return
        setFinancials(fin)
      } catch { if (seq === loadSeq.current) setErrFinancials('财务摘要暂不可用') }
    } catch (e: any) {
      if (seq === loadSeq.current) notifications.show({ color: 'red', message: '页面加载异常: ' + (e?.message ?? e) })
    } finally {
      if (seq === loadSeq.current) setLoading(false)
    }
  }, [])

  // ===== 手动覆盖 / 全局默认操作（具名参数，manual > backend global > default） =====
  const applyManualParams = useCallback(async (ind: string, values: IndicatorParamValues) => {
    setManual({ ...manualRef.current, [ind]: { ...values } })
    setEffectiveParams((prev) => ({ ...prev, [ind]: { ...values } }))
    invalidateIndicators()
  }, [setManual])
  const clearManualParams = useCallback(async (ind: string) => {
    const next = { ...manualRef.current }
    delete next[ind]
    setManual(next)
    setEffectiveParams((prev) => {
      const out = { ...prev }
      if (backendEffective[ind] != null) out[ind] = backendEffective[ind]
      else delete out[ind]
      return out
    })
    invalidateIndicators()
  }, [setManual, backendEffective])

  // E2b：URL ?code= 变化（与当前展示 code 不同）时重灌；code 相同时忽略（防覆盖手动导航），
  // 仅 period 变化时直接重载。
  // 切换 code 时立即清空上一股票全部数据（含错误/调优结果），旧请求由 loadSeq 守卫丢弃，防止旧数据覆盖新股票。
  const clearStockData = () => {
    // K 线与指标池随 key(code/period/custom) 自动切换清空，无需手动清；其余面板本地 state 需清
    setFundamental(null)
    setNews([])
    setFinancials(null)
    setRisk(null)
    // 切股清空调优状态（useTuneFlow 内部：结果/进度/取消标记 + seq 递增作废在途响应）
    tune.resetTuneFlow()
    setErrFund(null); setErrNews(null); setErrFinancials(null); setErrRisk(null)
  }
  const lastKeyRef = useRef<{ code: string; period: string } | null>(null)
  useEffect(() => {
    // 非活跃保活态不启动加载(数据已在首次打开时拉取;池 enabled=false 已停订阅轮询)
    if (!active) return
    const prev = lastKeyRef.current
    lastKeyRef.current = { code, period }
    if (prev === null) { load(code); return }
    if (prev.code !== code) {
      clearStockData()
      load(code)
      return
    }
    // 周期切换：仅失效 K 线池 + 指标池（池 key 含 period，失效后立即按新周期重拉），
    // 不重拉 period 无关面板（基本面/新闻/财务/风险/时间线）——P2-18
    if (prev.period !== period) {
      invalidateKlines()
      invalidateIndicators()
      return
    }
  }, [code, period, active])

  // E4：全局刷新（sr-refresh）→ 失效 kline 池 + 指标池 + 行情订阅层 + 时间线池 → 重载当前股票；
  // 非活跃 tab 不监听（停数据活动,避免后台拉取）
  useEffect(() => {
    if (!active) return
    const onRefresh = () => {
      invalidateKlines()
      invalidateIndicators()
      invalidateQuotes()
      invalidateTimeline()
      load(code)
    }
    window.addEventListener('sr-refresh', onRefresh)
    return () => window.removeEventListener('sr-refresh', onRefresh)
  }, [code, period, active])

  // 信号时间线：池订阅（P1-47 迁池，替代原手写 setTimeout 链式轮询）。
  // 节奏与行为与原实现一致：盘中 60s、非交易时段降频 5min；visibilitychange 停表（页面隐藏
  // 时原实现依然在跑，现由池的 refetchIntervalInBackground:false 停表）、失败指数退避、惰性淘汰
  // 全部由 queryBase 池机制承担（data/timeline.ts）。切股随 queryKey 变化自动重拉。
  // 非活跃(enabled=false)停止订阅与轮询。
  const timelineQ = useTimelineState(code, active)
  const timelineLoading = timelineQ.loading && !timelineQ.value
  // 空数组用模块级常量：value 未就绪时引用稳定，避免 ctx useMemo 因新字面量每次重建
  const timelineEvents = timelineQ.value ?? EMPTY_TIMELINE
  const errSignal = timelineQ.error ? '信号时间线加载失败' : null

  // E4b：指标盘中轮询已并入订阅池（60s SWR + 有订阅者时 60s 轮询、非交易时段降频 5min、
  // 惰性淘汰/退避/visibilitychange 停表均由池机制承担），此处不再需要手动定时器

  // copilot 上下文读取最新状态（context 每次渲染都变，但注册/注销只应发生一次）；
  // 非活跃时不注册（providers 按 key 单例,仅激活 tab 占用 /stocks）
  const ctxRef = useWorkbenchCopilot(active)

  const prevOverlays = useRef<string[] | null>(null)
  useEffect(() => {
    if (prevOverlays.current === null) { prevOverlays.current = overlays; return }
    if (JSON.stringify(prevOverlays.current) === JSON.stringify(overlays)) return
    prevOverlays.current = overlays
    invalidateIndicators()
  }, [overlays])

  // 稳定回调：setPeriod/toggleOverlay 走 useStableSetter（latest-ref，D4），onRetry 依赖稳定 load（D4）
  const setPeriod = useCallback((p: string) => setWbStateStable((s) => ({ ...s, period: p })), [setWbStateStable])
  const toggleOverlay = useCallback((k: string) =>
    setWbStateStable((s) => ({ ...s, overlays: s.overlays.includes(k) ? s.overlays.filter((x) => x !== k) : [...s.overlays, k] })),
  [setWbStateStable])
  const onRetry = useCallback(() => { invalidateKlines(); invalidateIndicators(); invalidateQuotes(); invalidateTimeline(); load(code) }, [load, code, period])

  // ctx 依赖列全（含数据/筛选/分页/无限/回调）：缺依赖会 stale——仔细核对（D4）
  const ctx = useMemo<WorkbenchCtxApi>(() => ({
    code,
    klineRef,
    quote, errQuote, quoteTime, fundamental,
    klineData, loading: loading || klineLoading || timelineLoading, klineLoading, errKline,
    errInd, retryIndicators,
    period,
    setPeriod,
    overlays,
    toggleOverlay,
    onRetry,
    watchlisted, toggleWatch, wlPending,
    tuneOpen: tune.tuneOpen, setTuneOpen: tune.setTuneOpen,
    specs: specsMap,
    defaults: defaultsMap,
    globalParams,
    manualParams,
    effectiveParams,
    tunedInds,
    applyManualParams, clearManualParams,
    saveGlobalParams: tune.saveGlobalParams, resetGlobalParams: tune.resetGlobalParams,
    applyBestToChart: tune.applyBestToChart, saveBestGlobal: tune.saveBestGlobal,
    tuneInd: tune.tuneInd, setTuneInd: tune.setTuneInd, tuneHorizon: tune.tuneHorizon, setTuneHorizon: tune.setTuneHorizon,
    tuneTarget: tune.tuneTarget, setTuneTarget: tune.setTuneTarget,
    tuneAlgorithm: tune.tuneAlgorithm, setTuneAlgorithm: tune.setTuneAlgorithm,
    tuneOptions: tune.tuneOptions,
    tuneCatalogErr: tune.tuneCatalogErr, tuneContractErr: tune.tuneContractErr, retryTuneContract: tune.retryTuneContract,
    tuneInds: tune.tuneInds, setTuneInds: tune.setTuneInds, tuneMode: tune.tuneMode, setTuneMode: tune.setTuneMode,
    tuneScope: tune.tuneScope, setTuneScope: tune.setTuneScope,
    tuneResults: tune.tuneResults, tuneErrors: tune.tuneErrors, tuneProgress: tune.tuneProgress,
    tuning: tune.tuning, cancelling: tune.cancelling, runTune: tune.runTune, retryTune: tune.retryTune, cancelTune: tune.cancelTune,
    applyBest: tune.applyBest, applyAllBest: tune.applyAllBest, clearTune: tune.clearTune,
    risk, errRisk, riskDays, setRiskDays, riskSummary,
    timelineEvents, errSignal, labelMap,
    errFund, financials, errFinancials, news, errNews,
  }), [
    code, klineRef, quote, errQuote, quoteTime, fundamental,
    klineData, loading, klineLoading, timelineLoading, errKline,
    errInd, retryIndicators,
    period, setPeriod, overlays, toggleOverlay, onRetry,
    watchlisted, toggleWatch, wlPending,
    tune.tuneOpen, specsMap, defaultsMap, globalParams, manualParams, effectiveParams, tunedInds,
    applyManualParams, clearManualParams,
    tune.saveGlobalParams, tune.resetGlobalParams,
    tune.applyBestToChart, tune.saveBestGlobal,
    tune.tuneInd, tune.setTuneInds, tune.tuneMode, tune.setTuneMode, tune.tuneScope, tune.setTuneScope,
    tune.tuneHorizon, tune.tuneTarget, tune.tuneAlgorithm,
    tune.tuneOptions,
    tune.tuneCatalogErr, tune.tuneContractErr, tune.retryTuneContract,
    tune.tuneInds, tune.tuneResults, tune.tuneErrors, tune.tuneProgress, tune.tuning, tune.cancelling,
    tune.runTune, tune.retryTune, tune.cancelTune,
    tune.applyBest, tune.applyAllBest, tune.clearTune,
    risk, errRisk, riskDays, riskSummary,
    timelineEvents, errSignal, labelMap,
    errFund, financials, errFinancials, news, errNews,
  ])
  ctxRef.current = ctx

  return ctx
}
