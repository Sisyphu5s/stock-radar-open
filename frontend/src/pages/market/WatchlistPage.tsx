import { Button, Flex, Group, Select, Text, TextInput } from '@mantine/core'
import { modals } from '@mantine/modals'
import { notifications } from '@mantine/notifications'
import { IconChartLine, IconDownload, IconFolderCog, IconRefresh, IconSearch, IconTrash, IconUpload, IconX } from '@tabler/icons-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, getKline, getSignalCatalog, invalidateCache, invalidatePersistedCache } from '../../api/client'
import type { SignalCatalogItem, SignalEvent, WatchlistItem } from '../../api/client'
import {
  addWatchlistGroupItems, createWatchlistGroup, deleteWatchlistGroup,
  downloadWatchlistCsv, removeWatchlistGroupItem, renameWatchlistGroup,
} from '../../api/watchlistGroups'
import { invalidateSignalEvents, invalidateWatchlist, useSignalEventsState, useWatchlistState } from '../../data/market'
import { invalidateWatchlistGroups, useWatchlistGroups } from '../../data/watchlistGroups'
import { invalidateKlines } from '../../data/kline'
import { invalidateRisk } from '../../data/indicators'
import PageHeader from '../../components/ui/PageHeader'
import EmptyState from '../../components/ui/EmptyState'
import InfiniteScrollSentinel from '../../components/ui/InfiniteScrollSentinel'
import InfiniteScrollToggle, { useInfiniteScrollEnabled } from '../../components/ui/InfiniteScrollToggle'
import PageShell from '../../components/ui/PageShell'
import StatStrip from '../../components/ui/StatStrip'
import SkeletonBlock from '../../components/ui/SkeletonBlock'
import { groupSignalEvents, signalLabelMap } from '../../utils/signals'
import { fmtPct, errMsg } from '../../utils/format'
import { cnTodayOf, toMarketEpochMs } from '../../utils/time'
import { useViewport } from '../../app/useViewport'
import './watchlist/watchlist.css'
import MobileWatchlist from './watchlist/MobileWatchlist'
import WatchlistTable from './watchlist/WatchlistTable'
import WatchlistGroupManager from './watchlist/WatchlistGroupManager'
import WatchlistImportModal from './watchlist/WatchlistImportModal'
import { toRowView } from './watchlist/rowModel'
import type { KlineInfo, RiskInfo } from './watchlist/rowModel'
import { WL_SEGMENTS } from './watchlist/types'
import type { WlFilter, WlGroup, WlRow } from './watchlist/types'
import { useCopilotProvider } from '../../hooks/useCopilotProvider'

/**
 * P2-75 并发受限批量拉取：迷你 K 线等无缓存全量突发场景，限制同时在途请求数
 * （浏览器连接池 ~6/域，超发只会排队阻塞，无吞吐收益）。
 * 输出与 Promise.allSettled 同构（保序），调用方既有结果处理逻辑零改动。
 */
async function mapLimitSettled<T, R>(
  items: readonly T[],
  limit: number,
  fn: (item: T) => Promise<R>,
): Promise<PromiseSettledResult<R>[]> {
  const out = new Array<PromiseSettledResult<R>>(items.length)
  let i = 0
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (i < items.length) {
      const idx = i++
      try {
        out[idx] = { status: 'fulfilled', value: await fn(items[idx]) }
      } catch (reason) {
        out[idx] = { status: 'rejected', reason }
      }
    }
  })
  await Promise.all(workers)
  return out
}

