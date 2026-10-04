/**
 * K 线数据 → lightweight-charts 纯数据转换层（无 React、无图表实例）。
 * 供 KlineChart 重构使用；所有时间序列类型统一从这里 re-export，调用方不直接依赖包路径。
 *
 * 时间纪律：后端返回 naive ISO（无时区），一律**字符串/字段直解**，禁止 `new Date` 按浏览器时区解析
 * （无时区串被 new Date 按本地时区解析，跨时区会偏移）。见 utils/time.ts 同款纪律。
 */
import type { Time, UTCTimestamp, CandlestickData, LineData, HistogramData, TickMarkFormatter } from 'lightweight-charts'
import { isMinutePeriod } from './periods'

// ---- 类型 re-export：调用方统一从这里 import，不直接依赖 lightweight-charts 包路径 ----
export type {
  Time, UTCTimestamp, BusinessDay, CandlestickData, LineData, HistogramData, TickMarkFormatter,
} from 'lightweight-charts'

/** K 线数据契约（后端 /stocks/{code}/kline 返回结构；KlineChart/数据层/工作台统一从本模块导入） */
export interface KlineData {
  dates: string[]
  open: number[]
  high: number[]
  low: number[]
  close: number[]
  volume: number[]
  indicators?: Record<string, number[]>
}

/** 成交量/成交额中文缩写：万/亿（信息条、图例、成交量轴共用） */
export function fmtVol(v: number): string {
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(1) + '万'
  return String(Math.round(v))
}

/** 成交额估算：均价 × 量 × 100（A 股一手 = 100 股），信息条共用 */
export const estAmount = (o: number, h: number, l: number, c: number, v: number): number => ((o + h + l + c) / 4) * v * 100

const pad2 = (n: number): string => String(n).padStart(2, '0')

/** 归一后端 naive 串：空格分隔（'2026-08-07 15:00:00'）→ 'T' 分隔（与 utils/time.ts 归一一致） */
const normalizeNaive = (d: string): string =>
  d.includes(' ') && !d.includes('T') ? d.replace(' ', 'T') : d

/** 日期/时间字段拆分正则（naive ISO，T 或空格分隔，秒可省略） */
const NAIVE_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?(?::(\d{2}))?/

/**
 * naive 完整时刻串 → UTC 秒（epoch 秒，UTCTimestamp）：**时刻序列**（watch 快照曲线等，与监控周期无关）用。
 * 与 klineTime 分钟分支同源（Date.UTC 手算，不依赖浏览器本地时区），但不受 period 影响——
 * 日/周/月周期下同一天可能有多条快照（watch 60s 落库一条），必须保留时刻精度才能时间唯一且升序
 * （lightweight-charts 要求同 series 时间严格升序唯一，日期字符串重复会静默替换丢数据）。
 * 无法解析返回 null（调用方跳过该点；正常数据源不会触发）。
 */
export function klineTsTime(d: string): UTCTimestamp | null {
  const m = NAIVE_RE.exec(normalizeNaive(d))
  if (!m) return null
  const [, y, mo, day, hh, mi, ss] = m
  // UTCTimestamp 是 branded number，Math.floor 结果需 cast
  return Math.floor(Date.UTC(+y, +mo - 1, +day, +(hh ?? 0), +(mi ?? 0), +(ss ?? 0)) / 1000) as UTCTimestamp
}

/**
 * 后端 naive 日期串 → lightweight-charts Time：
 * - 日/周/月线 '2026-08-07' → 直接返回字符串 '2026-08-07'
 * - 分钟线 '2026-08-07 15:00:00'（或 'T' 分隔）→ **手动字符串拆分 → UTC 秒时间戳**（Date.UTC 纯 UTC
 *   计算，不依赖浏览器本地时区；UTCTimestamp 单位是秒）。同一交易日的多根分钟 bar 时间必须
 *   逐根唯一且升序——BusinessDay 只到天，会重复（lightweight-charts 断言 "data must be asc
 *   ordered by time" 抛错，曾导致 `_private__timeScale` 连锁崩溃）。
 * - 非法输入：返回该日期字符串原样（调用方兜底）。
 */
