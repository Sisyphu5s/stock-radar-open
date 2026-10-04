/**
 * 全局参数 → 表达式应用（T-43：自 FactorEvaluation 页面迁出为纯函数）。
 * ------------------------------------------------------------
 * 简单规则：按「算子.参数[出现序号]」在表达式中定位算子调用，替换其末尾数字参数。
 * 纯函数、无 React 依赖，供评估页「应用全局参数」与后续调优 UI 共用。
 */
export function applyGlobalParam(
  expr: string,
  name: string,
  rawValue: string | number,
): { ok: boolean; expr: string } {
  const m = /^([A-Za-z_]\w*)\.([A-Za-z_]\w*)(?:\[(\d+)\])?$/.exec(name)
  if (!m) return { ok: false, expr }
  const op = m[1]
  const wantIdx = m[3] !== undefined ? Number(m[3]) : 0
  const value = String(rawValue)
  const re = new RegExp(`${op}\\s*\\(`, 'gi')
  const starts: number[] = []
  let mm: RegExpExecArray | null
  while ((mm = re.exec(expr)) !== null) starts.push(mm.index + mm[0].length - 1)
  if (wantIdx >= starts.length) return { ok: false, expr }
  const openAt = starts[wantIdx]
  let depth = 0
  let closeAt = -1
  for (let i = openAt; i < expr.length; i++) {
    const c = expr[i]
    if (c === '(') depth++
    else if (c === ')') {
      depth--
      if (depth === 0) { closeAt = i; break }
    }
  }
  if (closeAt < 0) return { ok: false, expr }
  const span = expr.slice(openAt, closeAt + 1)
  const numRe = /\d+(?:\.\d+)?/g
  let last: RegExpExecArray | null = null
  let n: RegExpExecArray | null
  while ((n = numRe.exec(span)) !== null) last = n
  if (!last) return { ok: false, expr }
  const numAbs = openAt + last.index
  return { ok: true, expr: expr.slice(0, numAbs) + value + expr.slice(numAbs + last[0].length) }
}
