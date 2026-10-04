#!/usr/bin/env node
/**
 * 图审计编排器:逐条运行 rules/R1..R8.mjs,emit 风格与 scripts/design-system-audit.mjs 一致
 * ([FAIL]/[WARN] + 证据缩进,FAIL 累计,结尾 Score 汇总,FAIL>0 退出码 1)。
 *
 * 规则只读图(graph.json 由 build.mjs 构建),不做文件级启发式;白名单
 * scripts/frontend/graph-audit/graph-whitelist.json 承载存量豁免(差集非空即 FAIL——只拦新增)。
 *
 * 用法:
 *   node scripts/frontend/graph-audit/graph-audit.mjs [--contract <path>] [--graph <path>]
 *     --contract  契约文件路径(默认 ../../backend/contract.json,相对本目录)
 *     --graph     读取已有 graph.json(--out 产物)代替现场构建
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildGraph } from './build.mjs'

const DIR = path.dirname(fileURLToPath(import.meta.url))

/* ---------- 参数 ---------- */
const argIdx = (name) => process.argv.indexOf(name)
const contractPath = argIdx('--contract') >= 0
  ? path.resolve(DIR, process.argv[argIdx('--contract') + 1])
  : path.resolve(DIR, '../../backend/contract.json')
const graphPath = argIdx('--graph') >= 0 ? path.resolve(DIR, process.argv[argIdx('--graph') + 1]) : null
const whitelistPath = path.join(DIR, 'graph-whitelist.json')

/* ---------- 图:现场构建或读文件 ---------- */
let graph
if (graphPath && fs.existsSync(graphPath)) {
  graph = JSON.parse(fs.readFileSync(graphPath, 'utf8'))
} else {
  graph = buildGraph()
}

/* ---------- 契约 ---------- */
let contract = null
let contractNote = null
if (fs.existsSync(contractPath)) {
  try {
    contract = JSON.parse(fs.readFileSync(contractPath, 'utf8'))
  } catch (e) {
    contractNote = `contract 解析失败: ${e.message}`
  }
} else {
  contractNote = `contract 文件不存在(${contractPath}),R1 将跳过`
}

/* ---------- 白名单 ---------- */
let whitelist = {}
try {
  whitelist = JSON.parse(fs.readFileSync(whitelistPath, 'utf8'))
} catch {
  console.error(`[WARN] 白名单 ${whitelistPath} 缺失/损坏,按空白名单执行(全部规则将报满)`)
  whitelist = {}
}

/* ---------- emit(与 design-system-audit.mjs 同风格) ---------- */
let failed = 0
function emit(status, line, detail = []) {
  console.log(`[${status}] ${line}`)
  for (const d of detail) console.log(`    - ${d}`)
  if (status === 'FAIL') failed++
}
/** 非 FAIL 规则的明细截断(防 WARN 刷屏);FAIL 明细全量输出 */
const MAX_DETAIL = 30

/* ---------- 逐规则运行 ---------- */
const RULES = ['R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7', 'R8']
const score = {}
for (const id of RULES) {
  const mod = await import(`./rules/${id}.mjs`)
  const res = mod.run(graph, contract, whitelist)
  score[id] = res.counts ?? {}
  if (contractNote && id === 'R1' && !res.issues.length) {
    emit('WARN', `R1 契约闭合: ${contractNote}`)
  } else {
    const status = res.issues.some((i) => i.status === 'FAIL') ? 'FAIL' : (res.issues.length ? 'WARN' : 'PASS')
    const shown = status === 'FAIL' ? res.issues : res.issues.slice(0, MAX_DETAIL)
    const detailLines = shown.map((i) => `[${i.status}] ${i.loc} ${i.detail}`)
    if (res.issues.length > shown.length) {
      detailLines.push(`[截断] 另 ${res.issues.length - shown.length} 处见 --graph 全量 JSON`)
    }
    emit(status, `R${id.slice(1)} ${res.name}: ${res.summary}`, detailLines)
  }
}

/* ---------- Score 汇总 ---------- */
const g = graph.stats
console.log(
  `Score: 文件 ${g.files} | 组件 ${g.components} | 端点 ${g.endpoints} | effects ${g.effects} | timeFields ${g.timeFields} | `
  + `memoProps ${g.memoProps} | renderImpure ${g.renderImpure} | routes ${g.routes} | `
  + `R1 ${score.R1.fail ?? 0}/${score.R1.total ?? 0} | R2 ${score.R2.fail ?? 0}/${score.R2.total ?? 0} | R3 ${score.R3.fail ?? 0}/${score.R3.total ?? 0} | `
  + `R4 ${score.R4.fail ?? 0}/${score.R4.total ?? 0} | R5 ${score.R5.warn ?? 0} | R6 ${score.R6.fail ?? 0}/${score.R6.total ?? 0} | `
  + `R7 ${score.R7.warn ?? 0} | R8 ${score.R8.fail ?? 0}/${score.R8.total ?? 0}`,
)
console.log(failed ? `审计未通过: ${failed} 项 FAIL` : '审计通过')
process.exit(failed ? 1 : 0)
