/**
 * R6 轮询纪律:setInterval 轮询必须带可见性保护(document.visibilityState 检查,同 hook/同文件
 * 存在即视为已检查;不可见时停表,防后台空转)——visibility=false → FAIL。
 */
export const name = '轮询纪律'

const inWl = (wl, item) => wl.has(item) || [...wl].some((w) => item.startsWith(w))

export function run(graph, contract, whitelist) {
  const wl = new Set(whitelist.r6_poll || [])
  const polls = (graph.effects || []).filter((e) => e.kind === 'setInterval')
  const bad = polls.filter((e) => !e.visibility)
  const issues = bad
    .filter((e) => !inWl(wl, `${e.file}:${e.line}`))
    .map((e) => ({ status: 'FAIL', loc: `${e.file}:${e.line}`, detail: 'setInterval 轮询无 visibilityState 可见性保护(后台空转)' }))
  const fail = issues.length
  return {
    name,
    summary: `setInterval 轮询 ${polls.length} 处,无可见性保护 ${bad.length} 处,差集 ${fail} 处`,
    counts: { fail, total: polls.length },
    issues,
  }
}
