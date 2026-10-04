/**
 * R4 memo 契约:memo(...) 包裹的组件(memo 隔离重渲染的前提是 props 引用稳定),
 * JSX 使用点 props 传对象字面量/箭头函数/数组字面量(inline)会每次渲染生成新引用,
 * 击穿 memo 缓存 → FAIL。
 */
export const name = 'memo 契约'

/** 白名单条目: `file:line`(点位精确,行号漂移敏感)或 `file::component::prop`(语义锚点,漂移免疫) */
const exempt = (wl, m) => wl.has(`${m.file}:${m.line}`) || wl.has(`${m.file}::${m.component}::${m.prop}`)

export function run(graph, contract, whitelist) {
  const wl = new Set(whitelist.r4_memo || [])
  const all = (graph.memoProps || []).filter((m) => m.inline)
  const issues = all
    .filter((m) => !exempt(wl, m))
    .map((m) => ({ status: 'FAIL', loc: `${m.file}:${m.line}`, detail: `memo 组件 ${m.component} 的 prop「${m.prop}」传内联字面量(引用不稳定,击穿 memo)` }))
  const fail = issues.length
  return {
    name,
    summary: `memo 组件内联 props ${all.length} 处(白名单 ${wl.size}),差集 ${fail} 处`,
    counts: { fail, total: all.length },
    issues,
  }
}
