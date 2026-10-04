/**
 * K 线图副图指标配置层（纯数据，无 React / 无图表实例依赖）。
 *
 * 供 lightweight-charts v5 重构后的副图 pane 系统使用；数据与逻辑自
 * components/KlineChart.tsx 迁移，契约如下：
 *
 * - SUB_PANE_KIND[key] 决定每指标 pane 的主渲染类型（'line' | 'histogram'）：
 *   现有 KlineChart.buildSub 中仅 macd 的 macd_hist 用 bar 渲染，其余指标全为 line。
 * - 颜色约定：固定色（金/蓝/紫）引用 chartTheme.ts 导出的 SUB_LINE_* 常量
 *   （P1-46 收敛，禁止重复字面量；值不变）；涨跌语义用 CSS 变量
 *   'var(--sr-up)' / 'var(--sr-down)'；暗色提亮紫存亮色原始值（SUB_LINE_PURPLE，
 *   暗色对应 '#a78bfa'），isDark 的最终转换由 KlineChart 侧 resolveChartColor 负责。
 */

import { SUB_LINE_BLUE, SUB_LINE_GOLD, SUB_LINE_PURPLE } from './chartTheme'

/** 副图指标 key（与 KlineChart SUB_KEYS 一致，17 个） */
export const SUB_KEYS: string[] = [
  'macd', 'kdj', 'rsi', 'wr', 'dmi', 'cci', 'roc', 'mtm', 'obv',
  'bias', 'psy', 'trix', 'vr', 'atr', 'cmo', 'dpo', 'emv',
]

/** 副图 pane 渲染类型：折线 / 柱状 */
export type SubPaneKind = 'line' | 'histogram'

/** 每指标 → 渲染类型（仅 macd 含柱状系列 macd_hist，其余全 line） */
export const SUB_PANE_KIND: Record<string, SubPaneKind> = {
  macd: 'histogram',
  kdj: 'line', rsi: 'line', wr: 'line', dmi: 'line', cci: 'line', roc: 'line',
  mtm: 'line', obv: 'line', bias: 'line', psy: 'line', trix: 'line', vr: 'line',
  atr: 'line', cmo: 'line', dpo: 'line', emv: 'line',
}

/** 副图阈值线（yAxis 级细虚线，不占用图例；与 KlineChart SUB_THRESHOLDS 一致） */
export const SUB_THRESHOLDS: Record<string, number[]> = {
  macd: [0], rsi: [30, 70], wr: [20, 80], kdj: [20, 80], psy: [25, 75],
  cci: [-100, 100], bias: [-10, 10],
}

