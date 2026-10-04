import { Button, Drawer, SegmentedControl, Splitter, Text } from '@mantine/core'
import type { SplitterPaneSize } from '@mantine/hooks'
import { notifications } from '@mantine/notifications'
import { IconBolt } from '@tabler/icons-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { getSignalCatalog, getSignalEventsPage } from '../../api/client'
import type { PageStats, ScanUniverse, SignalCatalogItem, SignalEvent, SignalEventsPageParams } from '../../api/client'
import { useWatchlistCodes } from '../../hooks/useWatchlistStar'
import { signalLabelMap, SIGNAL_CATEGORY_COLOR } from '../../utils/signals'
import { toMarketEpochMs } from '../../utils/time'
import { isMinutePeriod } from '../../utils/periods'
import { usePersistentState } from '../../utils/stateMemory'
import { useViewport } from '../../app/useViewport'
import { useLatestRef, useStableSetter } from '../../hooks/useStableSetter'
import { usePeriodScan } from './signalCenter/usePeriodScan'
import PageShell from '../../components/ui/PageShell'
import SignalStreamPanel from './signalCenter/SignalStreamPanel'
import WatchlistPane from './signalCenter/WatchlistPane'
import { sorters, useSignalColumns } from './signalCenter/columns'
import {
  DIR_KEY, PERIOD_OPTIONS, SIGNAL_PANE_MAX, SIGNAL_PANE_MIN, SIGNAL_SPLIT_DEFAULT, SignalCenterCtx, TIME_RANGE_DEFAULT,
  isSignalMatch, isTimeRangeValue, loadDir, loadSignalSplit, persistSignalSplit,
} from './signalCenter/context'
import { toSplitNumbers } from '../../utils/splitPersist'
import type {
  SignalCenterApi, SignalDir, SignalMatch, SignalSchemeFilters, SignalSortState, StockGroup, TimeRangeValue,
} from './signalCenter/context'
import { useSignalSchemes } from './signalCenter/useSignalSchemes'
import { useCopilotStore } from '../../stores/copilotStore'
import type { SignalCenterCommand } from '../../stores/copilotStore'
import { useCopilotProvider } from '../../hooks/useCopilotProvider'
import './signalCenter/signalStreamPanel.css'

// ===== 通知（antd message → @mantine/notifications；模块级单例，不参与 useCallback 依赖） =====
const notifyInfo = (message: string) => notifications.show({ color: 'blue', message })
const notifyError = (message: string) => notifications.show({ color: 'red', message })

// T-76 手动扫描 ETA 文案（分钟周期，按 universe 估算耗时：关注股≈快 / 成交额前 N≈慢；常量可配）
const MANUAL_SCAN_ETA_TEXT: Record<ScanUniverse, string> = {
  watchlist: '关注股分钟扫描约需 2 分钟，后台进行中',
  top_n: '成交额前 N 分钟扫描约需 5 分钟，后台进行中',
  codes: '指定代码分钟扫描约需 2 分钟，后台进行中',
}

