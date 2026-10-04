/**
 * ===== 三通道缓存职责边界（P1-41 收敛说明，2026-08-14） =====
 * ① TanStack Query 池（src/data/queryBase.ts queryClient 单例）——轮询/实时数据的唯一通道：
 *   kline/quote/events/jobs/watchlist/indicators 等全部 data 模块池；observer 级订阅计数、
 *   staleTime=TTL、gcTime=惰性淘汰、visibility 停表，跨会话不持久。
 * ② persistedGet（sr-cache: localStorage）——唯一授权跨会话持久通道：仅限低频只读资源
 *   （当前使用面 = 关注页迷你 K 线 getKline，5min 短 TTL）；含配额上限/按最旧驱逐/值内版本字段。
 *   轮询接口禁走本通道（持久缓存会把轮询架空，见 data/kline.ts、data/indicators.ts 迁移注释）。
 * ③ MEM（本模块内存热层）——persistedGet 的内部实现细节（会话内热缓存，避免同会话重复读
 *   localStorage），非独立通道，模块私有、外部禁止直接读写；失效入口 = invalidateCache（热层）
 *   + invalidatePersistedCache（持久层）配对调用。
 * 决策依据：新场景先评估 Query 池；跨会话低频只读才授权走 persistedGet；二者之外禁止新开缓存通道。
 */
import axios, { getAdapter } from 'axios'
import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { KEY_PREFIXES } from '../utils/storageKeys'

export const api = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
})

// ===== MEM 会话内热层（persistedGet 内部实现细节，非独立通道） =====
// persistedGet 的会话内热缓存：同一 key 命中即返回内存值，避免同会话重复读 localStorage。
// 模块私有、外部禁止直接读写；失效入口为 invalidateCache（热层）+ invalidatePersistedCache（持久层）配对。
const MEM: Record<string, { ts: number; data: any }> = {}

/** 失效 persistedGet 的会话内热层（MEM）——与 invalidatePersistedCache 配对使用（见 WatchlistPage 刷新）。 */
export function invalidateCache(keyPrefix?: string) {
  if (keyPrefix === undefined) {
    for (const k of Object.keys(MEM)) delete MEM[k]
    return
  }
  for (const k of Object.keys(MEM)) {
    if (k.startsWith(keyPrefix)) delete MEM[k]
  }
}

/** sr-cache: 前缀（注册表 KEY_PREFIXES.persistCache）；持久缓存值内版本见 PERSIST_VERSION */
const PERSIST_PREFIX = KEY_PREFIXES.persistCache
const PERSIST_VERSION = 1
const PERSIST_MAX_TOTAL = 3 * 1024 * 1024
const PERSIST_EVICT_TO = 2.5 * 1024 * 1024
const PERSIST_DEFAULT_MAX_BYTES = 512 * 1024

const byteSize = (s: string) => new TextEncoder().encode(s).length

// ===== persisted localStorage 内存索引（写路径 O(1)，替代每次写全表扫描） =====
// 模块内所有 sr-cache: 键的读写删均经此索引同步；索引惰性构建（首次写前全表扫一次）。
// 外部代码直接改 localStorage 的 sr-cache: 键不会进索引——项目内无此用法，容忍即可；
// 若确有需要，可调用 rebuildPersistedIndex() 全量重建。
interface PersistIndexEntry { ts: number; size: number }
let persistIndex: Map<string, PersistIndexEntry> | null = null
let persistTotalBytes = 0

function rebuildPersistedIndex() {
  const idx = new Map<string, PersistIndexEntry>()
  let total = 0
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (!k || !k.startsWith(PERSIST_PREFIX)) continue
    const raw = localStorage.getItem(k) ?? ''
    let ts = Infinity
    try { ts = JSON.parse(raw).ts ?? 0 } catch { /* ignore */ }
    const size = byteSize(raw)
    idx.set(k, { ts, size })
    total += size
  }
  persistIndex = idx
  persistTotalBytes = total
}

function ensurePersistIndex() {
  if (persistIndex === null) rebuildPersistedIndex()
}

function indexPersistEntry(key: string, size: number) {
  // 同 key 覆盖（如 TTL 到期重写）需先扣除旧尺寸，避免 persistTotalBytes 重复累计
  const prev = persistIndex?.get(key)
  if (prev) persistTotalBytes -= prev.size
  persistIndex?.set(key, { ts: Date.now(), size })
  persistTotalBytes += size
}

function unindexPersistEntry(key: string) {
  const e = persistIndex?.get(key)
  if (e) {
    persistIndex!.delete(key)
    persistTotalBytes -= e.size
  }
}

function readPersisted<T>(key: string, ttlMs: number): T | undefined {
  try {
    const raw = localStorage.getItem(PERSIST_PREFIX + key)
    if (raw === null) return undefined
    const parsed = JSON.parse(raw) as { v?: number; ts: number; data: T }
    if (parsed.v !== PERSIST_VERSION || typeof parsed.ts !== 'number' || Date.now() - parsed.ts >= ttlMs) {
      removePersisted(key)
      return undefined
    }
    return parsed.data
  } catch {
    try { removePersisted(key) } catch { /* ignore */ }
    return undefined
  }
}

function evictPersistedIfNeeded(extraBytes: number) {
  ensurePersistIndex()
  const idx = persistIndex!
  if (persistTotalBytes + extraBytes <= PERSIST_MAX_TOTAL) return
  // 按最旧 ts 排序驱逐（等价原全表扫描语义，但来自索引，写路径 O(1)）
  const entries = [...idx.entries()].sort((a, b) => a[1].ts - b[1].ts)
  for (const [k, e] of entries) {
    if (persistTotalBytes <= PERSIST_EVICT_TO) break
    try { localStorage.removeItem(k) } catch { /* ignore */ }
    idx.delete(k)
    persistTotalBytes -= e.size
  }
}

function writePersisted<T>(key: string, data: T, maxBytes = PERSIST_DEFAULT_MAX_BYTES) {
  try {
    const raw = JSON.stringify({ v: PERSIST_VERSION, ts: Date.now(), data })
    if (byteSize(raw) > maxBytes) return
    ensurePersistIndex()
    evictPersistedIfNeeded(byteSize(raw))
    localStorage.setItem(PERSIST_PREFIX + key, raw)
    indexPersistEntry(key, byteSize(raw))
  } catch { /* ignore quota errors / unavailable storage */ }
}

function removePersisted(key: string) {
  try { localStorage.removeItem(PERSIST_PREFIX + key) } catch { /* ignore */ }
  unindexPersistEntry(key)
}

export async function persistedGet<T>(key: string, fetcher: () => Promise<T>, ttlMs = 60 * 60 * 1000, opts?: { maxBytes?: number }): Promise<T> {
  const hit = MEM[key]
  if (hit && Date.now() - hit.ts < ttlMs) return hit.data
  const stored = readPersisted<T>(key, ttlMs)
  if (stored !== undefined) {
    MEM[key] = { ts: Date.now(), data: stored }
    return stored
  }
  let data: T
  try {
    data = await fetcher()
  } catch (e) {
    removePersisted(key)
    throw e
  }
  MEM[key] = { ts: Date.now(), data }
  writePersisted(key, data, opts?.maxBytes)
  return data
}

export function invalidatePersistedCache(keyPrefix?: string) {
  const prefix = PERSIST_PREFIX + (keyPrefix ?? '')
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (k && k.startsWith(prefix)) {
      try { localStorage.removeItem(k) } catch { /* ignore */ }
      unindexPersistEntry(k)
    }
  }
}

/** 参数 → 稳定缓存 key：过滤空值 + 键名排序 + encodeURIComponent，同参数必得同 key */
export function stableParamKey(params: Record<string, unknown>): string {
  return Object.entries(params)
    .filter(([, v]) => v != null && v !== '')
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
    .join('&')
}

/** stableParamKey 的互逆解析：key → 参数（值已 decode）；空 key 返回空对象 */
export function parseParamKey(key: string): Record<string, string> {
  const params: Record<string, string> = {}
  if (!key) return params
  for (const kv of key.split('&')) {
    if (!kv) continue
    const idx = kv.indexOf('=')
    if (idx < 0) continue
    params[kv.slice(0, idx)] = decodeURIComponent(kv.slice(idx + 1))
  }
  return params
}

// ===== 在途请求去重（adapter 层 key 化，P2-2） =====
// 同 key（方法 + URL + 参数 + 请求体）的并发请求共享同一 Promise，完成/失败即清 key——
// 覆盖全部非池化 getXxx 接口（kline/quote/events/jobs/watchlist 已有上层查询池，adapter 层
// 对它们同样生效，仅消除同帧并发重复请求）；POST/PUT/DELETE 有副作用，不参与去重。
// 不改 persistedGet / 查询池结构，API 契约不变。
const RAW_ADAPTER = getAdapter(axios.defaults.adapter)

const inFlight = new Map<string, Promise<AxiosResponse>>()

const dedupeKey = (config: InternalAxiosRequestConfig): string => {
  const params = config.params ? stableParamKey(config.params as Record<string, unknown>) : ''
  const data = config.data ? JSON.stringify(config.data) : ''
  return `${String(config.method ?? 'get').toUpperCase()} ${config.url ?? ''} ${params} ${data}`
}

const dedupeAdapter: AxiosAdapter = (config) => {
  if (String(config.method ?? 'get').toUpperCase() !== 'GET') return RAW_ADAPTER(config)
  const key = dedupeKey(config)
  const pending = inFlight.get(key)
  if (pending) return pending
  const p = RAW_ADAPTER(config).then(
    (r) => { inFlight.delete(key); return r },
    (e) => { inFlight.delete(key); throw e },
  )
  inFlight.set(key, p)
  return p
}

api.defaults.adapter = dedupeAdapter

/** 分页筛选口径的聚合统计（GET /signals/events/page 返回；全量聚合，不受分页截断） */
export interface PageStats {
  /** 今日新增事件数：triggered_at 落 Asia/Shanghai 今日零点后（筛选口径内） */
  today_new: number
  /** 筛选口径内的去重股票数（全量聚合，非当前页） */
  stock_count: number
}

/** 通用分页响应契约（后端 /signals/events/page 与 /experiments/page 共用） */
export interface PageResult<T> {
  items: T[]
  total: number
  limit: number
  offset: number
  has_more: boolean
  period?: string
  /** 信号事件分页端点返回的筛选口径聚合统计；实验分页端点无此字段 */
  stats?: PageStats
}

