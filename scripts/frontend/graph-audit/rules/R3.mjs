/**
 * R3 生命周期平衡:setInterval/EventSource/addEventListener(含 visibilitychange)在 useEffect
 * 回调或 hook 函数内必须有对应清理(clearInterval / close / removeEventListener;EventSource
 * 实例 listener 由实例 close() 清理视为已清理,见 build.mjs)。
 * 注意:visibilitychange 不豁免——普通函数(如 waitForJob)每次调用注册的全局监听同样泄漏,
 * 必须以 removeEventListener 配对(项目历史 bug:client.ts waitForJob 累积监听器)。
 */
export const name = '生命周期平衡'

const inWl = (wl, item) => wl.has(item) || [...wl].some((w) => item.startsWith(w))

export function run(graph, contract, whitelist) {
  const wl = new Set(whitelist.r3_effects || [])
  const all = graph.effects || []
  const bad = all.filter((e) => !e.cleanup)
  const issues = bad
    .filter((e) => !inWl(wl, `${e.file}:${e.line}`))
    .map((e) => ({ status: 'FAIL', loc: `${e.file}:${e.line}`, detail: `${e.kind} 无对应清理(clearInterval/close/removeEventListener)` }))
  const fail = issues.length
  return {
    name,
    summary: `效果点 ${all.length} 处,无清理 ${bad.length} 处,差集 ${fail} 处`,
    counts: { fail, total: all.length },
    issues,
  }
}