/** 副图标注配置：标题 + 各项 label/字段/颜色（与 KlineChart SUB_ANNOT 一致） */
export const SUB_ANNOT: Record<string, { title: string; items: { label: string; field: string; color: string }[] }> = {
  macd: {
    title: 'MACD',
    items: [
      { label: 'DIF:', field: 'macd_dif', color: SUB_LINE_GOLD },
      { label: 'DEA:', field: 'macd_dea', color: SUB_LINE_BLUE },
      { label: 'MACD:', field: 'macd_hist', color: SUB_LINE_PURPLE }, // 暗色 #a78bfa
    ],
  },
  kdj: {
    title: 'KDJ',
    items: [
      { label: 'K:', field: 'kdj_k', color: SUB_LINE_GOLD },
      { label: 'D:', field: 'kdj_d', color: SUB_LINE_BLUE },
      { label: 'J:', field: 'kdj_j', color: SUB_LINE_PURPLE }, // 暗色 #a78bfa
    ],
  },
  rsi: {
    title: 'RSI',
    items: [
      { label: 'RSI6:', field: 'rsi6', color: SUB_LINE_GOLD },
      { label: 'RSI12:', field: 'rsi12', color: SUB_LINE_BLUE },
      { label: 'RSI24:', field: 'rsi24', color: SUB_LINE_PURPLE }, // 暗色 #a78bfa
    ],
  },
  wr: {
    title: 'WR',
    items: [
      { label: 'WR10:', field: 'wr10', color: SUB_LINE_GOLD },
      { label: 'WR6:', field: 'wr6', color: SUB_LINE_BLUE },
    ],
  },
  dmi: {
    title: 'DMI(14,6)',
    items: [
      { label: 'PDI:', field: 'dmi_pdi', color: 'var(--sr-up)' },
      { label: 'MDI:', field: 'dmi_mdi', color: 'var(--sr-down)' },
      { label: 'ADX:', field: 'dmi_adx', color: SUB_LINE_GOLD },
      { label: 'ADXR:', field: 'dmi_adxr', color: SUB_LINE_PURPLE }, // 暗色 #a78bfa
    ],
  },
  cci: { title: 'CCI', items: [{ label: 'CCI:', field: 'cci', color: SUB_LINE_GOLD }] },
  roc: { title: 'ROC', items: [{ label: 'ROC:', field: 'roc', color: SUB_LINE_GOLD }] },
  mtm: {
    title: 'MTM',
    items: [
      { label: 'MTM:', field: 'mtm', color: SUB_LINE_GOLD },
      { label: 'MTMMA:', field: 'mtmma', color: SUB_LINE_BLUE },
    ],
  },
  obv: { title: 'OBV', items: [{ label: 'OBV:', field: 'obv', color: SUB_LINE_GOLD }] },
  bias: {
    title: 'BIAS',
    items: [
      { label: 'BIAS6:', field: 'bias6', color: SUB_LINE_GOLD },
      { label: 'BIAS12:', field: 'bias12', color: SUB_LINE_BLUE },
      { label: 'BIAS24:', field: 'bias24', color: SUB_LINE_PURPLE }, // 暗色 #a78bfa
    ],
  },
  psy: { title: 'PSY', items: [{ label: 'PSY:', field: 'psy', color: SUB_LINE_GOLD }] },
  trix: {
    title: 'TRIX',
    items: [
      { label: 'TRIX:', field: 'trix', color: SUB_LINE_GOLD },
      { label: 'MATRIX:', field: 'matrix', color: SUB_LINE_BLUE },
    ],
  },
  vr: { title: 'VR(26)', items: [{ label: 'VR:', field: 'vr', color: SUB_LINE_GOLD }] },
  atr: {
    title: 'ATR(14)',
    items: [
      { label: 'ATR:', field: 'atr', color: SUB_LINE_GOLD },
      { label: 'TR:', field: 'tr', color: SUB_LINE_BLUE },
    ],
  },
  cmo: { title: 'CMO', items: [{ label: 'CMO:', field: 'cmo', color: SUB_LINE_GOLD }] },
  dpo: { title: 'DPO(20)', items: [{ label: 'DPO:', field: 'dpo', color: SUB_LINE_GOLD }] },
  emv: {
    title: 'EMV(14,9)',
    items: [
      { label: 'EMV:', field: 'emv', color: SUB_LINE_GOLD },
      { label: 'EMVMA:', field: 'emvma', color: SUB_LINE_BLUE },
    ],
  },
}

/** 具名参数值对象（manual > backend global > default 合并后的实际生效参数） */
export type EffectiveParams = Record<string, Record<string, number>>

/** 具名参数 spec 顺序（与 backend SPECS 一致）：用于把生效参数拼进指标标题/图例 */
export const SPEC_ORDER: Record<string, string[]> = {
  ma: ['window'], ema: ['window'], rsi: ['window'], macd: ['fast', 'slow', 'signal'],
  kdj: ['window'], boll: ['window', 'multiplier'], wr: ['window'], cci: ['window'],
  roc: ['window'], mtm: ['window'], bias: ['window'], psy: ['window'], trix: ['window'],
  cmo: ['window'], volume_ratio: ['window'],
}

/** 生效具名参数 → 紧凑参数串：{fast:12,slow:26,signal:9} → "12,26,9"；缺失任一/非法值返回 '' */
export const fmtEff = (ep: Record<string, number> | undefined, order: string[] | undefined): string => {
  if (!ep || !order) return ''
  const vals = order.map((k) => ep[k]).filter((v): v is number => v != null && Number.isFinite(Number(v)))
  if (vals.length !== order.length) return ''
  return vals.join(',')
}

/** 副图标题（含生效参数串）：MACD → "MACD(12,26,9)"；无配置 key 回退大写 key */
export function subTitle(key: string, effectiveParams?: Record<string, Record<string, number>>): string {
  const cfg = SUB_ANNOT[key]
  const base = cfg?.title ?? key.toUpperCase()
  const eff = fmtEff(effectiveParams?.[key], SPEC_ORDER[key])
  return eff ? `${base}(${eff})` : base
}

/** 字段最新值 → 两位小数；无值/NaN → '—' */
export function subLatest(field: string, indicators?: Record<string, number[]>): string {
  const v = indicators?.[field]?.slice(-1)[0]
  return v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(2)
}

/** 副图数据可用性：字段数组存在且至少一个非 null 非 NaN 数值（无配置 key 直接查自身字段） */
export function subHasData(key: string, indicators?: Record<string, number[]>): boolean {
  if (!indicators) return false
  const cfg = SUB_ANNOT[key]
  const fields = cfg ? cfg.items.map((it) => it.field) : [key]
  return fields.some((f) => {
    const arr = indicators[f]
    return Array.isArray(arr) && arr.length > 0 && arr.some((x) => x != null && !Number.isNaN(Number(x)))
  })
}
