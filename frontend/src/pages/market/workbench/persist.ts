import type { IndicatorParamValues, IndicatorSpec } from '../../../api/client'
import { TUNE_LABEL } from './context'
import { loadSplitSizes, persistSplitSizes } from '../../../utils/splitPersist'
import type { SplitConstraints } from '../../../utils/splitPersist'

/** 信号名回退中文映射（服务端目录缺失时兜底展示用） */
export const FALLBACK_SIGNAL_LABEL: Record<string, { text: string; color: string }> = {
  trend_breakout: { text: '趋势突破', color: 'red' },
  volume_surge: { text: '放量', color: 'orange' },
  price_up: { text: '上涨', color: 'volcano' },
  rsi_cross_up: { text: 'RSI上穿', color: 'green' },
  rsi_above: { text: 'RSI超买', color: 'purple' },
  macd_golden_cross: { text: 'MACD金叉', color: 'cyan' },
  boll_breakout: { text: '布林突破', color: 'geekblue' },
  kdj_golden_cross: { text: 'KDJ金叉', color: 'blue' },
  ma_support: { text: 'MA支撑', color: 'gold' },
}

export const TUNE_OPTIONS_FALLBACK = ['rsi', 'ma', 'ema', 'macd', 'kdj', 'boll', 'wr', 'cci', 'roc', 'mtm', 'bias', 'psy', 'trix', 'cmo', 'volume_ratio']
  .map((k) => ({ value: k, label: TUNE_LABEL[k] }))

/** 手动覆盖持久化（版本化 key，避免旧 sr-tuned-* 语义冲突）：{indicator: {具名参数}} */
const MANUAL_KEY = 'sr-ind-manual:v2'
export const loadManual = (): Record<string, IndicatorParamValues> => {
  try {
    const raw = localStorage.getItem(MANUAL_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as unknown
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        const out: Record<string, IndicatorParamValues> = {}
        for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
          if (v && typeof v === 'object' && !Array.isArray(v)) out[k] = v as IndicatorParamValues
        }
        return out
      }
    }
  } catch { /* localStorage 不可用/损坏时回退空 */ }
  return {}
}
export const persistManual = (m: Record<string, IndicatorParamValues>) => {
  try { localStorage.setItem(MANUAL_KEY, JSON.stringify(m)) } catch { /* ignore */ }
}

/** 旧 sr-tuned-* 字符串（'6' / '12,26,9' / '20,2.5'）→ 具名对象（按 spec 参数顺序） */
const legacyStrToNamed = (spec: IndicatorSpec, raw: string): IndicatorParamValues | null => {
  const parts = raw.split(',').map((s) => Number(s.trim()))
  if (parts.length !== spec.params.length || parts.some((n) => !Number.isFinite(n))) return null
  const out: IndicatorParamValues = {}
  spec.params.forEach((p, i) => { out[p.key] = p.type === 'int' ? Math.round(parts[i]) : parts[i] })
  return out
}
/** 一次性迁移：sr-ind-manual:v2 为空时读取旧 sr-tuned-* 历史记忆，写入手动覆盖后删除旧键 */
export const migrateLegacyManual = (specs: Record<string, IndicatorSpec>): Record<string, IndicatorParamValues> | null => {
  const found: Record<string, IndicatorParamValues> = {}
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)
    if (!key?.startsWith('sr-tuned-')) continue
    const ind = key.slice('sr-tuned-'.length)
    const spec = specs[ind]
    const raw = localStorage.getItem(key)
    if (!spec || !raw) continue
    const named = legacyStrToNamed(spec, raw)
    if (named) found[ind] = named
  }
  if (Object.keys(found).length === 0) return null
  return found
}

/** 垂直分割比例（K线 / 下方分析，单位 %）：拖拽结束后持久化；默认 65/35
 *  （T-80:55/45 下 K 线主图仅 ~113px,多副图时被 pane 拉伸因子压得更小,用户反馈崩坏级难读）
 *  SPLIT_DEFAULT 同时是 Splitter pane defaultSize（双击 handle 重置目标，见 StockWorkbench） */
const SPLIT_KEY = 'sr-wb-split'
export const SPLIT_DEFAULT: [number, number] = [65, 35]
/** 垂直 Splitter pane 约束（%）：K线 [28,82] / 分析 [20,62] → left 有效 [38,80] */
const V_SPLIT_CONSTRAINTS: SplitConstraints = { min: 28, max: 82, min2: 20, max2: 62 }
export const loadSplit = (): number[] => loadSplitSizes(SPLIT_KEY, V_SPLIT_CONSTRAINTS, SPLIT_DEFAULT)
export const persistSplit = (sizes: number[]) => persistSplitSizes(SPLIT_KEY, sizes)

/** 水平分割比例（信号时间线 / K线主区，单位 %）：桌面端拖拽结束后持久化；
 *  HSPLIT_DEFAULT 同时是 Splitter pane defaultSize（双击 handle 重置目标） */
const HSPLIT_KEY = 'sr-wb-hsplit'
export const HSPLIT_DEFAULT: [number, number] = [30, 70]
/** 水平 Splitter pane 约束（%）：时间线 [20,45] / K线主区 [40,∞) → left 有效 [20,45] */
const H_SPLIT_CONSTRAINTS: SplitConstraints = { min: 20, max: 45, min2: 40 }
export const loadHSplit = (): number[] => loadSplitSizes(HSPLIT_KEY, H_SPLIT_CONSTRAINTS, HSPLIT_DEFAULT)
export const persistHSplit = (sizes: number[]) => persistSplitSizes(HSPLIT_KEY, sizes)

/** 调优应用范围（本图表/全部股票）：会话偏好，持久化跨会话保持。 */
export type TuneScope = 'chart' | 'global'
// 持久化键统一 sr- 前缀（旧键缺前缀：读时兼容迁移，写入一律新键）
const TUNE_SCOPE_KEY = 'sr-wb-tune-scope'
const TUNE_SCOPE_KEY_LEGACY = 'wb:tune-scope'
export const loadTuneScope = (): TuneScope => {
  try {
    const v = localStorage.getItem(TUNE_SCOPE_KEY)
    if (v === 'chart' || v === 'global') return v
    // 旧键兼容：读到旧键有效值 → 迁移写入新键并清除旧键（下次读取直取新键）
    const legacy = localStorage.getItem(TUNE_SCOPE_KEY_LEGACY)
    if (legacy === 'chart' || legacy === 'global') {
      localStorage.setItem(TUNE_SCOPE_KEY, legacy)
      localStorage.removeItem(TUNE_SCOPE_KEY_LEGACY)
      return legacy
    }
  } catch { /* localStorage 不可用/损坏时回退默认 */ }
  return 'chart'
}
export const persistTuneScope = (s: TuneScope) => {
  try { localStorage.setItem(TUNE_SCOPE_KEY, s) } catch { /* ignore */ }
}
