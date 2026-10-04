/**
 * 时间格式化唯一出口（前端所有 naive ISO 字符串展示统一走此模块）。
 * 纪律：后端返回 naive ISO（无时区），一律**字符串直解**，禁止 `new Date` 按浏览器时区解析
 * （无时区串被 new Date 按本地时区解析，跨时区会偏移）。
 */
import { isMinutePeriod } from './periods'

/**
 * 信号时点显示：精度随周期变化，且**直接按字段解析后端 naive ISO 字符串**
 * （不依赖浏览器本地时区——无时区串被 new Date 按本地时区解析，跨时区会偏）：
 * - 分钟周期（1/5/15/30/60）：MM-DD HH:mm（**不补年**；跨年分钟数据如需补年走 formatSignalMinute）
 * - 日线：MM-DD（跨年 YYYY-MM-DD，跨年判断基于 nowMs 的**上海年**）
 * - 周/月线：YYYY-MM-DD（bar 锚定具体交易日，保留年份）
 * 第 3 参 nowMs（epoch ms，默认 Date.now()）：跨年判断的参考时钟，测试可冻结；
 * 用上海年（UTC 值 +8h 的 UTC 字段）而非浏览器本地年——负时区用户在跨年窗口（上海 1/1 0:00-8:00）
 * 浏览器年可能仍是去年，会导致补年误判。
 * 兜底：带时区 ISO 用 Asia/Shanghai 市场时区格式化。
 */
export const formatSignalTime = (t: string | null | undefined, period: string, nowMs?: number): string => {
  if (!t) return '—'
  // 归一：后端 updated_at 类字段可能返回空格分隔 naive 串（'2026-08-11 10:30:00'），
  // 统一转 'T' 分隔走正则直解（Safari 的 new Date 不解析空格分隔串，曾因此出现展示缺失）
  const s = t.includes(' ') && !t.includes('T') ? t.replace(' ', 'T') : t
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(s)
  if (m) {
    const [, y, mo, d, h, mi] = m
    const mmdd = `${mo}-${d}`
    if (isMinutePeriod(period)) {
      return `${mmdd} ${h}:${mi}`
    }
    if (period === 'daily') {
      // 上海年（UTC 值 +8h 取 UTC 年份字段），非浏览器本地年
      const shYear = new Date((nowMs ?? Date.now()) + 8 * 3600e3).getUTCFullYear()
      return String(shYear) === y ? mmdd : `${y}-${mmdd}`
    }
    return `${y}-${mmdd}`
  }
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return '—'
  const fmt = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: isMinutePeriod(period) ? '2-digit' : undefined, minute: isMinutePeriod(period) ? '2-digit' : undefined,
  })
  return fmt.format(d)
}

/**
 * 信号时间戳显示到分钟（工作台左侧信号时间线用）：
 * - showDate=false → 'HH:MM'（今日组，如 '10:30'）
 * - showDate=true → 'MM-DD HH:MM'，跨年（上海年 ≠ 解析年）补 'YYYY-MM-DD HH:MM'
 *   （上海年判断对齐 formatSignalTime daily 分支，基于第 3 参 nowMs，测试可冻结）
 * 纪律：naive ISO 一律**字符串直解**（正则拆字段），禁止 `new Date` 按浏览器时区解析；
 * 空格分隔 naive 串（'2026-08-11 10:30:00'）先归一为 'T'。
 * 无法匹配（无时间部分等）→ 回退 formatSignalTime(t, '60') 保证有展示。
 */
export function formatSignalMinute(t: string | null | undefined, showDate: boolean, nowMs?: number): string {
  if (!t) return '—'
  const s = t.includes(' ') && !t.includes('T') ? t.replace(' ', 'T') : t
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(s)
  if (m) {
    const [, y, mo, d, h, mi] = m
    const hm = `${h}:${mi}`
    if (!showDate) return hm
    const shYear = new Date((nowMs ?? Date.now()) + 8 * 3600e3).getUTCFullYear()
    return String(shYear) === y ? `${mo}-${d} ${hm}` : `${y}-${mo}-${d} ${hm}`
  }
  return formatSignalTime(t, '60')
}

/** 完整时间展示选项 */
export interface FormatFullTimeOpts {
  /** 是否带年份前缀（false → 'MM-DD HH:mm:ss'），默认 true */
  withYear?: boolean
  /** 是否带秒（false → 无秒），默认 true；输入本身不含秒时自动省略 */
  withSeconds?: boolean
}