export async function searchStocks(q: string): Promise<{ code: string; name: string }[]> {
  const { data } = await api.get('/stocks/search', { params: { q } })
  return data.data
}

export async function getKline(code: string, period = 'daily', days = 300) {
  // 仅服务低频读场景（关注页迷你 K线）：统一 5min 短 TTL，与 60s 行情节奏匹配，
  // 避免持久缓存命中 6h 陈旧图；分钟周期同样 5min（迷你图非轮询，低频拉取即可）
  const ttl = 5 * 60 * 1000
  return persistedGet(`kline:${code}:${period}:${days}`, async () => {
    const { data } = await api.get(`/stocks/${code}/kline`, { params: { period, days } })
    return data
  }, ttl)
}

export async function getQuote(code: string) {
  const { data } = await api.get(`/stocks/${code}/quote`)
  return data
}

export interface SignalEvent {
  id: number
  stock_code: string
  stock_name?: string
  /** 命中信号类型列表；后端契约允许 null（旧行/异常数据），消费方必须空值守卫（T-129） */
  signals: string[] | null
  status: string
  evidence: Record<string, string | number | null>
  triggered_at: string | null
  /** 数据截至时刻（naive ISO，可能含秒；无则 null）。展示时刻 = as_of ?? triggered_at：
   *  日/周/月 = 交易日 15:00，分钟周期恒 null（展示回退 triggered_at） */
  as_of: string | null
  /** 扫描发现时刻（naive ISO，可空；T-80 新增）。信号中心双时间展示：triggered_at 自然触发
   *  + scan_discovered_at 发现时刻 + 时间差；旧数据 null 时只显示单时间 */
  scan_discovered_at: string | null
  /** 信号周期（'daily'/'weekly'/'monthly'，分钟周期为 '1'/'5'/'15'/...）。
   *  后端 /signals/events 与 /signals/events/page 恒输出（与 notifications 同源，旧行迁移默认 'daily'，
   *  T-97 契约对齐后不再缺失） */
  period: string
  is_watchlist?: boolean
  sector?: string
}

export async function getSignalEvents(params: Record<string, unknown> = {}): Promise<SignalEvent[]> {
  const { data } = await api.get('/signals/events', { params })
  return data.data
}

/** 信号触发时间范围（服务端筛选；后端缺省 3d，前端必须始终显式发送，含 all）。
 *  2026-08-15 T-104 扩展：新增 14d/60d/90d（近2周/近2月/近3月，Nd=含今天在内最近 N 个自然日） */
export type SignalTimeRange = 'today' | '3d' | '7d' | '14d' | '30d' | '60d' | '90d' | 'all'

export interface SignalEventsPageParams {
  limit?: number
  offset?: number
  status?: string
  code?: string
  sector?: string
  watchlist_only?: boolean
  period?: string
  /** 逗号分隔信号类型，OR 语义（命中任一即保留） */
  signal_types?: string
  /** 信号触发时间范围（today/3d/7d/30d/all，市场日历口径含今日）；后端缺省 3d，必须始终显式发送（含 all） */
  time_range?: SignalTimeRange
  /** 排除当日：过滤掉市场日期为今天的事件（不改变 time_range 的起始边界） */
  exclude_today?: boolean
  /** 信号匹配语义：any=命中任一信号类型即保留（OR），all=全部命中才保留（AND）；缺省 any */
  signal_match?: 'any' | 'all'
}

/** 分页信号事件（GET /signals/events/page）：筛选（信号类型服务端执行）+分页参数全量下传，返回通用 PageResult */
export async function getSignalEventsPage(params: SignalEventsPageParams = {}): Promise<PageResult<SignalEvent>> {
  const { data } = await api.get('/signals/events/page', { params })
  return data
}

/** 信号扫描状态（GET /signals/scan/status）：最后扫描时刻 + 当前是否处于交易时段 */
export interface ScanStatus {
  /** 最后扫描时刻（naive ISO；从未扫描为 null） */
  last_scan_at: string | null
  /** 当前是否处于交易时段 */
  in_trading_session: boolean
  /** 各周期最后扫描时刻：key 为周期标识（'daily'/'weekly'/'monthly'/'1'/...），
   *  value 为该周期最后扫描时刻 naive ISO（未扫描为 null） */
  periods?: Record<string, string | null>
  /** 扫描调度表：周期 → 间隔秒（单一事实源后端 Settings.scan_schedule；T-54 新增，
   *  供前端新鲜度阈值按当前周期自适应，缺失时回退 5min 常量） */
  schedule?: Record<string, number>
}

export async function getScanStatus(): Promise<ScanStatus> {
  const { data } = await api.get('/signals/scan/status')
  return data
}

/** 扫描触发响应（POST /signals/scan）：异步任务已受理；status 为任务状态，label 为任务描述 */
export interface SignalScanTrigger {
  job_id: string
  status: string
  label: string
}

/** 分钟扫描股票池范围（T-54）：watchlist 自选（缺省）/ top_n 成交额前 N / codes 显式代码集 */
export type ScanUniverse = 'watchlist' | 'top_n' | 'codes'

/** 扫描触发附加参数（T-54，分钟周期生效）：universe 范围 + top_n 规模 + codes 显式代码集 */
export interface TriggerScanOptions {
  universe?: ScanUniverse
  topN?: number
  codes?: string[]
}

/** 触发信号扫描（POST /signals/scan）：全市场按周期主动计算；period 缺省为日线，可传 'daily'/'weekly'/'monthly'/'1'/... */
export async function triggerSignalScan(period?: string, opts: TriggerScanOptions = {}): Promise<SignalScanTrigger> {
  const { data } = await api.post('/signals/scan', {
    full_universe: true,
    ...(period ? { period } : {}),
    ...(opts.universe ? { universe: opts.universe } : {}),
    ...(opts.topN ? { top_n: opts.topN } : {}),
    ...(opts.codes?.length ? { codes: opts.codes } : {}),
  })
  return data
}

export interface SignalCatalogItem {
  code: string
  name: string
  category: string
  desc: string
  params: Record<string, number>
  rank?: number
}

export async function getSignalCatalog(): Promise<SignalCatalogItem[]> {
  const { data } = await api.get('/signals/catalog')
  return data.data
}

export interface WatchlistItem {
  code: string
  name: string
  /** 行情快照可选字段(GET /market/watchlist 已返回;兼容旧缓存/旧数据) */
  last_price?: number | string | null
  pct_change?: number | string | null
  industry?: string | null
  updated_at?: string | null
}

export async function getWatchlist(): Promise<WatchlistItem[]> {
  const { data } = await api.get('/market/watchlist')
  return data.data ?? []
}

// ===== 全球核心指数（T-73，GET /market/indices） =====

export type IndexMarket = 'CN' | 'US' | 'HK'

export interface IndexQuote {
  /** 内部规范代码（000001.SH / .DJI / HSI 等） */
  code: string
  name: string
  market: IndexMarket
  /** 最新点位；市场无数据时为 null（前端静默降级显示 —） */
  price: number | null
  pct_change: number | null
  currency: string
  /** 行情时刻（naive ISO；缺失为 null） */
  updated_at: string | null
}

export interface IndicesResponse {
  indices: IndexQuote[]
}

/** 全球核心指数快照（8 只：A 股 4 / 美股 3 / 港股 1）。全市场无数据时后端 503。 */
export async function getIndices(): Promise<IndexQuote[]> {
  const { data } = await api.get<IndicesResponse>('/market/indices')
  return data.indices ?? []
}

/**
 * 个股时间线信号事件（GET /signals/events/{code}/timeline）。
 * 后端只返回子集字段（不含 stock_code/stock_name），与 SignalEvent 严格区分。
 */
export interface TimelineEvent {
  id: number
  signals: string[]
  status: string
  evidence: Record<string, string | number | null>
  triggered_at: string | null
  /** 数据截至时刻（naive ISO，可空；语义同 SignalEvent.as_of，展示回退 triggered_at） */
  as_of: string | null
}

export async function getStockTimeline(code: string): Promise<TimelineEvent[]> {
  const { data } = await api.get(`/signals/events/${code}/timeline`)
  return data.data
}

/** 任务状态：新增 paused（协作式暂停，见 pauseJob/resumeJob）；cancelled（已取消，保留记录）。 */
export type JobStatus = 'pending' | 'running' | 'paused' | 'done' | 'failed' | 'cancelled'

// ===== 任务结果载荷（result 形状随 job_type 判别；字段对齐 backend/services/experiments/queue.py 各 _run_* runner） =====

/** gp_run 载荷：results=Top 因子（含样本外评估 oos），evolution=演化曲线，meta=面板/切分/后端信息 */
export interface GpEvolutionPoint {
  gen: number
  best_train_ic?: number | null
  avg_ic?: number | null
  best_expression?: string
  best_complexity?: number
}

export interface GpRunMeta {
  stocks?: number
  days?: number
  split?: { train?: number; val?: number; oos?: number }
  dates?: { train?: string; oos_end?: string }
  backend?: string
  config?: { features?: string[]; op_set?: string[]; op_config?: Record<string, unknown> }
}

export interface GpRunPayload {
  results?: FactorResult[]
  evolution?: GpEvolutionPoint[]
  meta?: GpRunMeta
}

/** evaluate 载荷：result=训练段完整评估（含 latex/oos_dates），oos=样本外评估，meta=面板信息 */
export interface EvaluatePayload {
  result?: EvalResult & { latex?: string; oos_dates?: string[] }
  oos?: EvalResult & { latex?: string; dates?: string[] }
  meta?: { stocks?: number; days?: number; split_date?: string | null }
}

/** T-58 DQN 评估结果（rl_train 任务 meta.eval）：净值曲线 + 回测指标，字段对齐后端 rl_train handler */
export interface DqnEvalMeta {
  /** 评估段净值曲线（初始 1.0 的权益序列，逐日/逐 episode） */
  equity?: number[]
  /** 总收益率（小数，如 0.123 = 12.3%） */
  total_return?: number
  /** 夏普比率 */
  sharpe?: number
  /** 最大回撤（小数，负值表示回撤幅度） */
  max_drawdown?: number
  /** 交易次数 */
  trades?: number
  /** 平均仓位（0~1） */
  avg_pos?: number
}

