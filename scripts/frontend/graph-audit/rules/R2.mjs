/**
 * R2 时间字段流:后端 naive ISO 时间字段(triggered_at/as_of/updated_at/quote_time 等)的展示
 * 必须流经 utils/time.ts 的 resolveSignalMoment / formatSignalTime(单一事实源)。
 * timeFields.guarded=false(所在最内层函数未调用时间函数)→ FAIL。
 */
export const name = '时间字段流'

/** 白名单条目: `file:line`(点位精确,行号漂移敏感)或 `file::field`(语义锚点,漂移免疫) */
const exempt = (wl, t) => wl.has(`${t.file}:${t.line}`) || wl.has(`${t.file}::${t.field}`)

export function run(graph, contract, whitelist) {
  const wl = new Set(whitelist.r2_time || [])
  /* 同点同字段多变量访问合并(file:line:field 去重) */
  const seen = new Set()
  const bad = []
  for (const t of graph.timeFields || []) {
    if (t.guarded) continue
    const key = `${t.file}:${t.line}:${t.field}`
    if (seen.has(key)) continue
    seen.add(key)
    bad.push(t)
  }
  const issues = bad
    .filter((t) => !exempt(wl, t))
    .map((t) => ({
      status: 'FAIL',
      loc: `${t.file}:${t.line}`,
      detail: `时间字段「${t.field}」(${t.owner ? `owner: ${t.owner}` : 'owner 解析失败'})未经 formatSignalTime/resolveSignalMoment 处理`,
    }))
  const fail = issues.length
  return {
    name,
    summary: `未守卫时间字段 ${bad.length} 处(白名单 ${wl.size}),差集 ${fail} 处`,
    counts: { fail, total: bad.length },
    issues,
  }
}
