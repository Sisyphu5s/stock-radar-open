/**
 * R7 孤儿组件:components 中 usedIn(JSX 使用点)为空且全 src 文本粗筛无引用的组件 → WARN。
 * 排除 src/components/ui/** 基板(契约声明/供模板组合)与 templates/、layouts/ 骨架。
 * 文本粗筛:组件名在其他文件以任何形式出现(注册表字符串引用/类型引用/动态渲染)→ 非孤儿。
 */
import fs from 'node:fs'
import path from 'node:path'

export const name = '孤儿组件'

const inWl = (wl, item) => wl.has(item) || [...wl].some((w) => item.startsWith(w))
const EXCLUDE = ['components/ui/', 'templates/', 'layouts/']

/** 全 src 源码文本(粗筛用,一次读入;排除组件自身文件) */
function srcTexts(graph) {
  const root = path.join(graph.root, 'src')
  const out = new Map()
  const walk = (dir) => {
    if (!fs.existsSync(dir)) return
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name)
      if (e.isDirectory()) walk(p)
      else if (/\.(ts|tsx)$/.test(e.name)) {
        const rel = path.relative(root, p).split(path.sep).join('/')
        out.set(rel, fs.readFileSync(p, 'utf8'))
      }
    }
  }
  walk(root)
  return out
}

export function run(graph, contract, whitelist) {
  const wl = new Set(whitelist.r7_orphans || [])
  const candidates = (graph.orphans || []).filter((o) => !EXCLUDE.some((p) => o.file.startsWith(p)))
  const texts = srcTexts(graph)
  const orphans = candidates.filter((o) => {
    /* 其他文件文本出现组件裸名(注册表/字符串/类型引用)→ 排除 */
    for (const [rel, text] of texts) {
      if (rel === o.file) continue
      const re = new RegExp(`\\b${o.name}\\b`)
      if (re.test(text)) return false
    }
    return true
  })
  const issues = orphans
    .filter((o) => !inWl(wl, `${o.file}:${o.line}`))
    .map((o) => ({ status: 'WARN', loc: `${o.file}:${o.line}`, detail: `组件 ${o.name} 零 JSX 使用点且全 src 无文本引用(孤儿)` }))
  return {
    name,
    summary: `孤儿组件 ${candidates.length} 处(排除 ui/基板/templates/layouts;文本粗筛后 ${orphans.length} 处),差集 ${issues.length} 处(WARN)`,
    counts: { fail: 0, total: orphans.length, warn: issues.length },
    issues,
  }
}
