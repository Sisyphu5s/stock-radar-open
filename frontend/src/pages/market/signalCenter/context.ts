import { createContext, useContext } from 'react'
import type { SrColumn } from '../../../components/ui/tableTypes'
import type { PageStats, ScanUniverse, SignalCatalogItem, SignalEvent, SignalTimeRange } from '../../../api/client'
import type { ViewportMode } from '../../../app/useViewport'
import { loadSplitSizes, persistSplitSizes } from '../../../utils/splitPersist'
import type { SplitConstraints } from '../../../utils/splitPersist'

export const SECTOR_OPTIONS = ['沪主板', '深主板', '创业板', '科创板', '北交所', '其他']
export { SECTOR_COLOR } from '../../../utils/signals'
// 周期常量统一在 utils/periods.ts（唯一事实源），此处 re-export 保持既有 import 方兼容
export { PERIOD_OPTIONS } from '../../../utils/periods'

// ===== 信号触发时间范围（服务端筛选，缺省近3日） =====
/** 与 API 契约单一来源（client.ts SignalTimeRange），不复制 union */
export type TimeRangeValue = SignalTimeRange
/** 今日 / 近3日 / 近7日 / 近2周 / 近30日 / 近2月 / 近3月 / 全部；
 *  「全部」也显式发送 time_range=all（后端缺省 3d，不发送会回落）。
 *  T-104 扩展近2周/近2月/近3月（后端已支持 14d/60d/90d） */
export const TIME_RANGE_OPTIONS: { label: string; value: TimeRangeValue }[] = [
  { label: '今日', value: 'today' },
  { label: '近3日', value: '3d' },
  { label: '近7日', value: '7d' },
  { label: '近2周', value: '14d' },
  { label: '近30日', value: '30d' },
  { label: '近2月', value: '60d' },
  { label: '近3月', value: '90d' },
  { label: '全部', value: 'all' },
]
export const TIME_RANGE_DEFAULT: TimeRangeValue = '3d'
export const isTimeRangeValue = (v: unknown): v is TimeRangeValue =>
  TIME_RANGE_OPTIONS.some((o) => o.value === v)

// ===== 多信号命中语义（服务端筛选，持久化缺省任一） =====
/** 信号类型筛选命中语义：any 任一命中（OR）/ all 全部命中（AND）；与 API 契约（SignalEventsPageParams.signal_match）同 union */
export type SignalMatch = 'any' | 'all'
export const MATCH_OPTIONS: { label: string; value: SignalMatch }[] = [
  { label: '任一', value: 'any' },
  { label: '全部', value: 'all' },
]
export const isSignalMatch = (v: unknown): v is SignalMatch =>
  v === 'any' || v === 'all'

export const DIR_KEY = 'sr-signal-dir'
/** 信号方向：-1=排除（过滤掉命中该信号的股票）/ 0=无操作。
 * 「只看（1）」已废弃：语义并入服务端 signalFilter（SignalStreamPanel 工具条信号多选为唯一入口，
 * 可配合命中语义=全部转 AND）。类型保留三态以兼容持久化旧数据——loadDir 会把旧数据中 dir=1
 * 归一为 0，setDirOf 只应写入 0/-1。排除为浏览筛选（sessionStorage 持久化，与其他筛选一致，
 * 关浏览器即丢，行为可预期；T-27 由 localStorage 迁移） */
export type SignalDir = 1 | 0 | -1
export const evPct = (ev: SignalEvent['evidence'] | undefined) => {
  const v = ev?.pct_change
  return typeof v === 'number' ? v : Number(v ?? 0)
}
export const loadDir = (): Record<string, SignalDir> => {
  try {
    const raw = JSON.parse(sessionStorage.getItem(DIR_KEY) ?? '{}') as Record<string, unknown>
    const out: Record<string, SignalDir> = {}
    for (const [k, v] of Object.entries(raw)) {
      // 旧数据迁移：「只看（1）」已废弃并入服务端 signalFilter，加载时归一为 0（无操作），仅排除（-1）保留
      if (v === 0 || v === -1) out[k] = v as SignalDir
    }
    return out
  } catch {
    return {}
  }
}

// ===== 命名筛选方案（T-13）：完整筛选现场快照，localStorage 持久化（sr-signal-schemes） =====
/** 方案保存的筛选现场：全部服务端筛选字段 + 浏览层 dir 排除（dir 一并保存/应用，
 *  方案语义 = 完整筛选现场，见 SignalCenter.applyFilters 注释） */
export interface SignalSchemeFilters {
  period: string
  timeRange: TimeRangeValue
  sectors: string[]
  signalFilter: string[]
  signalMatch: SignalMatch
  watchlistOnly: boolean
  excludeToday: boolean
  dir: Record<string, SignalDir>
}

export interface SignalScheme {
  id: string
  name: string
  createdAt: string
  filters: SignalSchemeFilters
}

