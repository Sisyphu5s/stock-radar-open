import type { IndicatorParamValues, IndicatorSpec } from '../../../../../api/client'

/** 调优目标分组：理论（IC 系）/ 实操（多空系）/ 综合 */
export const TARGET_GROUPS: { name: string; opts: { value: string; label: string }[] }[] = [
  { name: '理论', opts: [
    { value: 'ic', label: 'IC' }, { value: 'ic_abs', label: '|IC|' }, { value: 'icir', label: 'ICIR' },
    { value: 'rank_ic', label: 'RankIC' }, { value: 'stability', label: '稳定性' },
  ]},
  { name: '实操', opts: [
    { value: 'ls_annual', label: '多空年化' }, { value: 'sharpe', label: '夏普' },
    { value: 'max_drawdown', label: '最大回撤' }, { value: 'win_rate', label: '胜率' },
  ]},
  { name: '综合', opts: [{ value: 'composite', label: '综合' }] },
]

/** 目标 Select 的 Mantine 分组 data（{group, items} 结构；Select data prop 直接消费） */
export const TARGET_GROUPS_DATA: { group: string; items: { value: string; label: string }[] }[] =
  TARGET_GROUPS.map((g) => ({ group: g.name, items: g.opts }))

/** 优化算法：grid 全量网格 / random 随机采样（固定种子可复现）/ fast 快速采样（后端未支持时默认 grid 兼容） */
export const ALGO_OPTIONS: { value: 'grid' | 'random' | 'fast'; label: string }[] = [
  { value: 'grid', label: '网格搜索（全量）' },
  { value: 'random', label: '随机采样（可复现）' },
  { value: 'fast', label: '快速采样' },
]

/** 算法显示名（结果区标注用） */
export const ALGO_LABEL: Record<string, string> = {
  grid: '网格搜索', random: '随机采样', fast: '快速采样',
}

/**
 * 参数悬浮说明：优先匹配「指标:参数」特例，回退通用参数语义。
 * 只写与指标语义相关的说明，避免编造具体计算公式。
 */
export const PARAM_HINTS: Record<string, string> = {
  // 通用参数语义
  window: '计算窗口周期（bar 数）：越大曲线越平滑、反应越滞后',
  fast: '快线周期：越小对价格变化越敏感',
  slow: '慢线周期：越大趋势越稳定、越滞后',
  signal: '信号线（DEA）周期：金叉/死叉判定的均线窗口',
  multiplier: '标准差倍数：越大轨道越宽、上下轨越远',
  // 指标特例（SPECS 可调优指标全集：ma/ema/rsi/macd/kdj/boll/wr/cci/roc/mtm/bias/psy/trix/cmo/volume_ratio；
  // 其余副图指标不可调优，不在此表——通用 window 兜底即可）
  'rsi:window': 'RSI 回溯周期：越小对短期涨跌越敏感（经典 6/12/24）',
  'macd:fast': 'DIF 快线 EMA 周期（经典 12）',
  'macd:slow': 'DIF 慢线 EMA 周期（经典 26）',
  'macd:signal': 'DEA 信号线周期（经典 9）',
  'kdj:window': 'RSV 平滑窗口：K/D 值计算的平滑周期',
  'boll:window': '布林带中轨均线周期（经典 20）',
  'bias:window': '乖离率周期：衡量价格偏离均线的程度',
  'psy:window': '心理线周期：统计上涨天数占比',
  'wr:window': '威廉指标周期：超买超卖判断',
  'cci:window': 'CCI 周期：价格偏离统计均值的程度',
  'roc:window': '变动率周期：价格涨跌速度',
  'mtm:window': '动量周期：价格变动动能',
  'trix:window': 'TRIX 周期：三重平滑的趋势指标',
  'cmo:window': 'CMO 周期：钱德动量摆动',
  'volume_ratio:window': '量比周期：当日量能相对历史水平',
  'ma:window': '均线周期：收盘价 N 日均值',
  'ema:window': 'EMA 周期：指数加权均线（近值权重更高）',
}

/** 参数步长 → 小数位数（NumberInput decimalScale 用；0.5 → 1，2.0 → 1） */
export const stepPrecision = (s: number): number => {
  const str = String(s)
  const i = str.indexOf('.')
  return i < 0 ? 0 : str.length - i - 1
}

/** 具名参数 → 带标签展示串：{fast:12,slow:26,signal:9} → "快线 EMA12 慢线 EMA26 DEA 信号9" */
export const fmtNamed = (spec: IndicatorSpec | undefined, params: IndicatorParamValues | undefined): string => {
  if (!spec?.params || !params) return '—'
  return spec.params.map((p) => {
    const v = params[p.key]
    if (v == null || !Number.isFinite(Number(v))) return null
    const num = p.type === 'int' ? Math.round(Number(v)) : Number(v)
    return `${p.label}${num}`
  }).filter(Boolean).join(' ')
}
