/**
 * R5 渲染路径纯度:JSX 表达式位置(属性值/子元素)内调用 getBoundingClientRect / JSON.stringify
 * 属渲染期副作用/不稳定输出 → WARN(渲染路径应只读状态,测量与序列化交给 effect/事件)。
 */
export const name = '渲染路径纯度'

const inWl = (wl, item) => wl.has(item) || [...wl].some((w) => item.startsWith(w))

export function run(graph, contract, whitelist) {
  const wl = new Set(whitelist.r5_impure || [])
  const all = graph.renderImpure || []
  const issues = all
    .filter((r) => !inWl(wl, `${r.file}:${r.line}`))
    .map((r) => ({ status: 'WARN', loc: `${r.file}:${r.line}`, detail: `JSX 表达式内调用 ${r.api}(渲染期副作用)` }))
  return {
    name,
    summary: `渲染路径不纯调用 ${all.length} 处(白名单 ${wl.size}),差集 ${issues.length} 处(WARN)`,
    counts: { fail: 0, total: all.length, warn: issues.length },
    issues,
  }
}