export function klineTime(d: string, period: string): Time {
  const s = normalizeNaive(d)
  const m = NAIVE_RE.exec(s)
  if (!m) {
    // 完全非法输入：分钟周期兜底为日期前缀的 UTC 秒（同 series 格式统一，防混入 string 触发
    // "one time format per series" 违规）；日期也解析不了才原样返回（正常数据源不会触发）
    if (isMinutePeriod(period)) {
      const dm = NAIVE_RE.exec(normalizeNaive(d.slice(0, 10)))
      if (dm) return Math.floor(Date.UTC(+dm[1], +dm[2] - 1, +dm[3]) / 1000) as UTCTimestamp
    }
    return d // 非法输入：原样返回
  }
  if (isMinutePeriod(period)) {
    // 分钟级：UTC 秒（与 klineTsTime 同源，watch 曲线共用单一实现；m 已匹配故 klineTsTime 必非 null，
    // ?? d 仅类型收窄——理论不可达）
    return klineTsTime(d) ?? d
  }
  const [, y, mo, day] = m
  // 日/周/月线：字符串时间（按 YYYY-MM-DD 排序即时间序）
  return `${y}-${mo}-${day}`
}

/**
 * K 线数据 → CandlestickData[]：{ time, open, high, low, close }，数值保持原样。
 * 长度按 dates 与各价格数组的公共长度截断。
 */
export function klineCandles(data: KlineData, period: string): CandlestickData[] {
  const { dates, open, high, low, close } = data
  const n = Math.min(dates.length, open.length, high.length, low.length, close.length)
  const out: CandlestickData[] = []
  for (let i = 0; i < n; i++) {
    out.push({ time: klineTime(dates[i], period), open: open[i], high: high[i], low: low[i], close: close[i] })
  }
  return out
}

/**
 * 成交量 → HistogramData[]：{ time, value, color }。
 * 颜色由调用方传入：收涨（close >= open）用 up，收跌用 down（主题敏感色由调用方按暗/亮主题决定）。
 */
export function klineVolume(data: KlineData, period: string, up: string, down: string): HistogramData[] {
  const { dates, open, close, volume } = data
  const n = Math.min(dates.length, open.length, close.length, volume.length)
  const out: HistogramData[] = []
  for (let i = 0; i < n; i++) {
    out.push({ time: klineTime(dates[i], period), value: volume[i], color: close[i] >= open[i] ? up : down })
  }
  return out
}

/** 从 indicators[field] 提取非法值省略的单值序列（null/NaN/undefined 直接省略，lightweight-charts 不接受 null） */
function toSeries(
  field: string, data: KlineData, period: string,
): { time: Time; value: number }[] {
  const vals = data.indicators?.[field]
  if (!vals) return []
  const n = Math.min(data.dates.length, vals.length)
  const out: { time: Time; value: number }[] = []
  for (let i = 0; i < n; i++) {
    const v = vals[i]
    if (v == null || Number.isNaN(v)) continue // null/undefined/NaN 省略
    out.push({ time: klineTime(data.dates[i], period), value: v })
  }
  return out
}

/** 主图叠加线（均线/BOLL 等）→ LineData[]；null/NaN/undefined 的 point 直接省略；字段缺失返回 [] */
export function klineLine(field: string, data: KlineData, period: string): LineData[] {
  return toSeries(field, data, period) as LineData[]
}

/**
 * 副图单值指标（MACD 柱类）→ HistogramData[]；非法值省略；字段缺失返回 []。
 * colorFor 可选：按数值返回数据点 color（SKILL per-bar colors：逐点颜色放在数据点上，
 * 未提供时回退 series 级 color）。MACD 柱双色典型用法：v >= 0 涨色 / v < 0 跌色。
 */
export function klineHistogram(
  field: string, data: KlineData, period: string,
  colorFor?: (value: number) => string,
): HistogramData[] {
  const points = toSeries(field, data, period)
  return colorFor ? points.map((p) => ({ ...p, color: colorFor(p.value) })) : (points as HistogramData[])
}

/** 拆解 Time 为字段：string/BusinessDay 用字段拆分；UTCTimestamp 用 UTC 字段（不依赖浏览器本地时区） */
interface SplitFields { year: number; month: number; day: number; hh?: number; mm?: number }
function splitTime(time: Time): SplitFields | null {
  if (typeof time === 'string') {
    const m = NAIVE_RE.exec(normalizeNaive(time))
    if (!m) return null
    const [, y, mo, d, h, mi] = m
    return { year: +y, month: +mo, day: +d, hh: h !== undefined ? +h : undefined, mm: mi !== undefined ? +mi : undefined }
  }
  if (typeof time === 'number') {
    // UTCTimestamp（epoch 秒）→ UTC 字段直取，避免本地时区偏移
    const dt = new Date(time * 1000)
    if (Number.isNaN(dt.getTime())) return null
    return { year: dt.getUTCFullYear(), month: dt.getUTCMonth() + 1, day: dt.getUTCDate(), hh: dt.getUTCHours(), mm: dt.getUTCMinutes() }
  }
  return { year: time.year, month: time.month, day: time.day }
}