/** neural_train 载荷：results 恒空数组，指标在 meta（train/val IC、loss、epochs、model_kind、checkpoint）；
 *  T-58 DQN（rl_train 任务）复用本载荷形状：meta 为 {episodes, train_rewards, eval, model_kind="dqn", checkpoint, model_id?} */
export interface NeuralTrainMeta {
  train_loss?: number | null
  val_loss?: number | null
  train_ic?: number | null
  val_ic?: number | null
  epochs?: number
  model_kind?: string
  checkpoint?: unknown
  /** T-58 DQN（model_kind="dqn"）：训练 episode 总数（neural_train 无此字段） */
  episodes?: number
  /** T-58 DQN：每 episode 平均奖励数组（x=episode 序，训练奖励曲线） */
  train_rewards?: number[]
  /** T-58 DQN：评估段指标（净值曲线 + 回测指标；neural_train 无此字段） */
  eval?: DqnEvalMeta
  /** T-57 权重/注意力可视化：mlp=末层权重 / lstm=末层 LSTM 权重(Wx/Wh) / transformer=注意力(最后一层,均值)；无产出或计算失败为 null */
  viz?: { kind: string; matrix: number[][]; rowLabels?: string[]; colLabels?: string[] } | null
}

export interface NeuralTrainPayload {
  results?: unknown[]
  meta?: NeuralTrainMeta
}

/** backtest 载荷：backtest=多方式/分位回测结果，meta=面板信息与抽稀日期轴 */
export interface BacktestPayload {
  backtest?: BTModesResult
  meta?: { stocks?: number; days?: number; dates?: string[] }
}

/** alpha101_score 载荷：results=按库评分降序的逐因子评分，meta.scored=已评分因子数 */
export interface Alpha101ScoreItem {
  id: number
  name: string
  ic_mean: number | null
  stability: number | null
  score: number | null
}

export interface Alpha101ScorePayload {
  results?: Alpha101ScoreItem[]
  meta?: { stocks?: number; days?: number; scored?: number }
}

/** factor_tune 载荷：grid=按目标排序的网格点，best=最优网格点，best_expression=替换最优参数后的表达式 */
export interface TuneRunMeta {
  stocks?: number
  days?: number
  split?: { train?: number; oos?: number }
}

export interface FactorTunePayload {
  expression?: string
  param?: TuneParamRequest
  grid?: TuneGridItem[]
  best?: TuneGridItem | null
  best_expression?: string | null
  available_params?: TuneParamMeta[]
  target?: string
  horizon?: number
  meta?: TuneRunMeta
}

/** dataset_build 载荷：dataset=新建数据集摘要 */
export interface DatasetBuildInfo {
  id?: number
  name?: string
  universe?: string
  start_date?: string
  end_date?: string
  stock_count?: number
  row_count?: number
}

export interface DatasetBuildPayload {
  dataset?: DatasetBuildInfo
}

/** market_scan 载荷：scan=扫描摘要（scanned/events/source/elapsed） */
export interface MarketScanPayload {
  scan?: { scanned?: number; events?: number; source?: string; elapsed?: number }
}

/** paper_experiment 载荷：与 /paper/experiment 同步结果同形状（code/period/days + results 信号统计） */
export interface PaperJobPayload {
  code?: string
  period?: string
  days?: number
  signals?: string[]
  results?: PaperSignalResult[]
}

/** walk_forward 单窗汇总：anchored 滚动切分的每窗 OOS IC 指标 */
export interface WalkForwardWindow {
  window: number
  oos_start?: string | null
  oos_end?: string | null
  ic: number | null
  stability: number | null
  ic_positive_ratio: number | null
  n_days: number
}

/** walk_forward 结果：拼接 OOS IC 序列 + 分窗汇总 + 全 OOS 摘要 */
export interface WalkForwardResult {
  n_windows: number
  horizon: number
  windows: WalkForwardWindow[]
  oos_ic_series: number[]
  oos_dates: string[]
  summary: {
    mean_ic: number | null
    ic_std: number | null
    stability: number | null
    ic_positive_ratio: number | null
    n_days: number
  }
}

/** walk_forward 载荷：walk_forward=滚动验证结果，meta=面板信息 */
export interface WalkForwardPayload {
  walk_forward?: WalkForwardResult
  meta?: { stocks?: number; days?: number; n_windows?: number }
}

/** result 载荷判别联合：实际形状由 job_type 决定（详情接口 result 可为 null，兼容宽视图见 JobResultView） */
export type JobResultPayload =
  | GpRunPayload
  | EvaluatePayload
  | NeuralTrainPayload
  | BacktestPayload
  | Alpha101ScorePayload
  | FactorTunePayload
  | DatasetBuildPayload
  | MarketScanPayload
  | PaperJobPayload
  | WalkForwardPayload

/** 按 job_type 判别的完整任务结果：消费方收窄 job_type 后取精确 result 载荷（替代 as any 直读） */
export interface GpRunJobResult { job_type: 'gp_run'; result?: GpRunPayload }
export interface EvaluateJobResult { job_type: 'evaluate'; result?: EvaluatePayload }
export interface NeuralTrainJobResult { job_type: 'neural_train'; result?: NeuralTrainPayload }
export interface BacktestJobResult { job_type: 'backtest'; result?: BacktestPayload }
export interface Alpha101ScoreJobResult { job_type: 'alpha101_score'; result?: Alpha101ScorePayload }
export interface FactorTuneJobResult { job_type: 'factor_tune'; result?: FactorTunePayload }
export interface DatasetBuildJobResult { job_type: 'dataset_build'; result?: DatasetBuildPayload }
export interface MarketScanJobResult { job_type: 'market_scan'; result?: MarketScanPayload }
export interface PaperExperimentJobResult { job_type: 'paper_experiment'; result?: PaperJobPayload }
export interface WalkForwardJobResult { job_type: 'walk_forward'; result?: WalkForwardPayload }

export type JobResultByType =
  | GpRunJobResult
  | EvaluateJobResult
  | NeuralTrainJobResult
  | BacktestJobResult
  | Alpha101ScoreJobResult
  | FactorTuneJobResult
  | DatasetBuildJobResult
  | MarketScanJobResult
  | PaperExperimentJobResult
  | WalkForwardJobResult

export interface JobResult {
  id: number
  job_type: string
  status: JobStatus
  progress: number
  /** 阶段文案（进度语义化，如 "训练中 12/50"；非 running 任务通常为空） */
  phase?: string
  params: Record<string, unknown>
  /** result 载荷随 job_type 变化（判别联合见 JobResultByType） */
  result?: JobResultPayload
  error?: string
}

export interface FactorResult {
  expression: string
  train_ic: number | null
  val_ic: number | null
  complexity: number
  oos?: {
    ic?: number
    rank_ic?: number
    top_annual?: number
    long_short_annual?: number
    turnover?: number
    stability?: number
    error?: string
  }
}

/** 任务列表项（GET /experiments 与 /experiments/page 返回 _job_to_dict 快照）：
 *  无 result / finished_at，含列表摘要 summary 与创建参数快照 params。 */
export interface JobListItem {
  id: number
  job_type: string
  status: JobStatus
  progress: number
  phase?: string
  label?: string
  expr?: string
  dataset_id?: number | null
  summary?: Record<string, unknown> | null
  error?: string
  created_at: string
  params: Record<string, unknown>
}

/** JobDetail.result 兼容宽视图：所有 job_type 载荷字段并集（保留旧直读契约，避免各消费点逐一收窄；
 *  精确载荷按 job_type 见 JobResultByType/JobResultPayload）。 */
export interface JobResultView {
  results?: FactorResult[]
  meta?: Record<string, unknown>
  backtest?: BTModesResult
  result?: EvalResult
  oos?: Record<string, unknown>
  /** market_scan 任务结果快照（POST /signals/scan 触发；scan_once 返回）：
   *  source 取值：数据源 provider / 'busy'（扫描锁被占用，本轮未执行）/ 'empty' / 'no-watchlist' /
   *  'no-codes' / 'error'；T-76 前端据 'busy' 区分「触发成功但未真正计算」 */
  scan?: { scanned: number; events: number; source: string; elapsed: number }
}

/** 任务详情（GET /experiments/{id} 返回 get_job）：在列表字段基础上含完整 result 与 finished_at。
 *  注意详情接口不含 label/expr/dataset_id/summary（消费方以列表快照合并，见 TaskDetailDrawer）。 */
export interface JobDetail extends JobListItem {
  result?: JobResultView | null
  finished_at?: string | null
}

export async function getExperiments(jobType?: string, limit = 200): Promise<{ data: JobListItem[] }> {
  const { data } = await api.get('/experiments', { params: { job_type: jobType, limit } })
  return data
}

export interface ExperimentsPageParams {
  limit?: number
  offset?: number
  job_type?: string
  /** 单状态精确过滤（pending/running/paused/done/failed）；别名 active = pending+running */
  status?: string
  /** 在任务 id、表达式与序列化 params 中大小写不敏感模糊搜索 */
  keyword?: string
}

/** 分页实验任务列表（GET /experiments/page）：job_type/status/keyword 服务端筛选 + 分页，返回通用 PageResult */
export async function getExperimentsPage(params: ExperimentsPageParams = {}): Promise<PageResult<JobListItem>> {
  const { data } = await api.get('/experiments/page', { params })
  return data
}

export async function getJob(jobId: number): Promise<JobDetail> {
  const { data } = await api.get(`/experiments/${jobId}`)
  return data
}

/** 暂停/恢复操作结果（后端 POST /experiments/{id}/pause|resume）。 */
export interface JobControlResult {
  ok: boolean
  /** 操作后任务状态：暂停→paused；恢复→pending/running；失败时为当前实际状态 */
  status: JobStatus
  /** 协作式标记（成功时 true）：任务在下一个计算控制点停下，恢复后从断点继续 */
  cooperative?: boolean
  message?: string
  error?: string
}

/** 协作式暂停任务（仅 pending/running 可暂停；409 由 axios 抛错）。 */
export async function pauseJob(jobId: number): Promise<JobControlResult> {
  const { data } = await api.post(`/experiments/${jobId}/pause`)
  return data
}

