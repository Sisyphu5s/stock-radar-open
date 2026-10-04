/**
 * 多股对比页（/compare）计算层：全部为前端纯函数（无 React、无图表实例）。
 *
 * 指标公式（A 股惯例，基于日收盘序列）：
 * - 区间涨跌幅 = close[-1] / close[0] - 1
 * - 年化波动   = std(日收益) × √252（样本标准差，分母 n-1）
 * - 最大回撤   = max((runningMax - close) / runningMax)，返回正数幅度，展示层取负
 * - 夏普比率   = (年化收益 - 无风险利率) / 年化波动；无风险利率按 0 简化（展示层可标注假设）
 * - 相关性     = 两两日收益序列按「日期交集」对齐后的 Pearson 相关系数
 * 数据不足：序列长度 < MIN_COMPARE_BARS(20) → 指标/相关返回 null，展示「数据不足」。
 *
 * 最近使用持久化（对比页选项 = 关注列表 + 最近使用，localStorage 单一事实源）：
 * 本页选择即记录，容量上限 RECENT_MAX，最新在前、按 code 去重。
 */

export const MIN_COMPARE_BARS = 20
/** K 线回溯天数（对比口径：近一年交易日 ≈ 250） */
export const COMPARE_DAYS = 250
/** A 股年化交易日数（波动/夏普年化因子） */
export const TRADING_DAYS_PER_YEAR = 252
/** 对比上限（MultiSelect maxValues 与计算层共用同一常量） */
export const MAX_COMPARE_STOCKS = 8

export interface CompareStock {
  code: string
  name: string
}

/** 日收益序列（对数口径不用，普通百分比收益）：r_i = close[i]/close[i-1] - 1；非法前值跳过 */
export function dailyReturns(closes: number[]): number[] {
  const out: number[] = []
  for (let i = 1; i < closes.length; i++) {
    const prev = closes[i - 1]
    if (prev > 0 && Number.isFinite(closes[i])) out.push(closes[i] / prev - 1)
  }
  return out
}

/** 区间涨跌幅：close[-1]/close[0] - 1；首值非法返回 null */
export function periodReturn(closes: number[]): number | null {
  if (!closes.length || !(closes[0] > 0) || !Number.isFinite(closes[closes.length - 1])) return null
  return closes[closes.length - 1] / closes[0] - 1
}

/** 年化波动：样本标准差(√(Σ(x-μ)²/(n-1))) × √252；不足 2 根收益样本返回 null */
export function annualVolatility(closes: number[]): number | null {
  const r = dailyReturns(closes)
  if (r.length < 2) return null
  const mean = r.reduce((a, b) => a + b, 0) / r.length
  const variance = r.reduce((acc, x) => acc + (x - mean) ** 2, 0) / (r.length - 1)
  return Math.sqrt(variance) * Math.sqrt(TRADING_DAYS_PER_YEAR)
}

/** 最大回撤（正数幅度，展示取负）：max over i of (runningMax_i - close_i)/runningMax_i */
export function maxDrawdown(closes: number[]): number | null {
  let peak = -Infinity
  let maxDd = 0
  for (const c of closes) {
    if (!Number.isFinite(c)) continue
    if (c > peak) peak = c
    if (peak > 0) maxDd = Math.max(maxDd, (peak - c) / peak)
  }
  return maxDd
}

/**
 * 回撤序列（负值口径，与 maxDrawdown 同源）：(nav_i - runningMax_i) / runningMax_i。
 * 非法值原样透传；首点无回撤(0)；净值峰值非正时返回 null（回撤无意义）。
 * 供回测净值 → 回撤曲线图使用（T-03）。
 */
export function drawdownSeries(nav: (number | null)[]): (number | null)[] {
  let peak = -Infinity
  return nav.map((v) => {
    if (v == null || !Number.isFinite(v)) return null
    if (v > peak) peak = v
    if (!(peak > 0)) return null
    return (v - peak) / peak
  })
}

/** 夏普比率：(年化收益 - riskFree) / 年化波动；无风险利率默认 0；波动为零返回 null */
export function sharpeRatio(closes: number[], riskFree = 0): number | null {
  const r = dailyReturns(closes)
  if (r.length < 2) return null
  const vol = annualVolatility(closes)
  if (vol == null || vol === 0) return null
  const mean = r.reduce((a, b) => a + b, 0) / r.length
  return (mean * TRADING_DAYS_PER_YEAR - riskFree) / vol
}

export interface CompareMetrics {
  periodReturn: number | null
  annualVol: number | null
  maxDD: number | null
  sharpe: number | null
}