/**
 * 完整时间字符串直解（对齐原 `new Date(t).toLocaleString('zh-CN', { hour12: false })` 带秒行为）：
 * - 默认输出 `YYYY-MM-DD HH:mm:ss`
 * - opts.withYear=false → `MM-DD HH:mm:ss`
 * - opts.withSeconds=false → 无秒
 * - 输入归一：naive ISO 可能 'T' 分隔或空格分隔（如 '2026-08-11 10:30:00'），空格分隔时替换为 'T' 再拆解
 * 输入类型：string（naive ISO 字符串直解，禁 new Date 按浏览器时区解析）；
 * number（epoch ms 时间戳——绝对时刻无 naive ISO 时区歧义，按浏览器本地时区展示，项目既有惯例）。
 * 容错：null/undefined → '—'；无法解析 → 原样返回。
 */
export function formatFullTime(t: string | number | null | undefined, opts?: FormatFullTimeOpts): string {
  if (t === null || t === undefined || t === '') return '—'
  const { withYear = true, withSeconds = true } = opts ?? {}
  if (typeof t === 'number') {
    const d = new Date(t)
    if (Number.isNaN(d.getTime())) return '—'
    const pad = (n: number) => String(n).padStart(2, '0')
    const date = withYear
      ? `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
      : `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    const time = `${pad(d.getHours())}:${pad(d.getMinutes())}${withSeconds ? ':' + pad(d.getSeconds()) : ''}`
    return `${date} ${time}`
  }
  const s = t.includes(' ') && !t.includes('T') ? t.replace(' ', 'T') : t
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(s)
  if (m) {
    const [, y, mo, d, h, mi, sec] = m
    const date = withYear ? `${y}-${mo}-${d}` : `${mo}-${d}`
    return `${date} ${h}:${mi}${withSeconds && sec ? ':' + sec : ''}`
  }
  return t
}

/**
 * epoch ms（绝对时刻）→ 上海时区（UTC+8）naive 串 'YYYY-MM-DD HH:mm:ss'。
 * 与 formatFullTime 的 number 分支同形状，但固定按 Asia/Shanghai（UTC 值 +8h 取 UTC 字段 =
 * 上海本地字段，不依赖浏览器本地时区）——用于「客户端拉取时刻」这类本地时戳的展示：
 * 同面板其余时刻（后端 naive 串直解）均为上海口径，本地时戳必须收敛到同一口径，
 * 否则跨时区用户会看到两套时区并列（禁按浏览器本地时区渲染）。
 * opts.withYear/withSeconds 语义与 formatFullTime 相同（默认带年带秒）。
 */