// 同一股票多模板信号合并为一行（展开查看明细事件）。
// status 字段曾为分组状态机（确认/已忽略/观察）产物，审计确认无任何消费方，已随分组逻辑
// 去重（utils/signals.groupSignalEvents）一并移除
export interface StockGroup {
  code: string
  name: string
  events: SignalEvent[]
  signals: string[]
  best: SignalEvent
  latest: SignalEvent
  triggered_at: string | null
  /** 数据截至时刻（取 latest 事件的 as_of；无则 null），时点列展示与盘中判定用 */
  as_of: string | null
  /** 扫描发现时刻（取 latest 事件的 scan_discovered_at；T-80 新增，旧数据 null）。
   *  与 as_of 同取 latest 事件：双时间展示 = triggered_at 自然触发 + 发现时刻 + 时间差 */
  discovered_at: string | null
}

/** 分组排序状态：columnKey 为排序列标识（列 dataIndex 或自定义 key），order 为 antd Table 升/降序语义；null=未启用排序 */
export interface SignalSortState {
  columnKey: string
  order: 'ascend' | 'descend'
}

export interface SignalCenterApi {
  // ===== 事件流筛选（服务端筛选驱动分页） =====
  period: string
  setPeriod: (p: string) => void
  sectors: string[]
  setSectors: (v: string[]) => void
  signalFilter: string[]
  setSignalFilter: (v: string[]) => void
  /** 多信号命中语义（持久化，默认 any）：任一命中（OR）/ 全部命中（AND），服务端筛选；
   *  单选或未选时 UI 隐藏且值自动重置为 any（T-27：不再静默保留无意义的值，角标与可见条件一致） */
  signalMatch: SignalMatch
  setSignalMatch: (v: SignalMatch) => void
  watchlistOnly: boolean
  setWatchlistOnly: (v: boolean) => void
  /** 信号时间范围（持久化；持久化旧值非法时回退近3日），服务端筛选 */
  timeRange: TimeRangeValue
  setTimeRange: (v: string) => void
  /** 排除今日（持久化；默认不排除），服务端筛选（market_time_range 口径） */
  excludeToday: boolean
  setExcludeToday: (v: boolean) => void
  catalog: SignalCatalogItem[]
  signalCategories: { category: string; codes: string[]; color: string }[]
  labelOf: (code: string) => { text: string; color: string }

  // ===== 无限滚动（事件流）：按 offset 增量拉取，已加载集合内分组/排序/浏览筛选 =====
  /** 已加载事件按股票分组后、应用浏览筛选（信号筛选/排除）的结果 */
  filteredGroups: StockGroup[]
  /** 分组排序状态（列排序；null=未启用排序）；SignalCenter 对 filteredGroups 排序的依据 */
  sortState: SignalSortState | null
  setSortState: (s: SignalSortState | null) => void
  /** 对 filteredGroups 按 sortState 排序后的结果（排序未启用时恒等于 filteredGroups，供表格渲染） */
  sortedGroups: StockGroup[]
  /** 浏览筛选后的分组数（已加载集合的展示行数） */
  groupCount: number

  // ===== 按周期扫描（服务端异步计算任务） =====
  /** 当前周期扫描是否计算中（驱动触发按钮 loading 与扫描进度展示） */
  scanBusy: boolean
  /** 扫描任务进度百分比（0-100；经 GET /experiments/{job_id} 轮询 market_scan job 获取，
   *  尚未拿到时 null——确定进度条 + 阶段文案兜底） */
  scanProgress: number | null
  /** 取消当前扫描（DELETE /experiments/{job_id}，后端协作取消，进度回调控制点生效） */
  cancelScan: () => void
  /** 各周期最后扫描时刻：key 为周期标识（'daily'/'weekly'/'monthly'/'1'/...），
   *  value 为该周期最后扫描时刻 naive ISO（未扫描为 null），来自 GET /signals/scan/status */
  periodScanAt: Record<string, string | null>
  /** 触发指定周期全市场扫描计算（POST /signals/scan）；执行期间 scanBusy 置 true，结束后刷新 periodScanAt */
  triggerScan: (period: string) => Promise<boolean>

  // ===== T-54 分钟扫描范围（持久化 sr-scan-universe；分钟周期生效，驱动扫描触发/口径文案） =====
  /** 分钟扫描股票池范围：watchlist 自选（缺省）/ top_n 成交额前 N（前端 UI 暴露两项；
   *  codes 由后端直连等其他客户端使用，UI 不提供入口） */
  scanUniverse: ScanUniverse
  setScanUniverse: (v: ScanUniverse) => void
  /** 首次加载 / 筛选或翻页换参数（无缓存值） */
  pageLoading: boolean
  /** 后台刷新（SWR/轮询，有旧值） */
  refreshing: boolean
  pageError: boolean
  reload: () => void