/** 恢复协作式暂停的任务（仅 paused 可恢复；409 由 axios 抛错）。 */
export async function resumeJob(jobId: number): Promise<JobControlResult> {
  const { data } = await api.post(`/experiments/${jobId}/resume`)
  return data
}

export async function createExperiment(jobType: string, params: Record<string, unknown>): Promise<{ job_id: number }> {
  const { data } = await api.post('/experiments', { job_type: jobType, params })
  return data
}

/** 数据集列表项（GET /datasets；字段对齐后端 list_datasets 响应，与 Datasets 页 DatasetRow 同源） */
export interface DatasetInfo {
  id: number
  name: string
  universe: string
  start_date: string | null
  end_date: string | null
  stock_count: number
  row_count: number
  status: string
  created_at: string | null
}

export async function getDatasets(): Promise<DatasetInfo[]> {
  const { data } = await api.get('/datasets')
  return data.data
}

export async function createDataset(payload: Record<string, unknown>) {
  const { data } = await api.post('/datasets', payload)
  return data
}

/**
 * 等待既有后台任务完成（轮询现有 job_id，不发起新任务）：返回最终 job（详情结构）；onProgress 可选回调。
 * 后台节流（默认开启）：页面隐藏时暂停轮询且不累计等待时长（任务在后台继续执行，切走再回来不误报超时），
 * 恢复可见立即补查一次——FactorWorkbench 7 个常驻 tab 切走期间零请求。
 */
export async function waitForJob(
  jobId: number,
  opts?: {
    onProgress?: (j: JobDetail) => void
    intervalMs?: number
    /** 最长等待时间（ms），默认 40 分钟（后端 alpha101_score 上限 1800s，留有余量）；超时抛错 */
    maxWaitMs?: number
    /** 外部取消（如页面卸载）：每轮轮询检查，已中止则抛错 */
    signal?: AbortSignal
    /** 页面隐藏时暂停轮询（不计等待时长）、可见时恢复并补查；默认 true */
    backgroundThrottle?: boolean
  },
): Promise<JobDetail> {
  const intervalMs = opts?.intervalMs ?? 2000
  const maxWaitMs = opts?.maxWaitMs ?? 40 * 60 * 1000
  const throttle = opts?.backgroundThrottle ?? true
  const sig = opts?.signal
  // 页面隐藏态（Node/SSR 无 document → 恒可见，节流自动跳过）
  let visible = typeof document === 'undefined' || document.visibilityState !== 'hidden'
  /** 剩余等待预算：仅扣除可见期间的等待（隐藏期间暂停计时，避免切走长时间后误报超时） */
  let remaining = maxWaitMs
  const makeAbortError = () => {
    const err = new Error('任务轮询已取消')
    err.name = 'AbortError'
    return err
  }
  const aborted = () => {
    if (sig?.aborted) throw makeAbortError()
  }
  /** 等待至页面恢复可见（隐藏期间挂起、不发起请求）；signal 中止立即抛错 */
  const waitVisible = () => new Promise<void>((resolve, reject) => {
    if (visible) { resolve(); return }
    const onAbort = () => { cleanup(); reject(makeAbortError()) }
    const onVis = () => {
      if (document.visibilityState !== 'hidden') { cleanup(); resolve() }
    }
    const cleanup = () => {
      document.removeEventListener('visibilitychange', onVis)
      sig?.removeEventListener('abort', onAbort)
    }
    document.addEventListener('visibilitychange', onVis)
    sig?.addEventListener('abort', onAbort)
  })
  /** 全局可见性跟踪（单次 waitForJob 调用生命周期内有效）：退出（成功/失败/超时）必须在 finally 移除，
   *  防长会话累积成百上千个匿名监听器（P2-60） */
  const onVisibilityChange = () => {
    visible = document.visibilityState !== 'hidden'
  }
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibilityChange)
  }
  try {
    while (true) {
      aborted()
      if (throttle && !visible) {
        // 页面隐藏：暂停轮询，等恢复可见后立即补查
        await waitVisible()
        aborted()
        continue
      }
      if (remaining <= 0) {
        throw new Error(`任务等待超时（超过 ${Math.round(maxWaitMs / 60000)} 分钟）`)
      }
      const started = Date.now()
      await new Promise((r) => setTimeout(r, Math.min(intervalMs, remaining)))
      remaining -= Date.now() - started
      aborted()
      // 睡眠期间切到后台 → 本轮跳过 fetch，进入隐藏挂起分支
      if (throttle && !visible) continue
      const j = await getJob(jobId)
      opts?.onProgress?.(j)
      if (j.status === 'done' || j.status === 'failed') return j
    }
  } finally {
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }
}

// ===== 面板冷缓存重建任务化（C11b，配合后端 C11a 202 契约） =====
// 端点面板冷缓存 miss 时返回 HTTP 202 + {task_id, status:"queued"|..., message}（task_id=experiments job_id）；
// 热/冷缓存命中返回 200 原数据。前端收 202 → 轮询任务（1s/120s）→ 完成/失败收尾 → 重试原请求取 200 结果。

/** 面板冷缓存重建 202 受理响应（后端 _panel_or_202 契约）。 */
export interface PanelRebuildAccepted {
  task_id: number | string
  status: string
  message: string
}

/** 202 受理形状守卫：响应含 task_id 即视为重建受理（200 原数据不含该字段）。 */
export function isPanelRebuildAccepted(data: unknown): data is PanelRebuildAccepted {
  return typeof data === 'object' && data !== null && 'task_id' in data
}

/** 轮询进度回调形状（TaskProgress 直接消费：id/job_type/status/progress/phase）。 */
export interface PanelRebuildProgress {
  id: number
  job_type: string
  status: string
  progress?: number
  phase?: string
  [key: string]: unknown
}

/**
 * 面板冷缓存重建统一包装：请求返回 202（重建受理）→ 立即上报排队态 → 轮询
 * panel_build 任务（1s 间隔，可见等待上限 120s，页面隐藏自动暂停）→ 任务完成重试
 * 原请求取 200 热数据；failed/超时抛错（错误文案含任务号）。返回 200 原数据时原样透传。
 */
export async function callWithPanelRebuild<T>(
  request: () => Promise<T>,
  opts?: {
    /** 重建进度回调（排队→running→done；页面驱动 TaskProgress 等任务流 UI） */
    onProgress?: (job: PanelRebuildProgress) => void
    /** 外部取消（页面卸载等）：透传 waitForJob AbortSignal */
    signal?: AbortSignal
  },
): Promise<T> {
  const first = await request()
  if (!isPanelRebuildAccepted(first)) return first
  const taskId = Number(first.task_id)
  if (!Number.isFinite(taskId) || taskId <= 0) {
    throw new Error('面板缓存重建任务受理异常: task_id 非法')
  }
  opts?.onProgress?.({ id: taskId, job_type: 'panel_build', status: 'queued', progress: 0 })
  let job: JobDetail
  try {
    job = await waitForJob(taskId, {
      intervalMs: 1000,
      maxWaitMs: 120_000,
      signal: opts?.signal,
      onProgress: (j) => opts?.onProgress?.({
        id: j.id, job_type: j.job_type, status: j.status, progress: j.progress, phase: j.phase,
      }),
    })
  } catch (e: any) {
    if (e?.name === 'AbortError') throw e
    throw new Error(`面板缓存重建超时（120 秒内未完成），请稍后重试（任务 #${taskId}）`)
  }
  if (job.status === 'failed') {
    throw new Error(`面板缓存重建失败: ${job.error ?? '任务失败'}（任务 #${taskId}）`)
  }
  // 任务已完成：面板已落冷/热缓存，重试原请求即返回 200 热数据；仍 202 属异常，抛错提示
  const retried = await request()
  if (isPanelRebuildAccepted(retried)) {
    throw new Error('面板缓存重建完成但仍未就绪，请稍后重试')
  }
  return retried
}

/** 提交后台任务并轮询至完成：返回最终 job（详情结构）；onProgress 可选回调。 */
export async function runTask(
  jobType: string,
  params: Record<string, unknown>,
  opts?: {
    onProgress?: (j: JobDetail) => void
    intervalMs?: number
    /** 最长等待时间（ms），默认 40 分钟；超时抛错（原样转发 waitForJob） */
    maxWaitMs?: number
    /** 外部取消（如页面卸载）：每轮轮询检查，已中止则抛错 */
    signal?: AbortSignal
  },
): Promise<JobDetail> {
  const { job_id } = await createExperiment(jobType, params)
  return waitForJob(job_id, opts)
}

export async function getUniverses() {
  const { data } = await api.get('/datasets/universes')
  return data.data
}

export async function getOperators() {
  const { data } = await api.get('/alpha/operators')
  return data
}

export async function getBackend() {
  const { data } = await api.get('/alpha/backend')
  return data
}

export async function toLatex(expr: string): Promise<{ expression: string; latex: string }> {
  const { data } = await api.get('/alpha/latex', { params: { expr } })
  return data
}

export async function toLatexBatch(expressions: string[]): Promise<{ expression: string; latex: string }[]> {
  const { data } = await api.post('/alpha/latex', { expressions })
  return data.data
}

export interface EvalResult {
  expression: string
  complexity: number
  ic: number | null
  rank_ic: number | null
  top_annual: number | null
  long_short_annual: number | null
  turnover: number | null
  stability: number | null
  ic_positive_ratio: number | null
  ic_series?: (number | null)[]
  quantiles?: Record<string, (number | null)[]>
  dates?: string[]
  n_quantiles?: number
}

export interface FactorInfo {
  id: number
  name: string
  expression: string
  description: string
  status: string
  dataset_id: number
  versions: FactorVersion[]
  /** 因子类型：'expr'=传统表达式因子 / 'nn'=神经网络因子（列表接口已带出，收编自页面局部断言） */
  kind?: string
}

export interface FactorVersion {
  id: number
  factor_id: number
  version: number
  expression: string
  complexity: number
  train_ic: number | null
  val_ic: number | null
  oos_ic: number | null
  stability: number | null
  turnover: number | null
  oos_verified: boolean
  stability_checked: boolean
  complexity_checked: boolean
  version_released: boolean
  human_approved: boolean
  status: string
  approved_at: string | null
  note: string
  latex?: string
}