export function formatShanghaiFullTime(ms: number, opts?: FormatFullTimeOpts): string {
  const d = new Date(ms + 8 * 3600e3)
  const { withYear = true, withSeconds = true } = opts ?? {}
  const pad = (n: number) => String(n).padStart(2, '0')
  const date = withYear
    ? `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`
    : `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`
  const time = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}${withSeconds ? ':' + pad(d.getUTCSeconds()) : ''}`
  return `${date} ${time}`
}

/**
 * naive ISO 字符串直解为市场时区（UTC+8）绝对时刻（epoch ms）。
 * 与 utils/signals.ts groupOf 的 Date.UTC(h-8) 写法一致：无时区串按 Asia/Shanghai 解析，
 * 不依赖浏览器本地时区（无时区串被 new Date 按本地时区解析，跨时区会偏）。
 * 用于 triggered_at 等字段的时间比较/排序（替换 new Date(t).getTime()）。
 * 容错：null/undefined/空串/无法解析 → 0。
 */
export function toMarketEpochMs(t: string | null | undefined): number {
  if (!t) return 0
  const s = t.includes(' ') && !t.includes('T') ? t.replace(' ', 'T') : t
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(s)
  if (m) {
    const [, y, mo, d, h, mi, sec] = m
    return Date.UTC(+y, +mo - 1, +d, +h - 8, +mi, +(sec ?? 0)) // naive 字段 → UTC+8 绝对时刻
  }
  return 0
}

/**
 * 事件列表中「最新」事件：按展示时刻（as_of ?? triggered_at）的 UTC+8 epoch ms 取最大。
 * 排序口径与 resolveSignalMoment/sorter 一致（as_of 优先）；空列表 → null。
 * SignalCenter 分组 latest 与关注页 side 行模型共用（单一实现，避免各自手写 for 循环）。
 */
export function latestEventOf<T extends { as_of?: string | null; triggered_at?: string | null }>(events: readonly T[]): T | null {
  let latest: T | null = null
  let maxMs = -1
  for (const e of events) {
    const ms = toMarketEpochMs(e.as_of ?? e.triggered_at)
    if (ms > maxMs) { maxMs = ms; latest = e }
  }
  return latest
}

/**
 * 扫描发现延迟（时间差）格式化：scan_discovered_at(发现时刻) − triggered_at(自然触发时刻)。
 * - 任一为 null/undefined/无法解析 → null（调用方据此只显示单时间，旧数据兜底）
 * - diff ≤ 1min → '刚刚'；< 60min → 'N分钟'；< 24h → 'N小时'；≥ 24h（跨日）→ 'N天'
 * - diff 为负（数据异常：发现早于触发）→ '刚刚' 兜底，不产生负向文案
 * 纪律不变：naive ISO 一律字符串直解（toMarketEpochMs 按 UTC+8 解析为绝对时刻），禁 new Date。
 */
export function formatDiscoveredGap(
  t: string | null | undefined,
  discoveredAt: string | null | undefined,
): string | null {
  const tMs = toMarketEpochMs(t)
  const dMs = toMarketEpochMs(discoveredAt)
  if (!tMs || !dMs) return null
  const diff = dMs - tMs
  if (diff <= 60e3) return '刚刚'
  if (diff < 60 * 60e3) return `${Math.floor(diff / 60e3)}分钟`
  if (diff < 24 * 60 * 60e3) return `${Math.floor(diff / 3600e3)}小时`
  return `${Math.floor(diff / 86400e3)}天`
}

// ===== 周期语义状态机 resolveSignalMoment（单一事实源） =====
// 纪律不变：naive ISO 一律字符串直解（toMarketEpochMs 按 UTC+8 解析为绝对时刻），
// 禁 new Date 按浏览器时区解析；「上海日期/上海当前时刻」由 nowMs 的 UTC 值 +8h 推导。
// 语义总纲：kind 表达「时段语义」（盘中/今日收盘/昨日/更早/未来），label 恒为周期原生精度
// 绝对时刻（分钟 HH:mm｜日 MM-DD｜周月 YYYY-MM-DD，分钟跨日/跨年、日历周期跨年补 YYYY-），
// hint 为相对上下文词（盘中/今天/今天收盘/昨天/本周/本月），title 为 tooltip 双时间+相对词。

/** 指定时刻（epoch ms）的上海日期（YYYY-MM-DD，naive 日历口径） */
const cnDateOf = (nowMs: number): string => new Date(nowMs + 8 * 3600e3).toISOString().slice(0, 10)

/**
 * 上海今日日期（YYYY-MM-DD，naive 日历口径；UTC 值 +8h 取 UTC 字段 = 上海本地字段，
 * 不依赖浏览器时区，与内部 cnDateOf 同口径）。供 K 线「盘中实时合成」标注等今日判定场景；
 * 第 1 参 nowMs（epoch ms，默认 Date.now()）测试可冻结。
 */
export function cnTodayOf(nowMs?: number): string {
  return cnDateOf(nowMs ?? Date.now())
}

/** 上海昨天日期（YYYY-MM-DD；跨月/跨年由 Date 对象毫秒运算自动处理） */
const cnYesterdayOf = (nowMs: number): string => cnDateOf(nowMs - 86400e3)

/** 展示时刻的上海日期（naive 字符串前 10 位即 YYYY-MM-DD；空/异常返回 null） */
const datePart = (s: string): string | null => /^\d{4}-\d{2}-\d{2}/.test(s) ? s.slice(0, 10) : null

/** 上海当前时刻是否早于 15:05（收盘后 5 分钟宽限；UTC 值 +8h 后取 UTC 字段 = 上海本地字段） */
const beforeClose = (nowMs: number): boolean => {
  const nowSh = new Date(nowMs + 8 * 3600e3)
  const h = nowSh.getUTCHours()
  const mi = nowSh.getUTCMinutes()
  return h < 15 || (h === 15 && mi < 5)
}

/**
 * 上海交易日盘中判定（工作日 09:30~15:05，含收盘 5 分钟宽限，午休视为盘中——数据源午间返回
 * 上午 partial bar，允许刷新）。节假日误判为盘中无碍：后端 is_stale 非交易时段不 stale，
 * 请求仅 DB 读零网络。供前端轮询调度（K 线实时刷新等）使用；UTC 值 +8h 取 UTC 字段 =
 * 上海本地字段，不依赖浏览器时区。
 */
export function isCnTradingSession(nowMs?: number): boolean {
  const nowSh = new Date((nowMs ?? Date.now()) + 8 * 3600e3)
  const day = nowSh.getUTCDay() // 0=周日
  if (day === 0 || day === 6) return false
  const t = nowSh.getUTCHours() * 60 + nowSh.getUTCMinutes()
  return t >= 9 * 60 + 30 && t < 15 * 60 + 5
}

/** 上海日历日所在周的周一（一周起点）epoch ms：偏移 = (weekday+6)%7（周一=0 … 周日=6） */
const weekStartMs = (d: Date): number => {
  const off = (d.getUTCDay() + 6) % 7
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() - off)
}

/**
 * 周期语义状态机结果（单一事实源，SignalCenter / MobileSignalList 时点列共用）：
 * - kind：盘中（intraday）/ 今日收盘后（today_closed）/ 昨日（yesterday）/ 更早（older）/ 未来异常（future）
 * - label：周期原生精度绝对时刻（分钟 HH:mm 今日 / MM-DD HH:mm 跨日，日历周期 MM-DD 日线 /
 *   YYYY-MM-DD 周月；跨年一律补 YYYY-）——只含绝对时刻，不含相对词
 * - hint：相对上下文词（盘中/今天/今天收盘/昨天/本周/本月），无则 null
 * - intraday：是否盘中（hint 着色的判定依据）
 * - sortEpoch：排序键（展示时刻 as_of ?? t 的 UTC+8 epoch ms）
 * - title：tooltip（双时间 + 相对时间，均空 → ''）
 */
export interface SignalMoment {
  kind: 'intraday' | 'today_closed' | 'yesterday' | 'older' | 'future'
  label: string
  hint: string | null
  intraday: boolean
  sortEpoch: number
  title: string
}

/**
 * 周期语义状态机（单一事实源）：period × 时点（t=bar 标签 / asOf=数据截止时刻）→ 语义结构。
 * 展示时刻 = asOf ?? t（as_of 为空/相等的时段即 bar 标签本身）。
 *
 * kind 判定（日历周期 daily/weekly/monthly）：
 * - asOf 存在且 ≠ t（后端盘中标记）→ intraday
 * - 无 asOf 分支（bar 标签）：tMs > nowMs+5min（未来 bar 标签）→ 上海当前 <15:05 ? intraday : future
 *   （盘中先行标注当日在途信号；收盘后仍出现未来标签视为数据异常）
 * - 上海日期==今天 → 上海当前 <15:05 ? intraday : today_closed
 * - 上海日期==昨天 → weekly/monthly 不产 'yesterday'（周月锚定交易日，昨天词会误导）→ older；daily → yesterday
 * - 其余 older
 *
 * kind 判定（分钟周期 1/5/15/30/60，as_of 恒 null）：
 * - tMs > nowMs+5min → future
 * - 上海日期==今天 → <15:05 ? intraday : today_closed；==昨天 → yesterday；其余 older
 *
 * label（周期原生精度，禁相对词）：
 * - 分钟：上海日期==今天 → 仅 HH:mm（如 '14:25'）；否则 formatSignalTime（MM-DD HH:mm，跨年补 YYYY-）
 * - daily → formatSignalTime(show,'daily')；weekly/monthly → formatSignalTime(show,'weekly')（均 YYYY-MM-DD）
 * - kind==='future' 时分钟回退 formatSignalTime(show, period)（未来时刻无今日简称意义）
 *
 * hint：intraday→'盘中'；today_closed→分钟'今天'/日线'今天收盘'/周月 null；yesterday→'昨天'；
 * weekly older 且 bar 所在周==当前周→'本周'；monthly older 且同年同月→'本月'；其余 null。
 *
 * sortEpoch = 展示时刻 epoch ms（排序键，对齐 sorter 的 as_of 优先）。
 *
 * title（tooltip）：asOf 存在且 ≠ showMs → 「as_of 完整时间 · bar t 完整时间」；否则展示时刻完整时间；
 * 追加相对词（基于 nowMs-showMs）：<1min '刚刚' / <1h 'N 分钟前' / <24h 'N 小时前' / ≥24h 不追加；
 * 基础为 '—'/'' → ''。
 *
 * 第 4 参 nowMs 必须支持（测试用固定时钟）；缺省 Date.now()。内部所有相对判定
 * （今天/昨天/未来/盘中）基于 nowMs，换算上海时刻 = new Date(nowMs + 8*3600e3) 的 UTC 字段。
 * 兜底：showMs===0（缺失或无法解析）→ { kind:'older', label:'—', … }，不进任何相对判定。
 */
export function resolveSignalMoment(
  period: string,
  t: string | null | undefined,
  asOf: string | null | undefined,
  nowMs: number = Date.now(),
): SignalMoment {
  const show = asOf ?? t
  const showMs = toMarketEpochMs(show)
  if (!show || !showMs) return { kind: 'older', label: '—', hint: null, intraday: false, sortEpoch: 0, title: '' }

  const minute = isMinutePeriod(period)
  const tMs = toMarketEpochMs(t)
  const shToday = cnDateOf(nowMs)
  const shYesterday = cnYesterdayOf(nowMs)
  const tDate = datePart(t ?? '')
  const closing = beforeClose(nowMs) // 上海当前 <15:05

  // ===== kind 判定 =====
  let kind: SignalMoment['kind']
  if (minute) {
    if (tMs > nowMs + 5 * 60e3) kind = 'future'
    else if (tDate === shToday) kind = closing ? 'intraday' : 'today_closed'
    else if (tDate === shYesterday) kind = 'yesterday'
    else kind = 'older'
  } else if (asOf && toMarketEpochMs(asOf) !== tMs) {
    kind = 'intraday' // 后端盘中标记：as_of 后于 bar 标签
  } else if (tMs > nowMs + 5 * 60e3) {
    kind = closing ? 'intraday' : 'future' // 未来 bar 标签：盘中=在途信号，收盘后=异常
  } else if (tDate === shToday) {
    kind = closing ? 'intraday' : 'today_closed'
  } else if (tDate === shYesterday) {
    kind = period === 'daily' ? 'yesterday' : 'older' // 周月锚定交易日，不产昨天词
  } else {
    kind = 'older'
  }

  // ===== label（周期原生精度绝对时刻，禁相对词） =====
  let label: string
  if (minute) {
    if (kind === 'future' || tDate !== shToday) {
      // 跨日/未来：MM-DD HH:mm（跨年补 YYYY-）——formatSignalMinute(show,true) 语义一致且含跨年补年
      label = formatSignalMinute(show, true, nowMs)
    } else {
      // 今日分钟：仅 HH:mm（naive 字符串直解，禁 new Date）
      const s = show.includes(' ') && !show.includes('T') ? show.replace(' ', 'T') : show
      const hm = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(s)
      label = hm ? `${hm[4]}:${hm[5]}` : formatSignalTime(show, period, nowMs)
    }
  } else if (period === 'daily') {
    label = formatSignalTime(show, 'daily', nowMs) // MM-DD（跨年补 YYYY-）
  } else {
    label = formatSignalTime(show, 'weekly', nowMs) // 周/月统一 YYYY-MM-DD
  }

  // ===== hint（相对上下文词） =====
  let hint: string | null = null
  if (kind === 'intraday') {
    hint = '盘中'
  } else if (kind === 'today_closed') {
    hint = minute ? '今天' : (period === 'daily' ? '今天收盘' : null)
  } else if (kind === 'yesterday') {
    hint = '昨天'
  } else if (kind === 'older') {
    // 周/月相对词基于 showMs+8h 与 nowMs+8h 的 UTC 日期字段（上海日历口径）
    const barSh = new Date(showMs + 8 * 3600e3)
    const nowSh = new Date(nowMs + 8 * 3600e3)
    if (period === 'weekly') {
      hint = weekStartMs(barSh) === weekStartMs(nowSh) ? '本周' : null
    } else if (period === 'monthly') {
      hint = (barSh.getUTCFullYear() === nowSh.getUTCFullYear() && barSh.getUTCMonth() === nowSh.getUTCMonth())
        ? '本月' : null
    }
  }
  // 周月 yesterday 词已由判定规避（→older），此处 hint 保持 null

  // ===== sortEpoch = 展示时刻（as_of 优先，对齐 sorter） =====
  const sortEpoch = showMs

  // ===== title（tooltip：双时间 + 相对时间） =====
  // 双时间条件与 kind 判定一致：asOf 存在且 ≠ t（后端盘中标记）→ as_of 完整时间 · bar 标签完整时间
  const base = (asOf && toMarketEpochMs(asOf) !== tMs)
    ? `${formatFullTime(asOf)} · bar ${formatFullTime(t ?? asOf)}`
    : formatFullTime(show)
  let title = ''
  if (base !== '—' && base !== '') {
    const diff = nowMs - showMs
    let rel: string | null = null
    if (diff < 60e3) rel = '刚刚'
    else if (diff < 60 * 60e3) rel = `${Math.floor(diff / 60e3)} 分钟前`
    else if (diff < 24 * 60 * 60e3) rel = `${Math.floor(diff / 3600e3)} 小时前`
    title = rel ? `${base} · ${rel}` : base
  }

  return { kind, label, hint, intraday: kind === 'intraday', sortEpoch, title }
}