  // ===== 客户端无限滚动（事件流）：按 offset 增量拉取，事件 id 去重 + 跨批按股票合并 =====
  /** 跨批累计、按事件 id 去重后的全部事件（供分组/浏览筛选） */
  infEvents: SignalEvent[]
  /** 服务端筛选条件下的事件总数 */
  infTotal: number
  /** 筛选口径聚合统计（最近一批响应 stats：今日新增事件数 + 去重股票数，全量聚合不受分页截断） */
  infStats: PageStats | undefined
  infHasMore: boolean
  infLoading: boolean
  infError: boolean
  /** 无限滚动重置键（筛选/周期变化时自增，驱动哨兵废弃在途请求） */
  infResetKey: number
  loadInfiniteMore: () => void | Promise<unknown>

  // ===== 信号流表格列 =====
  /** 信号流表格列（SignalCenter 传入，SignalStreamPanel 按 title remap 统一渲染） */
  groupCols: SrColumn<StockGroup>[]
  detailColumns: SrColumn<SignalEvent>[]

  // 方向排除（dir=-1 作用于事件流浏览；「只看」已废弃并入服务端 signalFilter）
  dir: Record<string, SignalDir>
  dirOf: (code: string) => SignalDir
  setDirOf: (code: string, v: SignalDir) => void
  /** 清除全部排除（dir 全置 0 并立即持久化） */
  clearDir: () => void

  // ===== 命名筛选方案（T-13）：保存/应用完整筛选现场 =====
  /** 已保存方案列表（localStorage 持久化，上限 50） */
  schemes: SignalScheme[]
  /** 当前激活（已应用且与当前筛选一致）的方案 id；null=无激活（筛选被手动改动后自动清除） */
  activeSchemeId: string | null
  /** 保存当前筛选为命名方案（name 去空格，空名忽略） */
  saveScheme: (name: string) => void
  /** 应用方案：完整筛选快照写回（含 dir），触发既有防抖重拉链路 */
  applyScheme: (id: string) => void
  /** 删除方案（同时清除其激活标记） */
  removeScheme: (id: string) => void

  // 布局
  viewport: ViewportMode
  isDesktop: boolean
  isMobile: boolean

  // ===== T-53 关注流左栏：窄屏抽屉状态 + 关注计数（页面级单一订阅，入口按钮/左栏共用） =====
  /** 窄屏（<992）关注流抽屉开关（左栏入口按钮 ↔ Drawer 复用 WatchlistPane） */
  watchlistPaneOpen: boolean
  setWatchlistPaneOpen: (v: boolean) => void
  /** 关注股票数（SignalCenter 页面级 useWatchlist 订阅，左栏头部「关注 N」与窄屏入口 Badge 共用） */
  watchlistCount: number
  /** 关注成员集合（页级单一订阅派生，P2-68）：行内星标 O(1) 成员查找，替代每行独立订阅 useWatchlist */
  watchlistCodes: ReadonlySet<string>
}

export const SignalCenterCtx = createContext<SignalCenterApi | null>(null)

export const useSignalCenter = (): SignalCenterApi => {
  const ctx = useContext(SignalCenterCtx)
  if (!ctx) throw new Error('useSignalCenter 必须在 SignalCenterCtx.Provider 内使用')
  return ctx
}

// ===== T-53 左右分栏 Splitter：左栏关注流 / 右栏全市场信号流（桌面档） =====
/** 持久化键（localStorage，与工作台 sr-wb-hsplit 同制式） */
export const SIGNAL_SPLIT_KEY = 'sr-signal-split'
/** 默认比例（%，左/右）：22%≈320px@1440 内容区（任务卡 320px）；右栏 78% */
export const SIGNAL_SPLIT_DEFAULT: [number, number] = [22, 78]
/** 左栏比例下限（%，≈260px@1440，任务卡 min 260px；随容器等比收缩，同工作台注释惯例） */
export const SIGNAL_PANE_MIN = 18
/** 左栏比例上限（%，任务卡 max 45%） */
export const SIGNAL_PANE_MAX = 45
/** 左右分栏 pane 约束（%）：左栏 [18,45] / 右栏 [55,82] → left 有效 [18,45]，right = 100 − left */
const SIGNAL_SPLIT_CONSTRAINTS: SplitConstraints = { min: SIGNAL_PANE_MIN, max: SIGNAL_PANE_MAX, min2: 100 - SIGNAL_PANE_MAX, max2: 100 - SIGNAL_PANE_MIN }

/** 读取持久化比例（共享钳制工具：单变量钳制 + sum=100，越界/非法回退默认） */
export const loadSignalSplit = (): [number, number] =>
  loadSplitSizes(SIGNAL_SPLIT_KEY, SIGNAL_SPLIT_CONSTRAINTS, SIGNAL_SPLIT_DEFAULT)
/** 持久化比例（onResizeEnd 调用；只存左栏，右栏 = 100 − left，与工作台节奏一致） */
export const persistSignalSplit = (sizes: number[]) => persistSplitSizes(SIGNAL_SPLIT_KEY, sizes)