/** 每股指标打包：数据不足(< MIN_COMPARE_BARS 根 K 线) → 全 null（展示「数据不足」） */
export function computeStockMetrics(closes: number[]): CompareMetrics {
  if (closes.length < MIN_COMPARE_BARS) {
    return { periodReturn: null, annualVol: null, maxDD: null, sharpe: null }
  }
  return {
    periodReturn: periodReturn(closes),
    annualVol: annualVolatility(closes),
    maxDD: maxDrawdown(closes),
    sharpe: sharpeRatio(closes),
  }
}

/** 归一化净值序列：close/close[0]（首日 = 1）；首值非法返回 [] */
export function normalizeCloses(closes: number[]): number[] {
  if (!closes.length || !(closes[0] > 0)) return []
  const base = closes[0]
  return closes.map((c) => (Number.isFinite(c) ? c / base : NaN))
}

/** 日收益按日期建索引：dates[i] 承载 i 根 K 线的收益（i ≥ 1）；非法收益跳过 */
export function returnByDate(dates: string[], closes: number[]): Map<string, number> {
  const n = Math.min(dates.length, closes.length)
  const m = new Map<string, number>()
  for (let i = 1; i < n; i++) {
    const prev = closes[i - 1]
    if (!(prev > 0) || !Number.isFinite(closes[i])) continue
    m.set(dates[i], closes[i] / prev - 1)
  }
  return m
}

/** Pearson 相关系数（两序列同序对齐；样本数 <2 或任一侧零方差 → null；结果截断到 [-1, 1]） */
export function pearson(a: number[], b: number[]): number | null {
  const n = a.length
  if (n < 2 || n !== b.length) return null
  let ma = 0
  let mb = 0
  for (let i = 0; i < n; i++) { ma += a[i]; mb += b[i] }
  ma /= n
  mb /= n
  let num = 0
  let da = 0
  let db = 0
  for (let i = 0; i < n; i++) {
    const x = a[i] - ma
    const y = b[i] - mb
    num += x * y
    da += x * x
    db += y * y
  }
  if (da === 0 || db === 0) return null
  const r = num / Math.sqrt(da * db)
  return Number.isFinite(r) ? Math.max(-1, Math.min(1, r)) : null
}

/**
 * 两两 Pearson 相关性矩阵（n×n，对角 = 1）：
 * 任一侧数据不足（closes < MIN_COMPARE_BARS）→ null（展示「数据不足」）；
 * 两序列共同交易日 <2 → null（展示「—」）。
 */
export function pairwiseCorrelation(series: { dates: string[]; closes: number[] }[]): (number | null)[][] {
  const rets = series.map((s) => returnByDate(s.dates, s.closes))
  const ok = series.map((s) => s.closes.length >= MIN_COMPARE_BARS)
  return series.map((_, i) => series.map((_, j) => {
    if (!ok[i] || !ok[j]) return null
    if (i === j) return 1
    const ai = rets[i]
    const aj = rets[j]
    const a: number[] = []
    const b: number[] = []
    for (const d of ai.keys()) {
      const vj = aj.get(d)
      if (vj !== undefined) {
        a.push(ai.get(d) as number)
        b.push(vj)
      }
    }
    return a.length >= 2 ? pearson(a, b) : null
  }))
}

/** 十六进制色 → rgba（热力着色用；非法输入原样返回） */
export function hexToRgba(hex: string, alpha: number): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim())
  if (!m) return hex
  const n = parseInt(m[1], 16)
  const r = (n >> 16) & 255
  const g = (n >> 8) & 255
  const b = n & 255
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

// ===== 最近使用持久化（对比页选择记录，选项补充源） =====

const RECENT_STORAGE_KEY = 'sr-compare-recent'
const RECENT_MAX = 8

/** 读取最近使用股票（损坏/不可用时回退空列表） */
export function loadRecentStocks(): CompareStock[] {
  try {
    const raw = JSON.parse(localStorage.getItem(RECENT_STORAGE_KEY) ?? '[]') as unknown
    if (!Array.isArray(raw)) return []
    return raw
      .filter((x): x is CompareStock => !!x && typeof (x as CompareStock).code === 'string')
      .slice(0, RECENT_MAX)
  } catch {
    return []
  }
}

/** 记录/刷新一只股票到最近使用（最新在前、按 code 去重、容量上限 RECENT_MAX） */
export function touchRecentStock(code: string, name: string): void {
  try {
    const list = loadRecentStocks().filter((s) => s.code !== code)
    list.unshift({ code, name })
    localStorage.setItem(RECENT_STORAGE_KEY, JSON.stringify(list.slice(0, RECENT_MAX)))
  } catch {
    /* localStorage 不可用忽略 */
  }
}