export async function getFactors(): Promise<FactorInfo[]> {
  const { data } = await api.get('/factors')
  return data.data
}

/** 因子版本指标（createFactor/createVersion 的 metrics 字段；键名对齐后端 create_factor 写入列） */
export interface FactorMetrics {
  train_ic?: number | null
  train_rank_ic?: number | null
  val_ic?: number | null
  val_rank_ic?: number | null
  oos_ic?: number | null
  oos_rank_ic?: number | null
  return_annual?: number | null
  turnover?: number | null
  stability?: number | null
  complexity?: number | null
}

/** POST /factors 创建载荷：expr 因子走 expression，nn 因子走 model_id；创建后停在 draft */
export interface CreateFactorPayload {
  name: string
  /** 因子类型：'expr'（缺省）/ 'nn' */
  kind?: string
  expression?: string
  description?: string
  dataset_id?: number
  /** kind='nn' 必填：nn_models.id */
  model_id?: number
  metrics?: FactorMetrics
}

/** POST /factors 创建结果（后端 create_factor 返回 {factor: factor_dict, version: version_dict}；
 *  factor 形状为因子头部（无 versions 嵌套），version 为 v1 草稿版本 */
export interface CreateFactorResult {
  factor: {
    id: number
    name: string
    expression: string
    kind: string
    latex?: string
    description: string
    status: string
    dataset_id: number
    created_at: string | null
  }
  version: FactorVersion
}

export async function createFactor(payload: CreateFactorPayload): Promise<CreateFactorResult> {
  const { data } = await api.post('/factors', payload)
  return data
}

export async function advanceVersion(versionId: number, step: string): Promise<FactorVersion> {
  const { data } = await api.post(`/factors/versions/${versionId}/advance`, { step })
  return data
}

export async function getPublishSteps() {
  const { data } = await api.get('/alpha/publish-steps')
  return data.data
}

/** LLM Copilot 配置（GET /copilot/config；只读展示 + 连通性测试）。 */
export interface CopilotConfig {
  base_url: string
  model: string
  configured: boolean
}

export async function getCopilotConfig(): Promise<CopilotConfig> {
  const { data } = await api.get('/copilot/config')
  return data
}

// ===== 运行时配置（T-77，GET/PUT /system/config/runtime） =====

/** 运行时配置项类型：int/float 走数字输入，str 走文本输入，json 走 JSON 文本域 */
export type RuntimeConfigType = 'int' | 'float' | 'str' | 'json'

/** 生效语义：immediate = 立即生效；restart = 修改后需重启后端生效 */
export type RuntimeConfigApply = 'immediate' | 'restart'

/** 运行时配置项（GET /system/config/runtime 返回；value 当前生效值，default 为 env 基线） */
export interface RuntimeConfigItem {
  key: string
  label: string
  type: RuntimeConfigType
  value: unknown
  default: unknown
  description: string
  apply: RuntimeConfigApply
}

/** 拉取运行时配置清单（6 项：扫描调度/扫描上限/行情缓存 TTL/任务并发/LLM 超时/LLM 模型）。 */
export async function getRuntimeConfig(): Promise<RuntimeConfigItem[]> {
  const { data } = await api.get('/system/config/runtime')
  return data.data
}

/** 更新运行时配置（PUT /system/config/runtime）：非法 key/类型/范围 400 由 axios 抛错
 *  （FastAPI detail 在 error.response.data.detail）。返回保存后的生效值与生效语义。 */
export async function setRuntimeConfig(
  key: string,
  value: unknown,
): Promise<{ ok: boolean; key: string; value: unknown; default: unknown; apply: RuntimeConfigApply }> {
  const { data } = await api.put('/system/config/runtime', { key, value })
  return data
}

// ===== 信号中心操作命令（copilot 流式 action 事件 → 前端执行） =====

/** LLM 工具产出的信号中心筛选（wire 契约，与 /signals/events/page 筛选同语义）。
 *  后端始终输出全部字段（未提及的用默认值），字段为完整值而非增量；
 *  数组字段与 page 接口的逗号分隔字符串互转由消费方完成。 */
export interface SignalCenterFilters {
  /** 周期（1/5/15/30/60/daily/weekly/monthly；缺省 daily） */
  period: string
  /** 信号触发时间范围（today/3d/7d/30d/all；缺省近3日） */
  time_range: SignalTimeRange
  /** 排除当日（缺省 false） */
  exclude_today: boolean
  /** 板块（中文名，如 沪主板/创业板；缺省空数组） */
  sectors: string[]
  /** 信号类型（OR 语义；缺省空数组） */
  signal_types: string[]
  /** 信号匹配语义：any=命中任一即可，all=全部命中才保留；缺省 any */
  signal_match: 'any' | 'all'
  /** 仅看关注（缺省 false） */
  watchlist_only: boolean
}

/** LLM 工具产出的信号中心操作命令（/copilot/{market|research}/chat 流式 action 事件契约）。
 *  command/mode 均为字面量：command 必须是 signal_center.apply_filters、mode 固定 replace。
 *  不含任何导航字段：前端固定只导航 /signals，模型不允许指定路径。 */
export interface SignalFilterCommand {
  command: 'signal_center.apply_filters'
  mode: 'replace'
  filters: SignalCenterFilters
}

/** LLM 工具产出的个股跳转命令（/copilot/market/chat 流式 action 事件契约）。
 *  code 服务端已严格校验格式（6 位数字，可选 .SH/.SZ/.BJ 后缀，防路径/脚本注入）；
 *  前端仅按固定路径导航 /stocks/{code}，模型不允许返回任意路径。 */
export interface StockNavigateCommand {
  command: 'stock.navigate'
  code: string
}

/** 全部可执行动作联合：前端动作执行器按 command 分派（映射表见 ChatPanel.ACTION_HANDLERS）。 */
export type CopilotAction = SignalFilterCommand | StockNavigateCommand

/** 多轮历史消息（user/assistant 交替；content 为纯文本，不含上下文注入） */
export interface CopilotHistoryMsg {
  role: 'user' | 'assistant'
  content: string
}