export default function SignalCenter() {
  const viewport = useViewport()
  const isDesktop = viewport === 'expanded' || viewport === 'rail'
  // T-136 平板档改造:drawer 档(768-991,抽屉导航)信号主区并入移动卡片列表——
  // 桌面表格在平板内容区列宽受限,卡片流+筛选抽屉是更一致的窄屏体验;
  // 992+ 仍走桌面表格(expanded/rail 不变量)
  const isMobile = viewport === 'mobile' || viewport === 'drawer'

  const [catalog, setCatalog] = useState<SignalCatalogItem[]>([])
  const [dir, setDir] = useState<Record<string, SignalDir>>(loadDir)

  // 关注集合由 useWatchlistStar（星标组件内部）消费；页面层仅保留 watchlistOnly 重灌副作用
  const [watchlistOnly, setWatchlistOnly] = usePersistentState<boolean>('signal-wl-only', false)
  const setWatchlistOnlySt = useStableSetter(setWatchlistOnly)
  const [sectors, setSectors] = usePersistentState<string[]>('signal-sectors', [])
  const setSectorsSt = useStableSetter(setSectors)
  const [signalFilter, setSignalFilter] = usePersistentState<string[]>('signal-signals', [])
  const setSignalFilterSt = useStableSetter(setSignalFilter)
  // 排除今日（持久化，默认不排除）；服务端筛选，见 serverFilter.exclude_today
  const [excludeToday, setExcludeTodayRaw] = usePersistentState<boolean>('signal-exclude-today', false)
  const setExcludeTodayRawSt = useStableSetter(setExcludeTodayRaw)
  // 多信号命中语义（持久化，默认任一）；服务端筛选，见 serverFilter.signal_match
  const [signalMatchRaw, setSignalMatchRaw] = usePersistentState<SignalMatch>('signal-match', 'any')
  const signalMatch: SignalMatch = isSignalMatch(signalMatchRaw) ? signalMatchRaw : 'any'
  const setSignalMatchRawSt = useStableSetter(setSignalMatchRaw)
  const setSignalMatch = useCallback((v: SignalMatch) => setSignalMatchRawSt(isSignalMatch(v) ? v : 'any'), [setSignalMatchRawSt])
  const [period, setPeriod] = usePersistentState<string>('signal-period', 'daily')
  const setPeriodSt = useStableSetter(setPeriod)
  // periodRef：reload（依赖 [] 恒稳）等无需随 period 重建的回调读取当前周期
  // （共享 useLatestRef，P2-19 收敛，替代原手写 useRef + useEffect 同步）
  const periodRef = useLatestRef(period)
  // 信号时间范围（持久化）：读时校验，旧值非法回退近3日；写入时同样归一化
  const [timeRangeRaw, setTimeRangeRaw] = usePersistentState<string>('signal-time-range', TIME_RANGE_DEFAULT)
  const timeRange: TimeRangeValue = isTimeRangeValue(timeRangeRaw) ? timeRangeRaw : TIME_RANGE_DEFAULT
  const setTimeRangeRawSt = useStableSetter(setTimeRangeRaw)
  // 「今日」与「排除今日」互斥（T-27）：任一方向写入都自动纠正对方，保证不出现必然空结果组合。
  // 反向纠正走原始持久化 setter（不递归触发），message 说明原因；互斥集中在 setter 层，
  // 抽屉/助手命令/持久化恢复三条写入路径统一生效
  const setTimeRange = useCallback((v: string) => {
    const next = isTimeRangeValue(v) ? v : TIME_RANGE_DEFAULT
    setTimeRangeRawSt(next)
    if (next === 'today' && excludeToday) {
      setExcludeTodayRawSt(false)
      notifyInfo('时间范围「今日」与「排除今日」互斥，已自动关闭「排除今日」')
    }
  }, [setTimeRangeRawSt, excludeToday, setExcludeTodayRawSt])
  const setExcludeToday = useCallback((v: boolean) => {
    setExcludeTodayRawSt(v)
    if (v && timeRange === 'today') {
      setTimeRangeRawSt('all')
      notifyInfo('时间范围「今日」与「排除今日」互斥，已重置时间范围为「全部」')
    }
  }, [setExcludeTodayRawSt, timeRange, setTimeRangeRawSt])
  // 互斥兜底（T-27）：批处理写入（copilot replace）/ 旧版本持久化恢复可能一次带入「今日+排除今日」
  // 组合，setter 级互斥（闭包读旧值）覆盖不到——此处以最终状态归一化，保证绝不出现必然空结果组合；
  // UI 顺序操作已由 setter 级互斥先行纠正，此 effect 仅在组合真正出现时触发一次
  useEffect(() => {
    if (timeRange === 'today' && excludeToday) {
      setExcludeTodayRawSt(false)
      notifyInfo('时间范围「今日」与「排除今日」互斥，已自动关闭「排除今日」')
    }
  }, [timeRange, excludeToday, setExcludeTodayRawSt])

  // ===== T-129 URL 筛选可寻址（05 §5.1 URL 规格：period/signals/sectors/time_range/match/watchlist_only） =====
  // URL 为可寻址单一入口之一：挂载时 URL 参数优先于持久化(深链/前进后退直达)重灌；
  // 状态变化防抖 300ms 回写(replace 不污染历史),仅写非默认值保持 URL 简洁。
  const [searchParams, setSearchParams] = useSearchParams()
  const urlInitRef = useRef(false)
  useEffect(() => {
    if (urlInitRef.current) return
    urlInitRef.current = true
    const p = searchParams
    const sp = p.get('period')
    if (sp && PERIOD_OPTIONS.some((o) => o.value === sp)) setPeriod(sp)
    const sg = p.get('signals')
    if (sg) setSignalFilter(sg.split(',').filter(Boolean))
    const se = p.get('sectors')
    if (se) setSectors(se.split(',').filter(Boolean))
    const tr = p.get('time_range')
    if (tr) setTimeRange(tr)
    const m = p.get('match')
    if (m === 'all' || m === 'any') setSignalMatch(m)
    if (p.get('watchlist_only') === '1' || p.get('watchlist_only') === 'true') setWatchlistOnly(true)
    if (p.get('exclude_today') === '1' || p.get('exclude_today') === 'true') setExcludeToday(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const urlTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (!urlInitRef.current) return
    if (urlTimerRef.current != null) clearTimeout(urlTimerRef.current)
    urlTimerRef.current = setTimeout(() => {
      urlTimerRef.current = null
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        const setOrDel = (k: string, v: string | null | undefined) => { if (v) next.set(k, v); else next.delete(k) }
        setOrDel('period', period === 'daily' ? undefined : period)
        setOrDel('signals', signalFilter.length ? signalFilter.join(',') : undefined)
        setOrDel('sectors', sectors.length ? sectors.join(',') : undefined)
        setOrDel('time_range', timeRange === TIME_RANGE_DEFAULT ? undefined : timeRange)
        setOrDel('match', signalMatch === 'any' ? undefined : signalMatch)
        setOrDel('watchlist_only', watchlistOnly ? '1' : undefined)
        setOrDel('exclude_today', excludeToday ? '1' : undefined)
        return next
      }, { replace: true })
    }, 300)
    return () => { if (urlTimerRef.current != null) clearTimeout(urlTimerRef.current) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [period, sectors, signalFilter, signalMatch, excludeToday, watchlistOnly, timeRange, setSearchParams])

  // ===== T-53 左右分栏：桌面 Splitter 左栏关注流 / 右栏全市场信号流；窄屏（<992）左栏转抽屉 =====
  // 窄屏抽屉开关：入口按钮在 SignalStreamPanel 功能栏（isDesktop 时恒关、不渲染）
  const [watchlistPaneOpen, setWatchlistPaneOpen] = useState(false)
  // 桌面 Splitter 比例（%）：onSizeChange 以 rAF 合并 setState（拖拽高频回调只落一次状态），
  // onResizeEnd 冲刷挂起 rAF 并持久化到 localStorage（同 StockWorkbench 模式）
  const [splitSizes, setSplitSizes] = useState<number[]>(loadSignalSplit)
  const splitRafRef = useRef<number | null>(null)
  // Mantine 回调 sizes 单位为声明单位（number=百分比），归一为 number[] 再落状态
  // （防御走共享 toSplitNumbers：仅 number/'%' 串，防 px 串被 parseFloat 污染）
  const toNumbers = (sizes: SplitterPaneSize[]): number[] => toSplitNumbers(sizes)
  const onSplitResize = useCallback((sizes: SplitterPaneSize[]) => {
    if (splitRafRef.current != null) return
    splitRafRef.current = requestAnimationFrame(() => {
      splitRafRef.current = null
      setSplitSizes(toNumbers(sizes))
    })
  }, [])
  const onSplitResizeEnd = useCallback((_handle: number, sizes: SplitterPaneSize[]) => {
    if (splitRafRef.current != null) { cancelAnimationFrame(splitRafRef.current); splitRafRef.current = null }
    const nums = toNumbers(sizes)
    setSplitSizes(nums)
    persistSignalSplit(nums)
  }, [])
  useEffect(() => () => {
    if (splitRafRef.current != null) { cancelAnimationFrame(splitRafRef.current); splitRafRef.current = null }
  }, [])

  // ===== 受控排序：sortState 驱动 sortedGroups（作用于已加载集合，再供浏览筛选后的列表） =====
  const [sortState, setSortState] = useState<SignalSortState | null>(null)

  // ===== 客户端无限滚动（事件流）：按 offset 增量拉取，事件 id 去重 + 跨批按股票合并 =====
  // P1-47 评估结论（2026-08-14，保留手写实现并记账）：
  // useInfiniteQuery 迁移评估——7 处语义不匹配，每处都需桥接补丁（机制外特判），成本≈收益，保留：
  // ① 重置语义 = 清空累积从第 1 批重建（sr-refresh/筛选防抖/关注集合变化/扫描完成四路统一走
  //    reloadInfinite）；useInfiniteQuery 的 refetch/invalidate 默认保留累积重跑全部页，清空重建需
  //    removeQueries+refetch 或 refreshCounter 进 queryKey（后者使三路入口退化为 key 拼接 hack）；
  // ② 关注集合内容变化（仅看关注时取消关注的股票需消失）无法进 queryKey（watchlistOnly 布尔不够），
  //    需手动 reset 与 queryKey 自动重置双轨并存，是已知反模式；
  // ③ 哨兵 resetKey（infResetKey）契约需额外 state 桥接递增；
  // ④ id 去重 + 跨批合并需 select 在每次新页到达时全量重算；
  // ⑤ stats/total 取最近一批响应，需 select 提取后再拆回 ctx 五字段契约；
  // ⑥ 首拉失败（pageError）与加载更多失败（哨兵 error）需 isError+data 存在性区分；
  // ⑦ in-flight 同步锁/代际守卫由 infGen+infLoadingRef 承担，useInfiniteQuery 依赖 query 级并发控制，
  //    慢请求叠加场景行为依赖版本实现。
  // 当前实现已机制化（统一重置入口 + 同步锁 + 代际守卫 + 防抖合并），本文件 623 行 + 6 处
  // eslint-disable，验收仅 tsc 无运行时验证，大面积重写回归风险高于收益；后续重构路径为
  // 「数据获取下沉 data/ 池、页面保留状态机」，useInfiniteQuery 选型留待有运行时验证条件时重估。
  const [infEvents, setInfEvents] = useState<SignalEvent[]>([])
  // P2-74 增量分组：跨批只并入新事件（O(新增) 每批），替代每批对全量累积集合
  // groupSignalEvents + latestEventOf 重算（O(累积) 每批 → 累计 O(N²)）。
  // groupsRef 为分组 Map（含 best/latest 增量维护），seenRef 为已并入事件 id 去重，
  // 二者随 reloadInfinite 一并重置，与 infEvents 同一生命周期
  const groupsRef = useRef(new Map<string, StockGroup>())
  const seenRef = useRef(new Set<number>())
  const [infTotal, setInfTotal] = useState(0)
  const [infHasMore, setInfHasMore] = useState(true)
  const [infLoading, setInfLoading] = useState(false)
  const [infError, setInfError] = useState(false)
  const [infResetKey, setInfResetKey] = useState(0)
  // 筛选口径聚合统计（最近一批响应 stats：今日新增事件数 + 去重股票数，全量聚合）
  const [infStats, setInfStats] = useState<PageStats>()
  const infGen = useRef(0)
  const infNextOffset = useRef(0)
  const infFilterKeyRef = useRef('')
  // 同步加载锁：state 更新异步，reset/reload 需同步释放锁才能立即发起新批
  const infLoadingRef = useRef(false)

  const dirOf = useCallback((code: string) => dir[code] ?? 0, [dir])
  // setDirOf 身份恒定（[] 函数式更新）：供排除弹层与筛选入口稳定引用；
  // localStorage 持久化由下方防抖 useEffect 统一写入（不再每次同步序列化）
  const setDirOf = useCallback((code: string, v: SignalDir) => {
    setDir((prev) => ({ ...prev, [code]: v }))
  }, [])
  // 清除全部排除（排除指示条「清空」入口）：立即持久化空 map（与 copilot 清 dir 一致；
  // 下方防抖 useEffect 会再幂等写入 {}，无副作用）；T-27 起 dir 走 sessionStorage（与其他筛选一致）
  const clearDir = useCallback(() => {
    setDir({})
    try { sessionStorage.setItem(DIR_KEY, JSON.stringify({})) } catch { /* ignore */ }
  }, [])
  const dirSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const dirMountedRef = useRef(false)
  useEffect(() => {
    // 跳过首渲染：初始 dir 来自 loadDir()，无需回写
    if (!dirMountedRef.current) { dirMountedRef.current = true; return }
    if (dirSaveTimer.current) clearTimeout(dirSaveTimer.current)
    dirSaveTimer.current = setTimeout(() => {
      try { sessionStorage.setItem(DIR_KEY, JSON.stringify(dir)) } catch { /* ignore */ }
    }, 400)
    return () => { if (dirSaveTimer.current) clearTimeout(dirSaveTimer.current) }
  }, [dir])

  const labelMap = useMemo(() => signalLabelMap(catalog), [catalog])
  const labelOf = useCallback((code: string) => labelMap[code] ?? { text: code, color: 'default' }, [labelMap])

  // ===== 服务端筛选：筛选变化 → 无限滚动重置重建 =====
  // 每批大小（桌面 25 / 移动 20）；dir 排除为浏览层，不重置事件累积
  const batchSize = isDesktop ? 25 : 20
  const serverFilter = useMemo<SignalEventsPageParams>(() => ({
    period,
    // 信号触发时间范围始终显式发送（含 all；后端缺省 3d，省略会回落默认）
    time_range: timeRange,
    sector: sectors.length ? sectors.join(',') : undefined,
    watchlist_only: watchlistOnly ? true : undefined,
    // 信号类型（signal_match 控制任一/全部命中）服务端筛选，计入真实 total
    signal_types: signalFilter.length ? signalFilter.join(',') : undefined,
    signal_match: signalMatch,
    exclude_today: excludeToday ? true : undefined,
  }), [period, timeRange, sectors, watchlistOnly, signalFilter, signalMatch, excludeToday])
  // 无限模式重置键：服务端筛选 + 每批大小
  const serverFilterKey = useMemo(() => JSON.stringify(serverFilter) + '|ps:' + batchSize, [serverFilter, batchSize])
  const serverFilterKeyRef = useRef(serverFilterKey)
  serverFilterKeyRef.current = serverFilterKey
  // 300ms 防抖合并重拉（T-27）：所有服务端筛选字段共享同一 serverFilterKey，连续快速调整
  // （如多选网格逐项点选）只触发一次重拉；UI 选中态即时更新（state 立即变），仅网络层防抖。
  // 首渲染 debounced 值 = 当前 key（相等），不产生额外加载——初次加载由下方 effect 的 ref 对比承担
  const [debouncedFilterKey, setDebouncedFilterKey] = useState(serverFilterKey)
  useEffect(() => {
    const t = setTimeout(() => setDebouncedFilterKey(serverFilterKey), 300)
    return () => clearTimeout(t)
  }, [serverFilterKey])

  // 无限模式：增量拉取（offset 递增），按事件 id 去重、跨批累计；stats 随最近一批响应更新（全量聚合）
  const loadInfiniteMore = useCallback(async () => {
    if (infLoadingRef.current) return
    const gen = infGen.current
    infLoadingRef.current = true
    setInfLoading(true)
    setInfError(false)
    const offset = infNextOffset.current
    try {
      const res = await getSignalEventsPage({ ...serverFilter, limit: batchSize, offset })
      if (gen !== infGen.current) return
      const items = res?.items ?? []
      // P2-74 增量：仅把本批新事件并入分组 Map（seenRef 去重，与 setInfEvents 一致），
      // 不重建全量累积集合——groups 派生不再每批 O(累积) 重算
      const fresh: SignalEvent[] = []
      const groups = groupsRef.current
      const seen = seenRef.current
      for (const it of items) {
        if (seen.has(it.id)) continue
        seen.add(it.id)
        fresh.push(it)
        let g = groups.get(it.stock_code)
        if (!g) {
          g = {
            code: it.stock_code, name: it.stock_name ?? '',
            events: [], signals: [], best: it, latest: it,
            triggered_at: it.triggered_at ?? null, as_of: it.as_of ?? null, discovered_at: it.scan_discovered_at ?? null,
          }
          groups.set(it.stock_code, g)
        }
        g.events.push(it)
        for (const s of it.signals ?? []) if (!g.signals.includes(s)) g.signals.push(s)
        // best/latest 增量维护：展示时刻 as_of ?? triggered_at 取最大（与 latestEventOf 同口径，
        // 但 O(1)/事件，避免每次派生对全量事件求 max）
        const curMs = toMarketEpochMs(g.best.as_of ?? g.best.triggered_at)
        const newMs = toMarketEpochMs(it.as_of ?? it.triggered_at)
        if (newMs > curMs) {
          g.best = it; g.latest = it
          g.triggered_at = it.triggered_at ?? null
          g.as_of = it.as_of ?? null
          g.discovered_at = it.scan_discovered_at ?? null
        }
      }
      setInfEvents((prev) => (fresh.length ? [...prev, ...fresh] : prev))
      infNextOffset.current = offset + items.length
      setInfTotal(res?.total ?? 0)
      setInfHasMore(res?.has_more === true || (res?.has_more == null && items.length > 0))
      setInfStats(res?.stats)
    } catch (e: any) {
      if (gen !== infGen.current) return
      setInfError(true)
      notifyError('加载更多失败: ' + (e?.message ?? e))
    } finally {
      // 仅当前代释放锁；过期请求的锁已由 reset/reload 同步移交新一批，不得再触碰
      if (gen === infGen.current) {
        infLoadingRef.current = false
        setInfLoading(false)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverFilter, batchSize])

  const loadInfiniteMoreRef = useLatestRef(loadInfiniteMore)

  /** 无限模式统一重置（第 1 批重建）：废弃在途请求、清累积/游标/错误态/同步锁。
   *  筛选变化 effect、统一刷新 reload、关注集合变化（toggleWatch）三处复用，
   *  单一实现避免各调用点漏重置某状态（游标/已加载条数/错误态/聚合统计）。
   *  依赖 []：全部状态经 ref / 恒定 setter 读写，回调身份恒定（不破坏 effect/reload 稳定性） */
  const reloadInfinite = useCallback(() => {
    infGen.current++
    infNextOffset.current = 0
    infLoadingRef.current = false
    infFilterKeyRef.current = serverFilterKeyRef.current
    setInfEvents([])
    groupsRef.current = new Map()
    seenRef.current = new Set()
    setInfTotal(0)
    setInfHasMore(true)
    setInfError(false)
    setInfLoading(false)
    setInfStats(undefined)
    setInfResetKey((k) => k + 1)
    void loadInfiniteMoreRef.current()
  }, [])

  // sr-refresh 广播（顶栏全局刷新，仅重拉不触发重算）→ 无限重置重建，保证顶栏刷新与页内刷新
  // 一样拿到最新事件（页内刷新=触发扫描+重拉，顶栏=仅重拉，见 SignalStreamPanel tooltip）
  useEffect(() => {
    const onRefresh = () => reloadInfinite()
    window.addEventListener('sr-refresh', onRefresh)
    return () => window.removeEventListener('sr-refresh', onRefresh)
  }, [reloadInfinite])

  // T-129:SSE signal 事件（marketStream 广播）→ 主列表无限重置重建；
  // 扫描批量产出事件时高频,节流 2s 合并
  const signalEventTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    const onSignal = () => {
      if (signalEventTimerRef.current != null) return
      signalEventTimerRef.current = setTimeout(() => {
        signalEventTimerRef.current = null
        reloadInfinite()
      }, 2000)
    }
    window.addEventListener('sr-signal-event', onSignal)
    return () => {
      window.removeEventListener('sr-signal-event', onSignal)
      if (signalEventTimerRef.current != null) clearTimeout(signalEventTimerRef.current)
    }
  }, [reloadInfinite])

  // 筛选 / 周期 / 开关变化（防抖合并后）→ 废弃在途请求，重置累积并从第 1 批重建
  useEffect(() => {
    if (infFilterKeyRef.current === debouncedFilterKey) return
    reloadInfinite()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedFilterKey])

  // ===== 非日线周期扫描调度（P2-19 抽为 usePeriodScan + T-54 扩展：universe 随分钟扫描携带、
  // 新鲜度阈值按 scan_status.schedule 周期间隔；onScanDone = 扫描完成统一刷新事件列表） =====
  const { scanBusy, scanProgress, periodScanAt, triggerScan, cancelScan, isScanPending, manualTriggerScan, scanUniverse, setScanUniverse } = usePeriodScan({
    period,
    onScanDone: reloadInfinite,
  })

  /** T-76 手动触发分钟扫描（「立即扫描」按钮）：绕过新鲜度判定直接触发，携带当前 universe；
   *  冷却/互斥冲突已由 usePeriodScan.manualTriggerScan 提示并拒绝；触发成功补发 ETA 文案 */
  const handleManualScan = useCallback(async () => {
    if (!isMinutePeriod(period)) return
    const ok = await manualTriggerScan()
    if (ok) notifyInfo(MANUAL_SCAN_ETA_TEXT[scanUniverse])
  }, [period, scanUniverse, manualTriggerScan])

  // 统一刷新：无限模式 → 重置并重载第 1 批 + 非日线强制触发重算
  const reload = useCallback(() => {
    const p = periodRef.current
    if (p !== 'daily' && !isScanPending(p)) {
      void triggerScan(p)
    }
    reloadInfinite()
  }, [triggerScan, reloadInfinite, isScanPending])

  const pageLoading = infLoading && infEvents.length === 0
  const refreshing = infLoading && infEvents.length > 0
  // 首拉失败（错误且无已加载事件）进错误态（优先于「暂无信号」空态），P0-2 修复
  const pageError = infError && infEvents.length === 0

  // 服务端筛选（周期/时间范围/板块/关注/信号/命中语义/排除今日）变化 → 重置排序：
  // 筛选/周期变化后旧排序作用于新数据无意义（dir 为浏览层，排序仍对其结果有效，不清）
  useEffect(() => {
    setSortState(null)
  }, [period, timeRange, sectors, watchlistOnly, batchSize, signalFilter, signalMatch, excludeToday])

  // 命中语义仅在多选（signalFilter.length > 1）时有意义（T-27）：单选/未选时自动重置为「任一」，
  // 与抽屉隐藏条件一致——不静默保留无意义的值，角标 activeFilterCount 与可见生效条件对齐
  useEffect(() => {
    if (signalFilter.length <= 1 && signalMatch !== 'any') setSignalMatch('any')
  }, [signalFilter, signalMatch, setSignalMatch])

  // ===== 关注集合（星标已收编 useWatchlistStar，见 columns.tsx StarButton） =====
  // P2-68：关注集合改由 useWatchlistCodes 单一订阅派生（成员不变引用稳定），行内星标经
  // watchlistCodes 做 O(1) 成员查找，不再每行独立订阅 useWatchlist。
  // watchlistOnly 重灌副作用：仅看关注时，关注集合变化直接改变过滤结果——以服务端列表
  // 签名（code 排序拼接）变化为信号，无限重置累积从第 1 批重建（否则旧事件仍命中缓存，
  // 取消关注的股票不消失）。SWR 轮询返回同内容时签名不变，不触发重灌；首次挂载只记录不触发。
  const { codes: watchedCodes, watchlist: freshWatchlist } = useWatchlistCodes()
  const watchlistSigRef = useRef<string | null>(null)
  useEffect(() => {
    if (!watchlistOnly) return
    const sig = (freshWatchlist ?? []).map((w) => w.code).sort().join(',')
    if (watchlistSigRef.current !== null && watchlistSigRef.current !== sig) reloadInfinite()
    watchlistSigRef.current = sig
  }, [freshWatchlist, watchlistOnly, reloadInfinite])

  useEffect(() => {
    getSignalCatalog().then(setCatalog).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 按股票合并事件：分组/best/latest 已在 loadInfiniteMore 增量维护（P2-74），
  // 此处只做 Map → 数组派生（O(分组数)），不再对全量累积集合重分组（O(N²)）
  const groups = useMemo<StockGroup[]>(() => [...groupsRef.current.values()], [infEvents])

  // 浏览筛选：仅排除（dir=-1）作用于当前视图（「只看」已废弃并入服务端 signalFilter，dir 不再有 1）
  const filteredGroups = useMemo(() => {
    const exclSet = new Set(Object.keys(dir).filter((code) => dir[code] === -1))
    return groups.filter((g) => {
      if (exclSet.size && g.signals.some((s) => exclSet.has(s))) return false
      return true
    })
  }, [groups, dir])
  const groupCount = filteredGroups.length

  // 受控排序：作用于已加载集合（sortedGroups 为全部 filteredGroups 排序后的结果，
  // 不再按页切片——无限滚动一次性加载全部）。ascend=比较器原序，descend 取反；
  // 未启用或 key 无比较器 → 原样返回
  const sortedGroups = useMemo(() => {
    if (!sortState || !sorters[sortState.columnKey]) return filteredGroups
    const cmp = sorters[sortState.columnKey]
    return [...filteredGroups].sort(sortState.order === 'ascend' ? cmp : (a, b) => cmp(b, a))
  }, [filteredGroups, sortState])

  // ===== Copilot 上下文 provider（/signals）：当前筛选态 + 命中股票 Top 榜，供 LLM 追问答 =====
  useCopilotProvider('/signals', {
    context: () => ({
      route: '/signals',
      title: '信号中心',
      period,
      time_range: timeRange,
      sectors,
      signal_types: signalFilter,
      signal_match: signalMatch,
      watchlist_only: watchlistOnly,
      exclude_today: excludeToday,
      stock_count: groupCount,
      top_stocks: sortedGroups.slice(0, 10).map((g) => ({
        code: g.code, name: g.name, signals: g.signals, triggered_at: g.triggered_at,
      })),
    }),
  })

  // 关注切换已收编：星标渲染走 columns.tsx 的 StarButton（useWatchlistStar 单一实现，
  // 乐观更新 + 失败回滚 + in-flight 锁），页面不再持有关注集合状态与内联实现。

  // ===== 助手命令：signal_center.apply_filters（replace 语义，消费一次） =====
  const pendingCommand: SignalCenterCommand | null = useCopilotStore((s) => s.pendingCommand)
  const consumeCommand = useCopilotStore((s) => s.consumeCommand)
  useEffect(() => {
    if (!pendingCommand || pendingCommand.type !== 'signal_center.apply_filters') return
    const p = pendingCommand.payload ?? {}
    // replace 语义：payload 覆盖全部服务端筛选，缺字段一律回退默认（daily/3d/空数组/any/false），
    // 绝不"只更新已提供字段"——未携带项清空为默认值，确保 replace 真正生效
    setPeriod(typeof p.period === 'string' && p.period ? p.period : 'daily')
    setTimeRange(p.timeRange ?? TIME_RANGE_DEFAULT)
    // 排除今日（replace 语义缺省 false）：payload 携带布尔则应用，未携带回退不排除
    setExcludeToday(typeof p.excludeToday === 'boolean' ? p.excludeToday : false)
    setSectors(Array.isArray(p.sectors) ? p.sectors : [])
    setSignalFilter(Array.isArray(p.signals) ? p.signals : [])
    setSignalMatch(p.match === 'all' ? 'all' : 'any')
    setWatchlistOnly(p.watchlist === true)
    // 清除浏览筛选：排除为浏览筛选（sessionStorage 持久化），命令不携带即视为清空（「只看」已废弃）
    const nextDir: Record<string, SignalDir> = {}
    setDir(nextDir)
    try { sessionStorage.setItem(DIR_KEY, JSON.stringify(nextDir)) } catch { /* ignore */ }
    consumeCommand()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingCommand, consumeCommand])

  // ===== 命名筛选方案（T-13）：保存/应用完整筛选现场 =====
  // 方案快照 = 全部服务端筛选字段 + 浏览层 dir 排除；字段序固定（激活一致性比较依赖稳定序列化）
  const currentFilters = useMemo<SignalSchemeFilters>(() => ({
    period, timeRange, sectors, signalFilter, signalMatch, watchlistOnly, excludeToday, dir,
  }), [period, timeRange, sectors, signalFilter, signalMatch, watchlistOnly, excludeToday, dir])

  // 应用方案：走原始持久化 setter——方案快照保存时已互斥归一化（today 与排除今日不会同时为真），
  // 无需互斥纠正通知；dir 一并写回并立即持久化（sessionStorage，与 clearDir/copilot 命令一致，
  // 方案语义 = 完整筛选现场，应用后所见即所存）
  const applyFilters = useCallback((f: SignalSchemeFilters) => {
    setPeriodSt(f.period)
    setTimeRangeRawSt(f.timeRange)
    setExcludeTodayRawSt(f.excludeToday)
    setSectorsSt(f.sectors)
    setSignalFilterSt(f.signalFilter)
    setSignalMatchRawSt(f.signalMatch)
    setWatchlistOnlySt(f.watchlistOnly)
    setDir(f.dir)
    try { sessionStorage.setItem(DIR_KEY, JSON.stringify(f.dir)) } catch { /* ignore */ }
  }, [setPeriodSt, setTimeRangeRawSt, setExcludeTodayRawSt, setSectorsSt,
    setSignalFilterSt, setSignalMatchRawSt, setWatchlistOnlySt, setDir])

  const schemeStore = useSignalSchemes({ currentFilters, applyFilters })

  // 合并视图：每股一行（多模板信号合并）；展开查看明细。
  // 列定义已拆出 signalCenter/columns.tsx（useSignalColumns）：星标列走 StarButton（useWatchlistStar
  // 收编），列引用稳定 → SignalStreamPanel 内 streamCols useMemo 真正稳定，避免 Table 全量重建（D2）
  const { groupColumns, detailColsBase } = useSignalColumns({ period, sortState, labelOf, watchedCodes })

  const signalCategories = useMemo(() => {
    const m = new Map<string, string[]>()
    for (const it of catalog) {
      if (!it.category) continue
      const codes = m.get(it.category) ?? []
      codes.push(it.code)
      m.set(it.category, codes)
    }
    return [...m.entries()].map(([category, codes]) => ({
      category, codes, color: SIGNAL_CATEGORY_COLOR[category] ?? 'default',
    }))
  }, [catalog])

  // ctx 依赖列全（筛选/无限/数据字段/回调）：漏依赖会 stale——仔细核对（D2）
  const ctx = useMemo<SignalCenterApi>(() => ({
    period, setPeriod: setPeriodSt, sectors, setSectors: setSectorsSt, signalFilter, setSignalFilter: setSignalFilterSt,
    signalMatch, setSignalMatch,
    catalog, labelOf, signalCategories, watchlistOnly, setWatchlistOnly: setWatchlistOnlySt,
    timeRange, setTimeRange, excludeToday, setExcludeToday,
    filteredGroups,
    sortState, setSortState, sortedGroups,
    groupCount,
    scanBusy, scanProgress, cancelScan, periodScanAt, triggerScan,
    scanUniverse, setScanUniverse,
    pageLoading, refreshing, pageError, reload,
    groupCols: groupColumns, detailColumns: detailColsBase,
    infEvents, infTotal, infStats, infHasMore, infLoading, infError,
    infResetKey, loadInfiniteMore,
    dir, dirOf, setDirOf, clearDir,
    schemes: schemeStore.schemes,
    activeSchemeId: schemeStore.activeId,
    saveScheme: schemeStore.save,
    applyScheme: schemeStore.apply,
    removeScheme: schemeStore.remove,
    viewport, isDesktop, isMobile,
    watchlistPaneOpen, setWatchlistPaneOpen,
    watchlistCount: freshWatchlist?.length ?? 0,
    watchlistCodes: watchedCodes,
  }), [
    period, setPeriodSt, sectors, setSectorsSt, signalFilter, setSignalFilterSt,
    signalMatch, setSignalMatch,
    catalog, labelOf, signalCategories, watchlistOnly, setWatchlistOnlySt,
    timeRange, setTimeRange, excludeToday, setExcludeToday,
    filteredGroups,
    sortState, setSortState, sortedGroups,
    groupCount, pageLoading, refreshing, pageError, reload,
    scanBusy, scanProgress, cancelScan, periodScanAt, triggerScan,
    scanUniverse, setScanUniverse,
    groupColumns, detailColsBase,
    infEvents, infTotal, infStats, infHasMore, infLoading, infError,
    infResetKey, loadInfiniteMore,
    dir, dirOf, setDirOf, clearDir,
    schemeStore.schemes, schemeStore.activeId, schemeStore.save, schemeStore.apply, schemeStore.remove,
    viewport, isDesktop, isMobile,
    watchlistPaneOpen, setWatchlistPaneOpen,
    freshWatchlist, watchedCodes,
  ])

  return (
    <PageShell fill>
      <SignalCenterCtx.Provider value={ctx}>
        {/* T-54 分钟扫描范围选择条（仅分钟周期可见，分栏外一行；范围持久化 sr-scan-universe，
            手动扫描/自动触发均带 universe 参数，扫描口径文案随之动态）。
            T-76「立即扫描」按钮与范围同带：绕过新鲜度判定直接触发（手动冷却/互斥由
            usePeriodScan.manualTriggerScan 提示），忙碌进度条在 SignalStreamPanel 复用 */}
        {isMinutePeriod(period) && (
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0,
            padding: '4px var(--sr-pad-lg)', borderBottom: '1px solid var(--sr-border)',
          }}>
            <Text span size="xs" style={{ color: 'var(--sr-text-2)', whiteSpace: 'nowrap' }}>扫描范围</Text>
            <SegmentedControl size="xs" value={scanUniverse}
              onChange={(v) => setScanUniverse(v === 'top_n' ? 'top_n' : 'watchlist')}
              data={[
                { label: '关注股', value: 'watchlist' },
                { label: '成交额前 N', value: 'top_n' },
              ]} />
            <Button
              size="compact-xs"
              variant="light"
              leftSection={<IconBolt size={12} />}
              loading={scanBusy}
              onClick={() => void handleManualScan()}
              aria-label="立即扫描当前分钟周期"
            >
              立即扫描
            </Button>
          </div>
        )}
        {/* 页面根：桌面=Splitter 左右分栏（左栏关注流 / 右栏全市场信号流）；窄屏=<992 左栏转抽屉 */}
        <div className="sr-inbox-body">
          {isDesktop ? (
            <Splitter
              orientation="horizontal"
              className="sr-signal-split"
              sizes={splitSizes}
              onSizeChange={onSplitResize}
              onResizeEnd={onSplitResizeEnd}
            >
              {/* 左栏关注流：默认 22%（≈320px@1440）· min 18%（≈260px@1440）· max 45%；
                  defaultSize = 真默认常量（双击 handle 重置目标；sizes 受控承载持久化值） */}
              <Splitter.Pane className="sr-wlp-pane" defaultSize={SIGNAL_SPLIT_DEFAULT[0]} min={SIGNAL_PANE_MIN} max={SIGNAL_PANE_MAX}>
                <WatchlistPane />
              </Splitter.Pane>
              {/* 右栏全市场信号流（现有 SignalStreamPanel 原样，L0 结论条/筛选/列宽不受影响） */}
              <Splitter.Pane defaultSize={SIGNAL_SPLIT_DEFAULT[1]} min={100 - SIGNAL_PANE_MAX} max={100 - SIGNAL_PANE_MIN}>
                <SignalStreamPanel />
              </Splitter.Pane>
            </Splitter>
          ) : (
            <>
              <SignalStreamPanel />
              {/* 窄屏关注流抽屉：入口按钮在 SignalStreamPanel 功能栏（带关注计数 Badge） */}
              <Drawer
                opened={watchlistPaneOpen}
                onClose={() => setWatchlistPaneOpen(false)}
                title="关注流"
                position="right"
                size="min(340px, 92vw)"
                withCloseButton
              >
                <WatchlistPane />
              </Drawer>
            </>
          )}
        </div>
      </SignalCenterCtx.Provider>
    </PageShell>
  )
}