export default function WatchlistPage() {
  const navigate = useNavigate()
  const viewport = useViewport()
  const isMobile = viewport === 'mobile'
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([])
  const [events, setEvents] = useState<SignalEvent[]>([])
  const [catalog, setCatalog] = useState<SignalCatalogItem[]>([])
  const [filter, setFilter] = useState<WlFilter>('all')
  // 本地搜索（名称/代码模糊匹配，客户端即时过滤）：与三段筛选 AND 叠加
  const [keyword, setKeyword] = useState('')
  // T-12 分组筛选：'all' = 全部分组；选中分组后与三段筛选/搜索 AND 叠加
  const [selectedGroup, setSelectedGroup] = useState<number | 'all'>('all')
  const [groupManagerOpened, setGroupManagerOpened] = useState(false)
  const [importOpened, setImportOpened] = useState(false)
  const [wlPage, setWlPage] = useState(1)
  const [mobileCount, setMobileCount] = useState(20)

  // ---- 无限滚动（客户端分批 20）：开启时隐藏分页/「加载更多」，sentinel 自动灌入 ----
  const [infinite, setInfinite] = useInfiniteScrollEnabled('sr-wl-infinite', false)
  const [infCount, setInfCount] = useState(20)
  // 全部取消关注 in-flight 锁（Popconfirm 连点防并发全量请求）
  const unfollowBusyRef = useRef(false)
  // P2-61 复活守卫：乐观移除后、池推送确认移除前，过滤掉「待移除」code——
  // 取消关注后池推送旧缓存（DELETE 前快照）会把已移除股票重新加回造成闪烁，
  // 守卫在 freshWatchlist 不再含该 code（服务端已确认）时释放
  const pendingRemoveRef = useRef(new Set<string>())

  const klineFetched = useRef(new Set<string>())
  // 迷你图失败重试：失败项不立即标记 fetched（留待下次 watchlist/klineTick 变化自动重试），
  // 达上限才标记，防止网络持续异常时每次变化都重打请求（失败风暴）
  const KLINE_MAX_RETRIES = 3
  /** P2-75 迷你 K 线并发上限（同时在途请求数）：防大盘关注时全量突发打爆连接池 */
  const KLINE_CONCURRENCY = 8
  const klineFailCount = useRef(new Map<string, number>())
  const [klineInfo, setKlineInfo] = useState<KlineInfo>({})
  // 全局刷新（sr-refresh，MainLayout 顶栏按钮 dispatch）→ 清 kline 双层持久缓存 + 重置每代码一次缓存，
  // 迷你图立即重拉（persistedGet 短 TTL 5min，避免 6h 陈旧图与 60s 行情脱节）；tick 驱动下方拉取 effect 重跑；
  // 风险缓存同步清空（TTL 外的新鲜数据源），与 klineFetched 对称
  const [klineTick, setKlineTick] = useState(0)
  // 页面级数据刷新（kline 双层持久缓存 + 每代码一次缓存 + 风险缓存重置）：
  // 与全局 sr-refresh 广播一致的清理范围，供页面「刷新」按钮复用，避免两处各写一套
  const refreshPage = useCallback(() => {
    invalidateCache('kline:')
    invalidatePersistedCache('kline:')
    klineFetched.current = new Set<string>()
    klineFailCount.current.clear()
    riskCache.current.clear()
    setKlineTick((t) => t + 1)
  }, [])
  useEffect(() => {
    window.addEventListener('sr-refresh', refreshPage)
    return () => window.removeEventListener('sr-refresh', refreshPage)
  }, [refreshPage])
  useEffect(() => {
    const missing = watchlist.map((w) => w.code).filter((c) => !klineFetched.current.has(c))
    if (!missing.length) return
    let cancelled = false
    // P2-75：并发上限（迷你 K 线无缓存时不再 N 请求全量突发，连接池排队无益）
    mapLimitSettled(missing, KLINE_CONCURRENCY, (c) => getKline(c, 'daily', 60)).then((results) => {
      if (cancelled) return
      const next: KlineInfo = {}
      results.forEach((r, i) => {
        const c = missing[i]
        if (r.status === 'fulfilled' && Array.isArray(r.value?.close)) {
          klineFetched.current.add(c)
          klineFailCount.current.delete(c)
          const closes = (r.value.close as number[]).slice(-30)
          const dates = (r.value.dates as string[]).slice(-30)
          const valid: number[] = []
          closes.forEach((v, idx) => { if (Number.isFinite(v)) valid.push(idx) })
          next[c] = { closes: valid.map((idx) => closes[idx]), dates: valid.map((idx) => dates[idx]) }
        } else {
          // 失败项不标记 fetched（下次依赖变化自动重试）；达 KLINE_MAX_RETRIES 上限才标记，防失败风暴
          const n = (klineFailCount.current.get(c) ?? 0) + 1
          if (n >= KLINE_MAX_RETRIES) {
            klineFetched.current.add(c)
            klineFailCount.current.delete(c)
          } else {
            klineFailCount.current.set(c, n)
          }
          next[c] = null
        }
      })
      setKlineInfo((prev) => ({ ...prev, ...next }))
    })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchlist, klineTick])

  // 风险信息（年化波动 / 最大回撤）：每只并行 GET，Promise.allSettled + 组件级 Map 缓存避免重复拉。
  // TTL 5min 防永久保留旧值（风险指标随行情刷新）；sr-refresh 时一并清空（与 klineFetched 对称）。
  // 机制边界：本页为整列表批量快照场景，不走 data/indicators.ts 的 risk 单股池订阅——
  // 逐 code 循环 useRiskState 会产生 N 个轮询定时器，违背池「订阅驱动轮询」的设计；
  // 单股场景（useWorkbenchData）用池订阅，批量场景用此处并行拉取，两者共享同一后端端点，TTL 语义一致（5min）
  const RISK_CACHE_TTL = 5 * 60 * 1000
  const riskCache = useRef(new Map<string, { ts: number; v: { annual_volatility?: number; max_drawdown?: number } }>())
  const [riskInfo, setRiskInfo] = useState<RiskInfo>({})
  useEffect(() => {
    const missing = watchlist.map((w) => w.code).filter((c) => {
      const hit = riskCache.current.get(c)
      return !hit || Date.now() - hit.ts >= RISK_CACHE_TTL
    })
    if (!missing.length) return
    let cancelled = false
    Promise.allSettled(missing.map((c) => api.get(`/indicators/${c}/risk`))).then((results) => {
      if (cancelled) return
      const next: RiskInfo = {}
      results.forEach((r, i) => {
        const c = missing[i]
        const d = r.status === 'fulfilled' ? r.value?.data : null
        const val = { annual_volatility: d?.annual_volatility, max_drawdown: d?.max_drawdown }
        riskCache.current.set(c, { ts: Date.now(), v: val })
        next[c] = val
      })
      setRiskInfo((prev) => ({ ...prev, ...next }))
    })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchlist])

  const labelMap = useMemo(() => signalLabelMap(catalog), [catalog])
  const labelOf = (code: string) => labelMap[code] ?? { text: code, color: 'default' }

  // 统一订阅层：events(30s SWR 参数化池) + watchlist(60s SWR) 全站共享
  // 显式 14d + 仅关注 + 合理上限：绝不依赖后端缺省的近 3 日；窗口较原 30d 减半，
  // 关注页只需近期信号（今日新/有信号/触发列均以近 2 周为准，P2-74 全量拉取合理化）
  const eventsParams = useMemo(() => ({ watchlist_only: true, limit: 5000, time_range: '14d' }), [])
  const freshWatchlistState = useWatchlistState()
  const freshEventsState = useSignalEventsState(eventsParams)
  const freshWatchlist = freshWatchlistState.value
  const freshEvents = freshEventsState.value
  useEffect(() => {
    if (!freshWatchlist) return
    const pending = pendingRemoveRef.current
    if (pending.size === 0) {
      setWatchlist(freshWatchlist)
      return
    }
    // P2-61：过滤掉仍处于乐观移除中（服务端未确认）的 code，防池推送旧缓存「复活」；
    // 一旦 fresh 不再包含该 code（服务端已移除）即释放守卫
    setWatchlist(freshWatchlist.filter((w) => !pending.has(w.code)))
    for (const c of [...pending]) {
      if (!freshWatchlist.some((w) => w.code === c)) pending.delete(c)
    }
  }, [freshWatchlist])
  useEffect(() => {
    if (freshEvents) setEvents(freshEvents)
  }, [freshEvents])

  useEffect(() => {
    getSignalCatalog().then(setCatalog).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const groupsByCode = useMemo(() => {
    // 按股票合并（共享 utils/signals.groupSignalEvents），补充 WlGroup 特有字段：
    // sector 取首个非空、latestAt 取最新触发时间；事件顺序沿用后端 triggered_at 降序
    // （/events 数组端点保证降序 + groupSignalEvents 保序，无需每 poll 再排序，P2-74）
    const m = new Map<string, WlGroup>()
    for (const [code, g] of groupSignalEvents(events)) {
      let sector: string | undefined
      let latestAt: string | null = null
      for (const e of g.events) {
        if (e.sector && !sector) sector = e.sector
        // P2-65：latestAt 求最大用 toMarketEpochMs 数值比较（naive ISO 字典序在同日多事件时排序不稳）
        if (e.triggered_at && (!latestAt || toMarketEpochMs(e.triggered_at) > toMarketEpochMs(latestAt))) latestAt = e.triggered_at
      }
      m.set(code, { ...g, sector, latestAt })
    }
    return m
  }, [events])

  // ===== T-12 分组：列表（60s 池）+ 股票 → 分组 id 集合映射（行菜单勾选态 / 分组筛选共用） =====
  // 分组池失败静默（分组功能降级为空，不阻塞主列表）
  const groupsState = useWatchlistGroups()
  const groups = groupsState.value ?? []
  const codeGroups = useMemo(() => {
    const m = new Map<string, Set<number>>()
    for (const g of groups) {
      for (const c of g.codes) {
        let s = m.get(c)
        if (!s) { s = new Set(); m.set(c, s) }
        s.add(g.id)
      }
    }
    return m
  }, [groups])
  // 选中分组被删除 → 兜底回「全部」
  useEffect(() => {
    if (selectedGroup !== 'all' && !groups.some((g) => g.id === selectedGroup)) setSelectedGroup('all')
  }, [groups, selectedGroup])

  // 排序：有信号在前，无信号按代码序在后（两组内均按代码序）
  const rows = useMemo(() => {
    const withSig: WlRow[] = []
    const silent: WlRow[] = []
    for (const item of watchlist) {
      const group = groupsByCode.get(item.code)
      ;(group && group.signals.length > 0 ? withSig : silent).push({ item, group })
    }
    withSig.sort((a, b) => a.item.code.localeCompare(b.item.code))
    silent.sort((a, b) => a.item.code.localeCompare(b.item.code))
    return { withSig, silent }
  }, [watchlist, groupsByCode])

  const filtered = useMemo<WlRow[]>(() => {
    const base = filter === 'signal' ? rows.withSig
      : filter === 'silent' ? rows.silent
      : [...rows.withSig, ...rows.silent]
    const kw = keyword.trim().toLowerCase()
    // 本地搜索：名称/代码模糊匹配（大小写不敏感），与三段筛选 AND 叠加
    const byKw = !kw ? base : base.filter((r) =>
      r.item.code.toLowerCase().includes(kw) ||
      (r.item.name ?? '').toLowerCase().includes(kw),
    )
    // 分组筛选（T-12）：与三段筛选/搜索 AND 叠加；选中组被删时兜底为全部
    if (selectedGroup === 'all') return byKw
    const g = groups.find((x) => x.id === selectedGroup)
    if (!g) return byKw
    const gs = new Set(g.codes)
    return byKw.filter((r) => gs.has(r.item.code))
  }, [rows, filter, keyword, selectedGroup, groups])

  // ===== 摘要区指标（Toolbar 风格分段，压缩高度；每段带一个价值指标，替代原纯计数大卡）=====
  // 全部=今日新信号数（events 中 triggered_at 为上海今日的事件数，14d 关注池实时计算）；
  // 有信号=有信号股票数；静默=行情均涨跌（silent 项 pct_change 均值，无数据 → null）
  const todayNewCount = useMemo(() => {
    const today = cnTodayOf()
    return events.filter((e) => e.triggered_at && e.triggered_at.slice(0, 10) === today).length
  }, [events])

  const silentAvgPct = useMemo(() => {
    const pcts: number[] = []
    for (const r of rows.silent) {
      const v = Number(r.item.pct_change)
      if (Number.isFinite(v)) pcts.push(v)
    }
    if (!pcts.length) return null
    return pcts.reduce((a, b) => a + b, 0) / pcts.length
  }, [rows])

  // 有信号段价值指标：信号事件总条数（withSig 各股事件数之和，零成本取自已有 groupsByCode 数据）
  const signalEventCount = useMemo(
    () => rows.withSig.reduce((n, r) => n + (r.group?.events.length ?? 0), 0),
    [rows],
  )

  // ===== Copilot 上下文 provider（/watchlist）：关注集合 + 有信号 Top 榜，供 LLM 追问答 =====
  useCopilotProvider('/watchlist', {
    context: () => ({
      route: '/watchlist',
      title: '关注列表',
      stock_count: watchlist.length,
      signal_count: rows.withSig.length,
      silent_count: rows.silent.length,
      top_stocks: rows.withSig.slice(0, 10).map((r) => ({
        code: r.item.code, name: r.item.name,
        signals: r.group?.signals ?? [],
        triggered_at: r.group?.latestAt ?? null,
      })),
      silent_stocks: rows.silent.slice(0, 20).map((r) => r.item.code),
    }),
  })

  // 行模型时间依赖显式化：age/isNew 以 now 计算，静置时不会自动刷新 →
  // 60s tick 驱动 nowMs 状态变化，rowViews 随之重算（时间依赖不再被 useMemo 吞掉）
  const [nowMs, setNowMs] = useState(() => Date.now())
  useEffect(() => {
    const t = window.setInterval(() => {
      if (document.visibilityState === 'hidden') return // 后台不空转,回前台下一 tick 续上
      setNowMs(Date.now())
    }, 60_000)
    return () => window.clearInterval(t)
  }, [])
  const rowViews = useMemo(
    () => filtered.map((r) => toRowView(r, klineInfo, riskInfo, nowMs)),
    [filtered, klineInfo, riskInfo, nowMs])

  // 筛选/关注/分组/关键词变化时回到首屏（T-131:关键词漏项——之前 keyword 不在依赖,第 2 页搜索会显示空页）
  useEffect(() => { setWlPage(1); setMobileCount(20); setInfCount(20) }, [filter, watchlist.length, selectedGroup, keyword])
  useEffect(() => {
    const maxPage = Math.max(1, Math.ceil(rowViews.length / 25))
    if (wlPage > maxPage) setWlPage(maxPage)
  }, [rowViews.length, wlPage])

  const unfollow = async (code: string, name: string) => {
    const prev = watchlist
    // P2-61：乐观移除前登记守卫，池推送旧缓存期间不复活
    pendingRemoveRef.current.add(code)
    setWatchlist((p) => p.filter((w) => w.code !== code))
    try {
      await api.delete(`/market/watchlist/${code}`)
      invalidateWatchlist()
      invalidateSignalEvents()
      // 服务端已删除：下次 fresh 不含该 code 时守卫自动释放（sync effect）
      notifications.show({ color: 'green', message: `已取消关注 ${name || code}` })
    } catch (e: any) {
      pendingRemoveRef.current.delete(code)
      setWatchlist(prev)
      notifications.show({ color: 'red', message: '取消关注失败: ' + (e?.message ?? e) })
    }
  }

  const unfollowAll = async () => {
    if (unfollowBusyRef.current) return // in-flight 守卫：防 Popconfirm 连点并发全量请求
    unfollowBusyRef.current = true
    const prev = watchlist
    const codes = prev.map((w) => w.code)
    // P2-61：全部登记移除守卫（失败项在回滚时释放，成功项由 sync effect 在服务端确认后释放）
    for (const c of codes) pendingRemoveRef.current.add(c)
    setWatchlist([])
    try {
      const results = await Promise.allSettled(codes.map((c) => api.delete(`/market/watchlist/${c}`)))
      // 失败项按 code 反查原列表补回（精确回滚，不依赖 SWR 缓存时序）
      const failedCodes = codes.filter((_, i) => results[i]?.status === 'rejected')
      if (failedCodes.length > 0) {
        const failedSet = new Set(failedCodes)
        for (const c of failedCodes) pendingRemoveRef.current.delete(c)
        setWatchlist(prev.filter((w) => failedSet.has(w.code)))
        notifications.show({ color: 'orange', message: `${failedCodes.length} 只取消失败，已恢复这 ${failedCodes.length} 只关注` })
      } else {
        notifications.show({ color: 'green', message: `已全部取消关注（${codes.length} 只）` })
      }
      invalidateWatchlist()
      invalidateSignalEvents()
    } finally {
      unfollowBusyRef.current = false
    }
  }

  const openStock = (code: string) => navigate(`/stocks/${code}`)

  // ===== T-12 分组操作：移入/移出（行菜单）、建/改/删（管理 Modal）、导入/导出 =====
  const toggleGroup = async (code: string, groupId: number) => {
    const inGroup = codeGroups.get(code)?.has(groupId) ?? false
    try {
      if (inGroup) await removeWatchlistGroupItem(groupId, code)
      else await addWatchlistGroupItems(groupId, [code])
      invalidateWatchlistGroups()
    } catch (e: any) {
      notifications.show({ color: 'red', message: `${inGroup ? '移出' : '移入'}分组失败: ${errMsg(e)}` })
    }
  }

  const handleCreateGroup = async (name: string) => {
    await createWatchlistGroup(name)
    invalidateWatchlistGroups()
  }
  const handleRenameGroup = async (id: number, name: string) => {
    await renameWatchlistGroup(id, name)
    invalidateWatchlistGroups()
  }
  const handleDeleteGroup = async (id: number) => {
    await deleteWatchlistGroup(id)
    invalidateWatchlistGroups()
    // 删除的是当前选中分组 → 兜底回「全部」（effect 也会兜底，这里即时更新避免一帧空态）
    if (selectedGroup === id) setSelectedGroup('all')
  }

  const handleImport = (added: number, invalid: number) => {
    invalidateWatchlist()
    invalidateSignalEvents()
    notifications.show({
      color: 'green',
      // T-131:区分「已全部存在」与「存在无效代码」——原实现丢弃 invalid/total,全非法输入误报「均已关注」
      message: added > 0 ? `已导入关注 ${added} 只${invalid > 0 ? `（${invalid} 个代码无效已忽略）` : ''}`
        : invalid > 0 ? `未导入新关注：${invalid} 个代码无效`
        : '无新增（代码均已关注）',
    })
  }

  const handleExport = async () => {
    try {
      await downloadWatchlistCsv()
    } catch (e: any) {
      notifications.show({ color: 'red', message: '导出失败: ' + errMsg(e) })
    }
  }
  // 页面「刷新」：与全局 sr-refresh 广播一致的清理范围——watchlist/events 池失效 + kline 池/风险池失效
  // + 本页 kline 双层持久缓存与每代码一次缓存重置（refreshPage）；只失效不重复拉取（订阅方 SWR 自动重拉）
  const retry = () => {
    refreshPage()
    invalidateWatchlist()
    invalidateSignalEvents()
    invalidateKlines()
    invalidateRisk()
  }
  // 桌面无限滚动：表格内部滚动容器触底回调（哨兵在表格外，IO 感知不到表格滚动）；useCallback 稳定引用
  const loadMoreInf = useCallback(() => setInfCount((c) => c + 20), [])

  // 无限滚动开关：切换即回到首屏（筛选变化重置由上方 effect 兜底）
  const toggleInfinite = (next: boolean) => {
    setInfCount(20)
    setInfinite(next)
  }

  // 首载：等 watchlist 到齐再渲染，避免误显示"全部静默"；
  // T-131 错误分离：仅关注列表源失败才整页错误态；信号事件失败降级为横幅+重试
  //（不再因信号接口故障误报「关注列表加载失败」、阻断列表/分组/取消关注）
  const dataReady = freshWatchlist !== undefined
  const watchlistFailed = freshWatchlist === undefined && !!freshWatchlistState.error
  const eventsFailed = freshEvents === undefined && !!freshEventsState.error
  const firstLoadFailed = watchlistFailed
  const loadErrMsg = firstLoadFailed ? '关注列表加载失败: ' + errMsg(freshWatchlistState.error) : undefined

  // 无限滚动：客户端分批展示（每批 20）；分页模式：桌面 25/页、移动手动批次
  const shownCount = infinite
    ? Math.min(infCount, rowViews.length)
    : isMobile ? mobileCount : rowViews.length

  return (
    <PageShell
      fill={!isMobile}
      header={
        <PageHeader
          extra={
            <Group gap={8}>
              <Button
                size="compact-sm"
                leftSection={<IconChartLine size={14} />}
                onClick={() => navigate('/compare')}
                aria-label="多股对比"
              >
                多股对比
              </Button>
              <Button size="compact-sm" leftSection={<IconUpload size={14} />} onClick={() => setImportOpened(true)} aria-label="批量导入关注">
                导入
              </Button>
              <Button size="compact-sm" leftSection={<IconDownload size={14} />} onClick={() => void handleExport()} aria-label="导出 CSV">
                导出 CSV
              </Button>
              <Button size="compact-sm" leftSection={<IconRefresh size={14} />} onClick={retry} aria-label="刷新">刷新</Button>
              <Button
                size="compact-sm"
                leftSection={<IconTrash size={14} />}
                disabled={!watchlist.length}
                onClick={() => {
                  if (!watchlist.length) return
                  modals.openConfirmModal({
                    title: '确定全部取消关注？',
                    children: <Text size="sm">将移除当前 {watchlist.length} 只关注股票</Text>,
                    labels: { confirm: '全部取消', cancel: '取消' },
                    confirmProps: { color: 'red' },
                    onConfirm: () => void unfollowAll(),
                  })
                }}
              >
                全部取消关注
              </Button>
            </Group>
          }
        />
      }
    >
      <div className="sr-wl-page">
        <StatStrip
          loading={!dataReady}
          scrollable={isMobile}
          style={{ marginBottom: 'var(--sr-gap-row)' }}
          items={[
            { key: 'total', label: '关注', value: watchlist.length },
            { key: 'sig', label: '有信号', value: rows.withSig.length, sub: rows.withSig.length > 0 ? `信号 ${signalEventCount} 条` : undefined },
            { key: 'silent', label: '静默', value: rows.silent.length },
            { key: 'today', label: '今日新', value: dataReady ? todayNewCount : '—', tone: todayNewCount > 0 ? 'up' : 'plain' },
          ]}
        />
        <div className="sr-wl-segbar" role="group" aria-label="关注列表分段筛选">
          {WL_SEGMENTS.map((seg) => {
            const active = filter === seg.key
            const count = seg.key === 'all' ? watchlist.length
              : seg.key === 'signal' ? rows.withSig.length
              : rows.silent.length
            // 每段一个价值指标：全部=今日新信号数 / 有信号=信号事件总条数 / 静默=行情均涨跌（有涨跌着色）
            const metric = seg.key === 'all'
              ? (dataReady ? `今日新 ${todayNewCount}` : null)
              : seg.key === 'signal'
                ? (rows.withSig.length > 0 ? `信号 ${signalEventCount} 条` : null)
                : (silentAvgPct != null
                  ? (silentAvgPct >= 0 ? '+' : '') + fmtPct(silentAvgPct / 100, 1)
                  : null)
            const metricCls = seg.key === 'silent' ? (silentAvgPct != null ? (silentAvgPct >= 0 ? ' sr-wl-metric-up' : ' sr-wl-metric-down') : '')
              : ''
            return (
              <button
                key={seg.key}
                type="button"
                className={'sr-wl-seg' + (active ? ' sr-wl-seg-active' : '')}
                aria-pressed={active}
                onClick={() => setFilter(seg.key)}
              >
                <span className="sr-wl-seg-label">{seg.label}</span>
                <span className="sr-wl-seg-count">{count}</span>
                {metric && <span className={'sr-wl-seg-metric' + metricCls}>{metric}</span>}
              </button>
            )
          })}
        </div>

        {/* T-12 分组筛选条：全部分组 + 各组（搜索过滤），与三段筛选/搜索 AND 叠加；右侧入口分组管理 */}
        <Flex align="center" gap={8} wrap="wrap" className="sr-wl-groupbar">
          {/* T-131:分组接口失败不再伪装成「暂无分组」——显示错误+重试,防误建同名分组 */}
          {groupsState.error ? (
            <>
              <Text c="red" style={{ fontSize: 'var(--sr-font-sm)' }}>分组加载失败</Text>
              <Button size="xs" variant="subtle" leftSection={<IconRefresh size={14} />}
                onClick={() => invalidateWatchlistGroups()} aria-label="重试加载分组">重试</Button>
            </>
          ) : (
          <Select
            size="xs"
            className="sr-wl-groupsel"
            placeholder="全部分组"
            data={[
              { value: 'all', label: `全部分组（${watchlist.length}）` },
              ...groups.map((g) => ({ value: String(g.id), label: `${g.name}（${g.count}）` })),
            ]}
            value={selectedGroup === 'all' ? 'all' : String(selectedGroup)}
            onChange={(v) => setSelectedGroup(v === 'all' || v == null ? 'all' : Number(v))}
            searchable
            clearable
            aria-label="按分组筛选关注列表"
            style={{ width: 200, flexShrink: 0, flexGrow: 0 }}
          />
          )}
          <Button
            size="xs"
            variant="default"
            leftSection={<IconFolderCog size={14} />}
            onClick={() => setGroupManagerOpened(true)}
            aria-label="分组管理"
          >
            分组管理
          </Button>
        </Flex>

        <Flex align="center" gap={8} wrap="wrap" className="sr-wl-toolbar">
          <InfiniteScrollToggle
            checked={infinite}
            onChange={toggleInfinite}
            storageKey="sr-wl-infinite"
            label="无限滚动"
          />
          <TextInput
            size="sm"
            leftSection={<IconSearch size={14} style={{ color: 'var(--sr-text-3)' }} />}
            placeholder="搜索名称 / 代码"
            value={keyword}
            onChange={(e) => setKeyword(e.currentTarget.value)}
            style={{ width: 180, flexShrink: 0, flexGrow: 0 }}
            aria-label="搜索关注股票"
            rightSection={keyword ? (
              <button
                type="button"
                aria-label="清除搜索"
                onClick={() => setKeyword('')}
                style={{
                  display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                  background: 'none', border: 'none', cursor: 'pointer', padding: 0,
                  color: 'var(--sr-text-3)',
                }}
              >
                <IconX size={14} />
              </button>
            ) : undefined}
          />
        </Flex>

        {/* T-131:信号事件接口失败但关注列表正常——降级横幅提示+重试,不再整页误报「关注列表加载失败」 */}
        {eventsFailed && dataReady && !firstLoadFailed && (
          <div className="sr-wl-state-inline" role="alert">
            <Text c="red" style={{ fontSize: 'var(--sr-font-sm)' }}>信号数据加载失败，信号列暂不可用</Text>
            <Button size="xs" variant="subtle" leftSection={<IconRefresh size={14} />}
              onClick={() => invalidateSignalEvents()} aria-label="重试加载信号">重试</Button>
          </div>
        )}

        {firstLoadFailed ? (
          <div className="sr-wl-state">
            <EmptyState text={loadErrMsg} onRetry={retry} />
          </div>
        ) : !dataReady ? (
          <div className="sr-wl-state">
            {/* 桌面表格首载 6 行骨架；移动端卡片态保持 4 行（紧凑节奏） */}
            <SkeletonBlock rows={isMobile ? 4 : 6} />
          </div>
        ) : watchlist.length === 0 ? (
          <EmptyState
            description={
              <span style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <span>暂无关注股票</span>
                <Text component="span" style={{ color: 'var(--sr-text-2)', fontSize: 'var(--sr-font-sm)' }}>
                  可在「信号中心 / 个股工作台」点击星标添加关注
                </Text>
              </span>
            }
            padding="40px 0"
          />
        ) : filtered.length === 0 ? (
          <EmptyState
            description={keyword.trim()
              ? `未找到匹配「${keyword.trim()}」的关注股票`
              : selectedGroup !== 'all'
                ? '该分组暂无股票，可在行操作「移入分组」中添加'
                : (filter === 'silent' ? '暂无静默的关注股票' : '暂无有信号的关注股票')}
            padding="40px 0"
          />
        ) : isMobile ? (
          <MobileWatchlist
            rows={rowViews}
            visibleCount={shownCount}
            infinite={infinite}
            onLoadMore={() => setMobileCount((c) => c + 20)}
            onOpen={openStock}
            onUnfollow={(v) => unfollow(v.code, v.name)}
            labelOf={labelOf}
            groups={groups}
            codeGroups={codeGroups}
            onToggleGroup={toggleGroup}
            onManageGroups={() => setGroupManagerOpened(true)}
          />
        ) : (
          <WatchlistTable
            rows={infinite ? rowViews.slice(0, shownCount) : rowViews}
            current={wlPage}
            infinite={infinite}
            onPageChange={setWlPage}
            onOpen={openStock}
            onUnfollow={(v) => unfollow(v.code, v.name)}
            labelOf={labelOf}
            onReachBottom={infinite ? loadMoreInf : undefined}
            groups={groups}
            codeGroups={codeGroups}
            onToggleGroup={toggleGroup}
            onManageGroups={() => setGroupManagerOpened(true)}
          />
        )}

        {infinite && dataReady && filtered.length > 0 && (
          <InfiniteScrollSentinel
            hasMore={shownCount < rowViews.length}
            loadMore={() => setInfCount((c) => c + 20)}
            enabled
            resetKey={filter}
            doneText="已加载全部"
            loadMoreText="加载更多"
          />
        )}
      </div>

      {/* T-12 分组管理 / 批量导入 Modal（portal 挂载，逻辑收敛在子组件） */}
      <WatchlistGroupManager
        opened={groupManagerOpened}
        onClose={() => setGroupManagerOpened(false)}
        groups={groups}
        error={groupsState.error != null}
        onRetry={() => invalidateWatchlistGroups()}
        onCreate={handleCreateGroup}
        onRename={handleRenameGroup}
        onDelete={handleDeleteGroup}
      />
      <WatchlistImportModal
        opened={importOpened}
        onClose={() => setImportOpened(false)}
        onImported={handleImport}
      />
    </PageShell>
  )
}