export async function streamCopilot(
  endpoint: 'market' | 'research',
  payload: { question: string; context: Record<string, unknown>; history?: CopilotHistoryMsg[] },
  onChunk: (text: string) => void,
  signal?: AbortSignal,
  onAction?: (action: CopilotAction) => void,
): Promise<void> {
  const resp = await fetch(`/api/v1/copilot/${endpoint}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  })
  if (!resp.ok || !resp.body) {
    onChunk(`[请求失败 ${resp.status}]`)
    return
  }
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const lines = buf.split('\n\n')
    buf = lines.pop() ?? ''
    for (const line of lines) {
      if (!line.startsWith('data:')) continue
      const payloadStr = line.slice(5).trim()
      if (payloadStr === '[DONE]') return
      try {
        const chunk = JSON.parse(payloadStr)
        // action 事件：LLM 在流内直接产出动作命令（筛选/跳转，后端已严格校验），前端据此执行
        if (chunk.action) onAction?.(chunk.action as CopilotAction)
        else if (chunk.content) onChunk(chunk.content)
      } catch { /* ignore */ }
    }
  }
}

export async function getFundamental(code: string) {
  const { data } = await api.get(`/stocks/${code}/fundamental`)
  return data
}

export async function getNews(code: string, limit = 10) {
  const { data } = await api.get(`/stocks/${code}/news`, { params: { limit } })
  return data.data
}

export async function getFinancials(code: string) {
  const { data } = await api.get(`/stocks/${code}/financials`)
  return data
}

// ===== 财务历史库（T-06：三表按报告期 + 每日估值序列） =====

/** 财务历史类型（GET /stocks/{code}/financials/history type 参数） */
export type FinStatementType = 'balance' | 'income' | 'cash' | 'valuation'

/** 财务历史单行：三表为 report_date + 业务字段（金额元 / 率字段为百分数值）；估值为 date + pe/pb/ps/total_mv/float_mv（市值亿） */
export interface FinancialHistoryRow {
  report_date?: string
  date?: string
  [k: string]: unknown
}

/** GET /stocks/{code}/financials/history 响应：data 恒为倒序（新在前） */
export interface FinancialHistoryResponse {
  code: string
  type: string
  data: FinancialHistoryRow[]
}

/** 财务三表历史（按报告期倒序）或每日估值序列（日期倒序）；后端 SQLite 缓存，网络失败降级返回库内数据 */
export async function getFinancialHistory(code: string, type: FinStatementType, limit = 40): Promise<FinancialHistoryResponse> {
  const { data } = await api.get(`/stocks/${code}/financials/history`, { params: { type, limit } })
  return data
}

/** 三表最近一期合并摘要（GET /stocks/{code}/financials/latest）；各表无数据为 null */
export interface FinancialLatest {
  code: string
  /** 最新披露期（三表 max report_date）；全无数据为 null */
  period?: string | null
  balance?: FinancialHistoryRow | null
  income?: FinancialHistoryRow | null
  cash?: FinancialHistoryRow | null
}

export async function getFinancialsLatest(code: string): Promise<FinancialLatest> {
  const { data } = await api.get(`/stocks/${code}/financials/latest`)
  return data
}

// ===== 资金类历史库（T-08：个股资金流 / 龙虎榜 / 两融 / 北向历史持股） =====

export async function getRisk(code: string) {
  const { data } = await api.get(`/indicators/${code}/risk`)
  return data
}

// ===== 指标参数契约（/indicators/params 与 /indicators/catalog） =====

/** 单个可调参数定义（type: "int" | "float"）。 */
export interface IndicatorParamSpec {
  key: string
  label: string
  type: 'int' | 'float'
  default: number
  min: number
  max: number
  step: number
}

/** 具名多参数指标契约：params 参数定义 + defaults + candidates 完整候选网格。 */
export interface IndicatorSpec {
  key: string
  name: string
  params: IndicatorParamSpec[]
  defaults: Record<string, number>
  candidates: Record<string, number>[]
}

/** 具名参数值对象，如 MACD={fast:12, slow:26, signal:9}。 */
export type IndicatorParamValues = Record<string, number>

/** 已保存的指标参数文档（ind:v2:{indicator} JSON）。 */
export interface IndicatorStoredParams {
  values: IndicatorParamValues
  source: 'manual' | 'global' | 'default'
  target: string
  horizon: number
}

/** GET /indicators/params：specs 白名单 + 全局（v2 JSON）+ 生效参数（v2 > legacy > 默认）。 */
export interface IndicatorParamsResponse {
  specs: IndicatorSpec[]
  targets: Record<string, string>
  defaults: Record<string, IndicatorParamValues>
  /** v2 全局参数：{indicator: {values, source, target, horizon}} */
  global: Record<string, IndicatorStoredParams>
  /** legacy 全局参数：{indicator: "6" | "6,13,5" | "20,2.0"}（字符串形式） */
  legacy: Record<string, string>
  /** 生效具名参数（v2 global > legacy global > spec 默认） */
  effective: Record<string, IndicatorParamValues>
}

/** 保存指标参数（PUT /indicators/params/{indicator}）payload。 */
export interface SaveIndicatorParamsPayload {
  values: IndicatorParamValues
  source?: 'manual' | 'global' | 'default'
  target?: string
  horizon?: number
}

/** GET /indicators/params：读取全部指标参数契约与生效参数。 */
export async function getIndicatorParams(): Promise<IndicatorParamsResponse> {
  const { data } = await api.get('/indicators/params')
  return data
}

/** PUT /indicators/params/{indicator}：保存指标参数为全局默认（严格校验，400 由 axios 抛错）。 */
export async function saveIndicatorParams(
  indicator: string,
  payload: SaveIndicatorParamsPayload,
): Promise<{ ok: boolean; indicator: string; params: IndicatorParamValues; stored: IndicatorStoredParams }> {
  const { data } = await api.put(`/indicators/params/${indicator}`, payload)
  return data
}

/** DELETE /indicators/params/{indicator}：删除全局参数（恢复系统默认）。 */
export async function resetIndicatorParams(
  indicator: string,
): Promise<{ ok: boolean; indicator: string; deleted: number; defaults: IndicatorParamValues }> {
  const { data } = await api.delete(`/indicators/params/${indicator}`)
  return data
}

/** GET /indicators/catalog：指标目录（主图叠加 / 副图指标 + 具名多参数 specs + 可调优列表）。 */
export interface IndicatorCatalogResponse {
  main_overlays: { key: string; name: string }[]
  sub_indicators: { key: string; name: string }[]
  tuneable: string[]
  specs: IndicatorSpec[]
  targets: Record<string, string>
}

export async function getIndicatorCatalog(): Promise<IndicatorCatalogResponse> {
  const { data } = await api.get('/indicators/catalog')
  return data
}

/** 单条调优候选：param 为 legacy 位置参数字符串（"6" / "6,13,5" / "20,2.0"），params 为具名对象（新契约）。 */
export interface TuneCandidate {
  /** legacy param 字符串（与旧位置数组形式一致，可直存 ind:{indicator}:{target} 全局参数） */
  param: string
  /** 具名参数对象 */
  params: IndicatorParamValues
  ic: number | null
  icir: number | null
  rank_ic: number | null
  stability: number | null
  win_rate: number | null
  ls_annual: number | null
  sharpe: number | null
  max_drawdown: number | null
  composite: number | null
  score: number | null
  last_value: number | null
}

/** POST /indicators/{code}/tune 响应：含 legacy param 与具名 params 双形态，附 spec/defaults/current/global 契约字段。 */
export interface TuneResponse {
  indicator: string
  name: string
  target: string
  horizon: number
  results: TuneCandidate[]
  best: TuneCandidate | null
  spec: IndicatorSpec
  defaults: IndicatorParamValues
  /** 当前生效参数（v2 global > legacy global > 默认） */
  current?: IndicatorParamValues | null
  /** 已保存的 v2 全局参数文档（未保存为 null） */
  global?: IndicatorStoredParams | null
  /** 优化算法（grid/fast/random；旧缓存响应可能缺失） */
  algorithm?: string
  /** 采样评估的候选组数 / 全量网格候选总数（覆盖度标注） */
  evaluated?: number
  candidates_total?: number
  targets: Record<string, string>
  global_saved: boolean
}

/** POST /indicators/{code}/tune(与 /tune/combined) 提交响应(P1-38b)：参数校验同步(400/404)，计算入后台任务队列，返回任务 id；
 *  结果经 GET /experiments/{job_id} 轮询（job.result 即原同步契约的 TuneResponse / CombinedTuneResponse 结构）。 */
export interface TuneJobSubmit {
  job_id: number
  status: string
}

export async function tuneIndicator(code: string, indicator: string, horizon = 5, target = 'ic', algorithm: 'grid' | 'random' | 'fast' = 'grid'): Promise<TuneJobSubmit> {
  const { data } = await api.post(`/indicators/${code}/tune`, { indicator, horizon, target, algorithm })
  return data
}

/** 整体调优组合候选：params_by_indicator 为各指标具名参数组合。 */
export interface CombinedTuneCandidate {
  /** 组合参数：{indicator: {key: value}} */
  params_by_indicator: Record<string, IndicatorParamValues>
  ic: number | null
  icir: number | null
  rank_ic: number | null
  stability: number | null
  win_rate: number | null
  ls_annual: number | null
  sharpe: number | null
  max_drawdown: number | null
  composite: number | null
  score: number | null
  last_value: number | null
}

/** POST /indicators/{code}/tune/combined 响应：整体调优（多指标参数组合联合评分）。 */
export interface CombinedTuneResponse {
  combined: true
  indicators: string[]
  target: string
  horizon: number
  algorithm: string
  results: CombinedTuneCandidate[]
  best: CombinedTuneCandidate | null
  /** 各指标名称与默认参数（展示用） */
  specs: Record<string, { name: string; defaults: IndicatorParamValues }>
  /** 各指标单独最优的 IC/score（组合增益对比用；缺失=未计算） */
  singles?: Record<string, { best_ic: number | null; best_score: number | null }> | null
  evaluated: number
  candidates_total: number
  targets: Record<string, string>
  global_saved: boolean
  /** 当前生效参数汇总 */
  current?: Record<string, IndicatorParamValues> | null
  global?: Record<string, IndicatorStoredParams> | null
}

/** 整体调优：多指标参数作为组合联合优化（≤3 个指标，笛卡尔积联合评分）。异步任务（P1-38b）：返回任务 id，结果经 GET /experiments/{job_id} 轮询。 */
export async function tuneIndicatorsCombined(
  code: string,
  indicators: string[],
  horizon = 5,
  target = 'ic',
  algorithm: 'grid' | 'random' | 'fast' = 'grid',
): Promise<TuneJobSubmit> {
  const { data } = await api.post(`/indicators/${code}/tune/combined`, { indicators, horizon, target, algorithm })
  return data
}

/** 取消指标调优任务：DELETE /experiments/{job_id}（后端 delete_job：运行中/待执行标 cancelled 保留记录并中断计算，已终态直接删行）。 */
export async function cancelTuneJob(jobId: number): Promise<{ ok: boolean; deleted?: boolean }> {
  const { data } = await api.delete(`/experiments/${jobId}`)
  return data
}

export async function getAlpha101(category?: string) {
  const { data } = await api.get('/alpha/alpha101', { params: { category } })
  return data
}

export async function evaluateAlpha101(alphaId: number, datasetId: number, horizon = 5, onProgress?: (job: PanelRebuildProgress) => void) {
  // 面板冷缓存 miss → 202 受理：自动轮询 panel_build 任务后重试本端点（C11b）
  return callWithPanelRebuild(
    () => api.post('/alpha/alpha101/evaluate', { alpha_id: alphaId, dataset_id: datasetId, horizon }).then((r) => r.data),
    { onProgress },
  )
}

export async function scoreAlpha101(datasetId: number, limit?: number) {
  // 全库评分改为后台任务（alpha101_score），逐因子进度计入任务管理器
  const { data } = await api.post('/alpha/alpha101/score', { dataset_id: datasetId, limit })
  return data as { job_id: number; status: string }
}

// ===== 因子参数调优（/alpha/tune 网格搜索） =====

export type TuneTarget = 'ic' | 'ic_abs' | 'icir' | 'rank_ic' | 'ls_annual' | 'sharpe' | 'max_drawdown' | 'win_rate' | 'turnover' | 'stability' | 'composite'

export interface TuneParamMeta {
  name: string
  op: string
  key: string
  index: number
  default: number | null
  is_int: boolean
  count: number
}

export interface TuneParamRequest {
  name: string
  min: number
  max: number
  step: number
  is_int?: boolean
}

export interface TuneGridItem {
  param_value: number
  train_ic: number | null
  rank_ic: number | null
  icir: number | null
  ls_annual: number | null
  stability: number | null
  oos_ic: number | null
  turnover?: number | null
  ls_sharpe?: number | null
  max_drawdown?: number | null
  win_rate?: number | null
  error?: string
}

export interface TuneResult {
  expression: string
  probe?: boolean
  param?: TuneParamRequest
  grid?: TuneGridItem[]
  best?: TuneGridItem | null
  best_expression?: string | null
  available_params: string[]
  param_meta?: TuneParamMeta[]
  target: string
  horizon: number
  meta?: TuneRunMeta
}

export async function tuneFactorExpression(payload: {
  expression: string
  dataset_id: number
  horizon?: number
  target?: TuneTarget
  param?: TuneParamRequest
  probe?: boolean
}): Promise<TuneResult | { job_id: number; status: string; available_params: string[] }> {
  const { data } = await api.post('/alpha/tune', payload)
  return data
}

/** 全局调优参数（键值对，key 如 ts_mean.window[0]，value 为字符串数字）。 */
export async function getTuneGlobal(): Promise<Record<string, string>> {
  const { data } = await api.get('/alpha/tune/global')
  return data.data ?? {}
}

/** 保存/更新全局调优参数：PUT /alpha/tune/global {params: {key: value}}。 */
export async function saveTuneGlobal(params: Record<string, string | number>): Promise<{ ok: boolean; saved: string[] }> {
  const { data } = await api.put('/alpha/tune/global', { params })
  return data
}

// ===== 回测（backtest）契约 =====

export type BTModeKey = 'long_short' | 'long' | 'short' | 'quantile'

export interface BTMetrics {
  annual_return: number | null
  volatility: number | null
  sharpe: number | null
  max_drawdown: number | null
  win_rate: number | null
  trading_days: number | null
}

export interface BTRisk {
  var95?: number
  cvar95?: number
  annual_volatility?: number
  max_drawdown?: number
  sharpe?: number
  win_rate?: number
}

export interface BTMode {
  mode: BTModeKey
  combo_nav: (number | null)[]
  metrics: BTMetrics
  risk?: BTRisk
}

export interface BTParams {
  top_pct?: number
  bottom_pct?: number
  cost_rate: number
  trade_interval: number
  mode?: string
  direction?: string
  horizon?: number
  modes?: string[]
  n_q?: number
  /** T-02 交易成本(基点):滑点/佣金/印花税;未传(历史任务)为 null,前端 ?? 0 */
  slippage_bps?: number | null
  commission_bps?: number | null
  stamp_tax_bps?: number | null
}

export interface BTQuantile {
  q: number
  combo_nav: (number | null)[]
  metrics: BTMetrics
}

export interface BTModesResult {
  modes: BTMode[]
  quantiles?: BTQuantile[]
  bench_nav: (number | null)[]
  bench_metrics: BTMetrics
  params: BTParams
  dates?: string[]
}

export interface BTMeta {
  stocks: number
  days: number
  dates: string[]
}

export interface BTJobResult {
  backtest: BTModesResult
  meta: BTMeta
}

export interface BacktestSummary {
  annual_return?: number | null
  sharpe?: number | null
  max_drawdown?: number | null
  win_rate?: number | null
  mode?: string
  error?: string
}

export const BT_MODE_LABEL: Record<string, string> = {
  long_short: '多空对冲',
  long: '仅多头',
  short: '仅空头',
  quantile: '分层(5分位)',
}

export const BT_MODE_OPTIONS: { label: string; value: BTModeKey }[] = [
  { label: '多空对冲', value: 'long_short' },
  { label: '仅多头', value: 'long' },
  { label: '仅空头', value: 'short' },
  { label: '分层(5分位)', value: 'quantile' },
]

// ===== 模拟盘（paper trading）契约 =====

/** 单次信号触发明细（实验回测结果）：日期 + 触发日收盘 + 后续 5/10 日收益（小数，如 0.021=2.1%）。 */
export interface PaperTriggerDetail {
  date: string
  close: number | null
  r5: number | null
  r10: number | null
}

/** 单个信号的回测统计卡数据（/paper/experiment results[]）。 */
export interface PaperSignalResult {
  signal: string
  name?: string
  /** 触发次数 */
  triggers: number
  /** 胜率：0~1 小数；后端若返回百分比原值（>1）由展示层自适应 */
  win_rate?: number | null
  /** 平均 5 日收益（小数，如 0.021） */
  avg_r5?: number | null
  /** 平均 10 日收益（小数，如 0.035） */
  avg_r10?: number | null
  /** 最近触发日期（YYYY-MM-DD） */
  last_trigger?: string | null
  /** 触发明细（按日期升序/降序均可，展示层统一倒序） */
  details?: PaperTriggerDetail[]
}

/** POST /paper/experiment 请求体。 */
export interface PaperExperimentPayload {
  code: string
  period: string
  signals: string[]
  days: number
}

/** POST /paper/experiment 响应。 */
export interface PaperExperimentResult {
  code: string
  period: string
  days: number
  signals?: string[]
  results: PaperSignalResult[]
}

/** 运行指标信号实验（单股多信号回测统计）。 */
export async function runPaperExperiment(payload: PaperExperimentPayload): Promise<PaperExperimentResult> {
  const { data } = await api.post('/paper/experiment', payload)
  return data
}

/** GET /paper/meta 响应：信号白名单（单一事实源派生）+ 周期白名单 + 上限。 */
export interface PaperMetaSignal {
  key: string
  label: string
  description: string
}

export interface PaperMeta {
  signals: PaperMetaSignal[]
  periods: string[]
  max_signals: number
  max_stocks: number
  min_bars: number
}

/** 拉取模拟盘元信息（信号白名单/周期/上限）；加载失败由页面降级为内置列表。 */
export async function getPaperMeta(): Promise<PaperMeta> {
  const { data } = await api.get('/paper/meta')
  return data
}

/** 实时指标快照字段：RSI6/12/24、KDJ K/D/J、MACD DIF/DEA/HIST、MA5/10/20/60、BOLL 上/中/下、量比、现价。
 *  与后端 /indicators/{code} 的 latest 键命名保持一致，展示层按存在性渲染。 */
export interface PaperWatchSnapshot {
  /** 现价（最新 bar 收盘价；后端 /paper/watch 实际产出键为 close，price 为兼容旧契约的别名，可能缺失） */
  price?: number | null
  /** 最新 bar 收盘价（后端 /paper/watch 快照实际键，与 watch 落库 prices[code] 同源；T-74 对齐修复） */
  close?: number | null
  rsi6?: number | null
  rsi12?: number | null
  rsi24?: number | null
  kdj_k?: number | null
  kdj_d?: number | null
  kdj_j?: number | null
  macd_dif?: number | null
  macd_dea?: number | null
  macd_hist?: number | null
  ma5?: number | null
  ma10?: number | null
  ma20?: number | null
  ma60?: number | null
  boll_upper?: number | null
  boll_mid?: number | null
  boll_lower?: number | null
  volume_ratio?: number | null
  [k: string]: unknown
}

/** 最近触发点（实时监控）：date 为触发交易日（可带 time）。 */
export interface PaperWatchTrigger {
  date?: string
  time?: string
  signal: string
}

/** GET /paper/watch 响应。 */
export interface PaperWatchResult {
  code: string
  name?: string
  period: string
  /** 快照时间（后端时点，如 2026-08-11 10:30:00）；缺省用前端接收时间 */
  time?: string
  /** 结果更新时间（naive ISO，来自项目/快照记录） */
  updated_at?: string
  snapshot?: PaperWatchSnapshot
  /** 当前命中信号（code 列表，与 catalog code 对齐） */
  hit_signals?: string[]
  /** 最近触发点列表（新→旧） */
  recent_triggers?: PaperWatchTrigger[]
}

/** 拉取实时指标监控快照（15s 轮询由页面层控制；后端失败由展示层静默处理）。 */
export async function getPaperWatch(params: {
  code: string
  period: string
  signals: string[]
}): Promise<PaperWatchResult> {
  const { data } = await api.get('/paper/watch', {
    params: { code: params.code, period: params.period, signals: params.signals.join(',') },
  })
  return data
}

// ===== 模拟盘多项目（paper projects）契约 =====
// 页面改走项目 API：项目 = 参数持久化 + 最近一次运行结果；kind 决定展示实验/监控。

/** 项目股票项：{code, name}（名称取后端关联表快照；旧单股项目恒为单元素列表）。 */
export interface PaperProjectStock {
  code: string
  name: string
}

/** 多股观察结果外壳（watch 项目 result / 轮询端点 stocks 元素共用）。 */
export interface PaperWatchStock {
  code: string
  name: string
  period: string
  snapshot?: PaperWatchSnapshot
  hit_signals?: string[]
  recent_triggers?: PaperWatchTrigger[]
  updated_at?: string
}

/** 多股 watch 项目的结果/轮询响应：{multi: true, stocks: [...]}。 */
export interface PaperMultiWatchResult {
  multi: true
  stocks: PaperWatchStock[]
}

/** 多股实验项目 run 结果：{multi, period, days, stocks 明细, summary 跨股汇总}。 */
export interface PaperMultiExperimentResult {
  multi: true
  period: string
  days: number
  stocks: {
    code: string
    name?: string
    results: PaperSignalResult[]
    computed_bars: number
  }[]
  summary: {
    stock_count: number
    total_triggers: number
    avg_win_rate: number | null
  }
}

/** 观察项目历史快照点（GET /projects/{pid}/watch/history，时间升序）。 */
export interface PaperWatchHistoryPoint {
  ts: string | null
  bar_date: string | null
  prices: Record<string, number | null>
  hit_signals: string[]
}

/** 模拟盘项目：单股或多股 + 周期 + 信号集 + 最近一次运行结果。 */
export interface PaperProject {
  id: number
  name: string
  kind: 'experiment' | 'watch'
  /** 兼容字段：恒为首股代码；多股项目请用 stocks */
  code: string
  /** 项目股票列表（≥1；多股项目为完整列表） */
  stocks: PaperProjectStock[]
  period: string
  signals: string[]
  days: number
  result: PaperExperimentResult | PaperMultiExperimentResult | PaperWatchResult | PaperMultiWatchResult | null
  /** 列表契约瘦身：列表项 result 恒为 null，has_result 标记是否已运行过；详情/run 返回完整 result */
  has_result?: boolean
  created_at: string
  updated_at: string
}

/** POST /paper/projects 请求体（PATCH 时除 code/signals 外全字段可选）。
 *  stocks 可选（≤20）：提供时多股批量创建/更新，code 以首股为准（兼容字段）。 */
export interface PaperProjectPayload {
  name?: string
  kind?: 'experiment' | 'watch'
  code: string
  stocks?: string[]
  period?: string
  signals: string[]
  days?: number
}

/** PATCH /paper/projects/{id} 响应：额外携带 result_invalidated（参数变更使旧结果失效）。 */
export interface PaperProjectUpdate extends PaperProject {
  result_invalidated: boolean
}

/** 获取全部模拟盘项目（后端按 updated_at desc 返回；外层 {data:[...]} 包装需二次解包）。 */
export async function getPaperProjects(): Promise<PaperProject[]> {
  const { data } = await api.get('/paper/projects')
  return data.data
}

/** 获取单个模拟盘项目（完整字段，含最近一次 result 全量）；404 由请求层抛出。 */
export async function getPaperProject(id: number): Promise<PaperProject> {
  const { data } = await api.get(`/paper/projects/${id}`)
  return data
}

/**
 * 拉取观察项目实时快照（GET /paper/projects/{id}/watch，只读不落库）。
 * 单股返回与 PaperWatchResult 兼容结构；多股返回 {multi: true, stocks: [...]}；
 * kind='experiment' 时后端返回 400。
 */
export async function getPaperProjectWatch(id: number): Promise<PaperWatchResult | PaperMultiWatchResult> {
  const { data } = await api.get(`/paper/projects/${id}/watch`)
  return data
}

/** 拉取观察项目历史快照序列（GET /paper/projects/{id}/watch/history，时间升序，最近 limit 条，默认 60 上限 200）。 */
export async function getPaperWatchHistory(id: number, limit = 60): Promise<{ data: PaperWatchHistoryPoint[] }> {
  const { data } = await api.get(`/paper/projects/${id}/watch/history`, { params: { limit } })
  return data
}

/** 创建模拟盘项目。 */
export async function createPaperProject(payload: PaperProjectPayload): Promise<PaperProject> {
  const { data } = await api.post('/paper/projects', payload)
  return data
}

/** 更新模拟盘项目（仅传需要修改的字段，name/code/stocks/period/signals/days 任意组合）。
 *  code/stocks/period/signals/days 任一变更都会使旧 result 失效，响应携带 result_invalidated。 */
export async function updatePaperProject(id: number, payload: Partial<PaperProjectPayload>): Promise<PaperProjectUpdate> {
  const { data } = await api.patch(`/paper/projects/${id}`, payload)
  return data
}

/** 删除模拟盘项目（204 无响应体）。 */
export async function deletePaperProject(id: number): Promise<void> {
  await api.delete(`/paper/projects/${id}`)
}

/** POST /paper/projects/{id}/run 响应（任务化契约）：
 *  - experiment 项目：job_id 有值、status='pending'，project.result 为旧值（异步任务，任务中心可见进度/暂停/取消）
 *  - watch 项目：job_id=null、status='done'，project.result 为最新（同步计算）
 *  - 同项目已有运行中任务时后端返回 409「该项目已有运行中的任务」 */
export interface PaperRunResult {
  job_id: number | null
  status: 'pending' | 'done'
  project: PaperProject
}

/** 运行模拟盘项目（experiment → 异步任务 + job_id；watch → 同步计算返回最新 result）。 */
export async function runPaperProject(id: number): Promise<PaperRunResult> {
  const { data } = await api.post(`/paper/projects/${id}/run`)
  return data
}

// ===== 模拟盘账户（paper accounts，T-01 组合仪表盘）契约 =====

/** 模拟盘账户：初始资金 + 当前可用现金（撮合实时扣减/回补）。 */
export interface PaperAccount {
  id: number
  name: string
  initial_cash: number
  cash: number
  created_at: string | null
  updated_at: string | null
}

/** 持仓行（GET /paper/accounts/{id}/positions）：附最新日线收盘估值。 */
export interface PaperPositionRow {
  account_id: number
  code: string
  name: string
  quantity: number
  avg_cost: number
  /** 最新日线收盘（行情缺失为 null） */
  close: number | null
  market_value: number | null
  pnl: number | null
  pnl_pct: number | null
}

/** 委托（下单即撮合，终态 filled / pending / canceled / rejected）。 */
export interface PaperOrder {
  id: number
  account_id: number
  code: string
  name: string
  side: 'buy' | 'sell'
  order_type: 'market' | 'limit'
  price: number | null
  quantity: number
  filled_qty: number
  filled_avg_price: number | null
  status: 'pending' | 'filled' | 'canceled' | 'rejected'
  reject_reason: string | null
  bar_date: string | null
  created_at: string | null
  updated_at: string | null
}

/** 成交流水：绩效重放依据（成交价/数量/金额/费用/bar 时点）。 */
export interface PaperTrade {
  id: number
  account_id: number
  order_id: number
  code: string
  name: string
  side: 'buy' | 'sell'
  price: number
  quantity: number
  amount: number
  fee: number
  bar_date: string | null
  created_at: string | null
}

/** 绩效指标（GET /paper/accounts/{id}/performance.metrics）；无成交字段为 null。 */
export interface PaperPerformanceMetrics {
  total_return: number | null
  annualized_return: number | null
  max_drawdown: number | null
  sharpe: number | null
  win_rate: number | null
  trade_count: number
  winning_trades: number
  initial_cash: number
  final_equity: number | null
  days: number
}

/** 绩效结果：净值曲线（时间升序）+ 指标。无成交时 metrics 为 null（后端空态，前端渲染须零守卫）。 */
export interface PaperPerformance {
  curve: { date: string; equity: number }[]
  metrics: PaperPerformanceMetrics | null
}

/** POST /paper/accounts 请求体。 */
export interface PaperAccountPayload {
  name?: string
  initial_cash?: number
}

/** POST /paper/accounts/{id}/orders 请求体：bar_date 缺省 = 最新 bar 收盘价撮合。 */
export interface PaperOrderPayload {
  code: string
  side: 'buy' | 'sell'
  order_type?: 'market' | 'limit'
  /** 限价单必填；市价单忽略 */
  price?: number | null
  /** 数量须为正的 100 股整数倍（一手） */
  quantity: number
  bar_date?: string | null
}

/** 获取全部模拟盘账户（外层 {data: [...]} 需解包）。 */
export async function getPaperAccounts(): Promise<PaperAccount[]> {
  const { data } = await api.get('/paper/accounts')
  return data.data
}

/** 创建模拟盘账户（initial_cash 缺省 100 万）。 */
export async function createPaperAccount(payload: PaperAccountPayload): Promise<PaperAccount> {
  const { data } = await api.post('/paper/accounts', payload)
  return data
}

/** 删除模拟盘账户（含持仓/委托/成交级联；204 无响应体）。 */
export async function deletePaperAccount(id: number): Promise<void> {
  await api.delete(`/paper/accounts/${id}`)
}

/** 账户委托列表（新在前）。 */
export async function getPaperOrders(id: number): Promise<PaperOrder[]> {
  const { data } = await api.get(`/paper/accounts/${id}/orders`)
  return data.data
}

/** 账户持仓（附最新收盘估值：市值/浮动盈亏/比例）。 */
export async function getPaperPositions(id: number): Promise<PaperPositionRow[]> {
  const { data } = await api.get(`/paper/accounts/${id}/positions`)
  return data.data
}

/** 账户成交流水（bar_date 倒序，最近 limit 条）。 */
export async function getPaperTrades(id: number, limit = 200): Promise<PaperTrade[]> {
  const { data } = await api.get(`/paper/accounts/${id}/trades`, { params: { limit } })
  return data.data
}

/** 委托下单并撮合（按 bar 收盘价；限价未触发挂起、资金/持仓不足拒绝）。 */
export async function placePaperOrder(id: number, payload: PaperOrderPayload): Promise<PaperOrder> {
  const { data } = await api.post(`/paper/accounts/${id}/orders`, payload)
  return data
}

/** 撤单（仅 pending 委托可撤；filled/canceled/rejected 后端 400）。 */
export async function cancelPaperOrder(id: number, orderId: number): Promise<PaperOrder> {
  const { data } = await api.delete(`/paper/accounts/${id}/orders/${orderId}`)
  return data
}

/** 账户绩效：净值曲线 + 指标（收益/回撤/夏普/胜率）。 */
export async function getPaperPerformance(id: number): Promise<PaperPerformance> {
  const { data } = await api.get(`/paper/accounts/${id}/performance`)
  return data
}

// ===== 系统状态 / 设置域契约（/health、/system/*、/market/sources/*） =====

/** 服务健康信息（GET /health；响应无 data 包装，直接返回对象本体）。 */
export interface SystemHealth {
  status?: string
  app?: string
  gp_backend?: string
  native_available?: boolean
  /** T-81 版本自检：部署代码 commit / 前端构建 commit / 二者一致判定（dev 环境均为 null） */
  backend_commit?: string | null
  frontend_commit?: string | null
  frontend_synced?: boolean | null
}

export async function getSystemHealth(): Promise<SystemHealth> {
  const { data } = await api.get('/health')
  return data
}

/** 单个缓存实例命中统计（/system/cache-stats 元素；与后端 storage/cache.py stats() 对齐）。
 *  命中 = 内存 TTL 内直接返回；计数为进程内累计（clear 不清零）。 */
export interface CacheInstanceStats {
  size: number
  hits: number
  misses: number
  hit_rate: number
}

/** 各全局缓存实例命中统计：{实例名: stats, ..., "total": 合计}（GET /system/cache-stats，外层 {data: ...} 解包）。 */
export async function getCacheStats(): Promise<Record<string, CacheInstanceStats>> {
  const { data } = await api.get('/system/cache-stats')
  return data.data
}

/** 单数据源健康状态（/market/sources/status sources[] 元素）。 */
export interface SourceHealth {
  key: string
  name: string
  ok: boolean
  active: boolean
  latency_ms: number | null
  spot_fails: number
  spot_blocked: boolean
  blocked_until: number | null
  degraded: boolean
  kline_blocked: boolean
  last_ok: string | null
  last_fail: string | null
  last_error: string | null
}

/** 数据源总状态：模式/活跃源/偏好列表/最近切换/各源健康（GET /market/sources/status）。 */
export interface DataSourceStatus {
  mode: 'auto' | 'manual'
  override: string | null
  active: string | null
  preference: string[]
  last_switch_ts: string | number | null
  last_switch_reason: string | null
  sources: SourceHealth[]
}

/** 拉取数据源状态（GET /market/sources/status，外层 {data: ...} 解包）。 */
export async function getDataSourceStatus(): Promise<DataSourceStatus> {
  const { data } = await api.get('/market/sources/status')
  return data.data
}

/** 手动切换数据源（POST /market/sources/switch）：source=auto（自动）或 akshare/sina/tencent（锁定单源）；
 *  非法值 400 由 axios 抛错（detail 在 error.response.data.detail）。返回切换后最新状态。 */
export async function switchDataSource(source: string): Promise<DataSourceStatus> {
  const { data } = await api.post('/market/sources/switch', { source })
  return data.data
}

/** 立即快速探测全部数据源（POST /market/sources/probe），返回探测后最新状态。 */
export async function probeDataSources(): Promise<DataSourceStatus> {
  const { data } = await api.post('/market/sources/probe')
  return data.data
}

/** LLM 连通性测试（POST /copilot/market/chat 非流式消费）：硬超时 15s（axios ECONNABORTED）；
 *  返回响应原文（SSE 文本，调用方只按 2xx 判定连通性）；非 2xx 抛 AxiosError，FastAPI detail 在 error.response.data.detail。 */
export async function testCopilotChat(payload: { question: string; context: Record<string, unknown> }): Promise<string> {
  const resp = await api.post('/copilot/market/chat', payload, { timeout: 15_000 })
  return resp.data as string
}