/** 当前上海时区（UTC+8）日期字段——tick 跨日/跨年判断的参照（对齐 formatSignalTime daily 分支的上海年） */
function nowUtcFields(): { year: number; month: number; day: number } {
  const d = new Date(Date.now() + 8 * 3600e3)
  return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate() }
}

/**
 * 复刻 fmtAxisDate 语义的 lightweight-charts TickMarkFormatter：
 * - 分钟线：HH:MM；跨日（tick 日期 ≠ 当前上海日期）补 MM-DD 前缀
 * - 日线：MM-DD；跨年（tick 年份 ≠ 当前上海年份）补 YYYY-（与 formatSignalTime daily 分支一致）
 * - 周线：MM-DD；01 月补 YYYY-MM（避免跨年混淆）
 * - 月线：YYYY-MM
 * 输入为 Time（string | BusinessDay | UTCTimestamp）：string/BusinessDay 用字段拆分格式化，
 * UTCTimestamp 用 UTC 字段；跨日/跨年判断统一用当前上海时区（UTC+8）字段，全程不触碰浏览器本地时区。
 */
export function klineTickFormatter(period: string): TickMarkFormatter {
  return (time) => {
    const t = splitTime(time)
    if (!t) return String(time)
    const mmdd = `${pad2(t.month)}-${pad2(t.day)}`
    if (isMinutePeriod(period)) {
      if (t.hh === undefined || t.mm === undefined) return mmdd // 异常串（无时间部分）回退 MM-DD
      const now = nowUtcFields()
      const isToday = now.year === t.year && now.month === t.month && now.day === t.day
      return isToday ? `${pad2(t.hh)}:${pad2(t.mm)}` : `${mmdd} ${pad2(t.hh)}:${pad2(t.mm)}`
    }
    if (period === 'monthly') return `${t.year}-${pad2(t.month)}`
    if (period === 'weekly') return t.month === 1 ? `${t.year}-${pad2(t.month)}` : mmdd
    // daily：跨年补年份
    return nowUtcFields().year === t.year ? mmdd : `${t.year}-${mmdd}`
  }
}

/**
 * naive 日期串 → 轴标签文本（信息条等字符串输入场景；时间轴刻度用 klineTickFormatter）。
 * 语义与 klineTickFormatter 完全一致：分钟 HH:MM（跨日补 MM-DD）、日线 MM-DD（跨年补年份）、
 * 周线 MM-DD（01 月补 YYYY-MM）、月线 YYYY-MM。实现走 klineTime + klineTickFormatter 单一链路，无重复分支。
 * T-28 收敛源：PaperPortfolio.fmtAxisDate（旧 slice(5) 无跨年补年）/ WatchResults.fmtAxisTs（旧手写 replace/slice）。
 */
export function klineAxisLabel(d: string, period: string): string {
  // TickMarkFormatter 契约要求 3 参（time, tickMarkType, locale）；本函数只关心 time，后两参透传默认值
  // 返回类型声明含 null（库宽松声明），实际实现恒返回 string——?? '' 仅类型收窄，理论不可达
  const fmt = klineTickFormatter(period)
  return fmt(klineTime(d, period), 0, 'zh-CN') ?? ''
}

/** 主图叠加线显示名映射：ma5→MA5、ema12→EMA12、expma12→EXPMA12、boll_upper→BOLL上轨…；未知 key 返回 key.toUpperCase() */
const SERIES_NAMES: Record<string, string> = {
  ma5: 'MA5', ma10: 'MA10', ma20: 'MA20', ma30: 'MA30', ma60: 'MA60', ma120: 'MA120', ma250: 'MA250',
  ema5: 'EMA5', ema12: 'EMA12', ema20: 'EMA20', ema26: 'EMA26', ema60: 'EMA60',
  expma12: 'EXPMA12', expma26: 'EXPMA26',
  boll_upper: 'BOLL上轨', boll_mid: 'BOLL中轨', boll_lower: 'BOLL下轨',
}
export function klineSeriesName(k: string): string {
  return SERIES_NAMES[k] ?? k.toUpperCase()
}
