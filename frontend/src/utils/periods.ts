/**
 * 周期常量统一单一事实源（SignalCenter / PaperTrading / Workbench 共用）。
 * 历史上 PERIOD_OPTIONS/PERIODS 有 4 份独立定义，label 曾分歧「日/周/月」vs「日K/周K/月K」；
 * 本轮统一为仅此一处定义，label 统一「日/周/月」（不带 K 后缀，Segmented 紧凑场景兼容）。
 */

export interface PeriodOption { label: string; value: string }

/** 统一周期选项：分钟「N分」、日/周/月不带 K 后缀（与 signalCenter/context.ts 原定义完全一致） */
export const PERIOD_OPTIONS: PeriodOption[] = [
  { label: '1分', value: '1' },
  { label: '5分', value: '5' },
  { label: '15分', value: '15' },
  { label: '30分', value: '30' },
  { label: '60分', value: '60' },
  { label: '日', value: 'daily' },
  { label: '周', value: 'weekly' },
  { label: '月', value: 'monthly' },
]

/** 周期值 → 中文标签（未命中时原样返回） */
export const periodLabel = (p: string): string =>
  PERIOD_OPTIONS.find((o) => o.value === p)?.label ?? p

/** 分钟周期（1/5/15/30/60）判定：true 时信号时点显示「MM-DD HH:mm」 */
export const isMinutePeriod = (p: string): boolean =>
  p !== 'daily' && p !== 'weekly' && p !== 'monthly'

/** workbench K 线周期（k/l 结构，从 workbench/context.ts 迁移） */
export const PERIODS = [
  { k: '1', l: '1分' }, { k: '5', l: '5分' }, { k: '15', l: '15分' },
  { k: '30', l: '30分' }, { k: '60', l: '60分' },
  { k: 'daily', l: '日' }, { k: 'weekly', l: '周' }, { k: 'monthly', l: '月' },
]
