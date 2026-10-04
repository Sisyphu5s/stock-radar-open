import type { RiskGradeInfo, WorkbenchRisk } from '../context'

/** 数值清洗：null/undefined/空串/NaN/Infinity/无法解析的串 → null（展示层统一回退 '—'） */
export const toFinite = (v: unknown): number | null => {
  if (v == null || v === '') return null
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : null
}

/** 环比变化率：(cur - prev) / |prev|；任一缺失或 prev 为 0 → null（展示层回退无标注） */
export const pctDelta = (cur: unknown, prev: unknown): number | null => {
  const num = toFinite(cur)
  const prevNum = toFinite(prev)
  return num != null && prevNum != null && prevNum !== 0 ? (num - prevNum) / Math.abs(prevNum) : null
}

/**
 * 风险综合评级：波动（EWMA 优先）/ 最大回撤 / 尾部风险 / VaR（CF 修正优先）四维打分（0-8）。
 * 唯一实现（risk.tsx 与「综合」tab 共用；K 线叠加契约 riskSummary.grade 也由此派生）。
 * 回撤/VaR 后端为负值（-0.25 = 回撤 25%），按绝对值判定：数值越大风险越高。
 */
export const computeRiskGrade = (risk: WorkbenchRisk | null | undefined): RiskGradeInfo | null => {
  if (!risk) return null
  const vol = toFinite(risk.ewma_volatility) ?? toFinite(risk.annual_volatility)
  const mdd = toFinite(risk.max_drawdown)
  const tail = toFinite(risk.tail_risk)
  const var95 = toFinite(risk.var95_cf) ?? toFinite(risk.var95)
  if (vol == null || mdd == null) return null
  const dims = [
    { l: '波动', s: vol < 0.15 ? 0 : vol < 0.3 ? 1 : 2 },
    { l: '回撤', s: Math.abs(mdd) < 0.15 ? 0 : Math.abs(mdd) < 0.3 ? 1 : 2 },
    { l: '尾部', s: tail == null || tail <= 1.3 ? 0 : tail <= 1.6 ? 1 : 2 },
    { l: 'VaR', s: var95 == null || Math.abs(var95) < 0.03 ? 0 : Math.abs(var95) < 0.06 ? 1 : 2 },
  ]
  const score = dims.reduce((a, d) => a + d.s, 0)
  if (score <= 2) return { tag: '低风险', color: 'green', desc: '波动与回撤可控，尾部风险正常', dims }
  if (score <= 5) return { tag: '中风险', color: 'orange', desc: '波动或回撤偏大，注意仓位管理', dims }
  return { tag: '高风险', color: 'red', desc: '波动/回撤显著，建议控制仓位并设止损', dims }
}
