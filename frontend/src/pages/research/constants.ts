/**
 * 研究域共享常量：因子表达式算子 / 特征字段清单（T-18 表达式 IDE 补全与高亮的数据源）。
 * 事实源：backend/app/lib/alpha/operators.py 的 OPERATORS 注册表 + FEATURES
 * （@_op 装饰器注册的 29 个算子；13 个特征列：7 个 OHLCV 行情 + 6 个估值 + 行业）。
 * 前端不 import 后端，落一份只读镜像，后端新增算子时需同步此处（唯一手动同步点）。
 */

/** 全量算子（顺序即补全展示顺序：常用一元/时序靠前，算术与低频算子随后） */
export const RESEARCH_OPERATORS = [
  'rank',
  'log',
  'abs',
  'sign',
  'neg',
  'delta',
  'ts_mean',
  'ts_std',
  'ts_rank',
  'ts_sum',
  'ts_min',
  'ts_max',
  'ts_delay',
  'ts_corr',
  'add',
  'sub',
  'mul',
  'div',
  'max2',
  'min2',
  'ts_skew',
  'ts_kurt',
  'ts_count',
  'ts_argmax',
  'ts_argmin',
  'ts_product',
  'decay_linear',
  'winsorize',
  'scale',
  'signed_power',
] as const

/** 特征字段（叶子列名） */
export const RESEARCH_FEATURES = [
  'open',
  'high',
  'low',
  'close',
  'volume',
  'amount',
  'pct_change',
  'pe',
  'pb',
  'ps',
  'market_cap',
  'float_cap',
  'turnover',
  'industry',
] as const
