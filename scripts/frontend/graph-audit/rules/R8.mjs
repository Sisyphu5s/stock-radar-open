/**
 * R8 路由闭合:① Route 的懒加载组件引用必须在 src 存在文件(悬空即 FAIL);
 * ② 有页面组件的路由必须被 layouts/menu.tsx 菜单项覆盖(前缀匹配;重定向路由无需菜单)。
 */
import fs from 'node:fs'
import path from 'node:path'

export const name = '路由闭合'

const inWl = (wl, item) => wl.has(item) || [...wl].some((w) => item.startsWith(w))

export function run(graph, contract, whitelist) {
  const wl = new Set(whitelist.r8_routes || [])
  const issues = []
  const menuPaths = (graph.menu || []).map((m) => m.path)
  const menuCovered = (p) => menuPaths.some((k) => p === k || p.startsWith(k + '/'))
  let total = 0

  for (const r of graph.routes || []) {
    total++
    /* ① 组件文件存在性:component 非空但 file 空(懒加载解析失败)或文件缺失 → 悬空 */
    if (r.component) {
      const missing = !r.file || !fs.existsSync(path.join(graph.root, 'src', r.file))
      if (missing) {
        const loc = `App.tsx:${r.line}`
        if (!inWl(wl, loc)) issues.push({ status: 'FAIL', loc, detail: `路由 ${r.path} 的组件 ${r.component} 悬空(懒加载文件 ${r.file || '未解析'})` })
      }
    }
    /* ② 菜单覆盖:仅要求有页面组件、非重定向/通配的路由(component 非空);
     *    带 :param 的路由(详情/个股等)非导航入口,菜单不覆盖是正常形态 */
    if (r.component && r.path.startsWith('/') && !r.path.includes(':') && !menuCovered(r.path)) {
      const loc = `App.tsx:${r.line}`
      if (!inWl(wl, loc)) issues.push({ status: 'FAIL', loc, detail: `路由 ${r.path}(${r.component})无菜单入口(menu.tsx 未覆盖)` })
    }
  }

  const fail = issues.filter((i) => i.status === 'FAIL').length
  return {
    name,
    summary: `路由 ${total} 条(菜单 ${menuPaths.length} 项),悬空/未覆盖 FAIL ${fail} 处`,
    counts: { fail, total },
    issues,
  }
}
