// 工作台下方分析各 section（拆分自原 sections.tsx，按职责一个 section 一个文件）：
// - sections/tune.tsx        指标调优（TuneSection + 参数编辑器 + 目标分组/展示串工具）
// - sections/fundamental.tsx 基本面 · 估值
// - sections/financial.tsx   财务摘要（最近两期）
// - sections/capital.tsx     资金（资金流/龙虎榜/两融/北向持股，T-08）
// - sections/risk.tsx        风险指标（含收益分布直方图，T-66 置顶为第一项）
// - sections/news.tsx        新闻动态
// - sections/shared.ts       数值清洗工具（toFinite/pctDelta/computeRiskGrade）
// 此处 re-export 保持既有消费方 import 路径（'./sections'）不变。
export { TuneSection } from './sections/tune'
export { FundamentalSection } from './sections/fundamental'
export { FinancialSection } from './sections/financial'
export { CapitalSection } from './sections/capital'
export { RiskSection } from './sections/risk'
export { NewsSection } from './sections/news'
