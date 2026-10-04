#!/usr/bin/env node
/**
 * 图构建器(图审计器第一段):零视觉、零执行——把 frontend/src 编译成静态调用图 graph.json,
 * 供 graph-audit.mjs 的 8 条规则在图上查询,取代浏览器审计。
 *
 * 手段:TypeScript Compiler API(ts.createProgram + checker),diagnostics 一律忽略
 * (只取 AST 与类型信息),不跑任何构建/测试/浏览器。
 *
 * 图内容:
 *   modules      文件 → imports(解析到 src 内真实文件)
 *   components   组件定义(export function/const 首字母大写 + JSX 返回)与 JSX 使用点
 *   apiEndpoints src/api/*.ts 导出函数(async 函数与 use* hook)→ consumedBy + fields + urls
 *   effects      setInterval/EventSource/addEventListener/visibilitychange → cleanup/visibility
 *   timeFields   时间字段属性访问(triggered_at 等)→ guarded(是否经 resolveSignalMoment/formatSignalTime)
 *   memoProps    memo(...) 组件的 JSX 使用点 props → inline(对象/箭头/数组字面量)
 *   renderImpure JSX 表达式位置内 getBoundingClientRect / JSON.stringify 调用
 *   orphans      usedIn 为空的组件
 *   routes       App.tsx <Route path> + 懒加载组件文件;layouts/menu.tsx 菜单项
 *
 * 用法:
 *   node scripts/graph-audit/build.mjs [--out <file>] [--components] [--usedby <名称>] [--help]
 *     默认把 graph.json 打印到 stdout;--out 写文件(仍打印统计行);
 *     --components 输出基板组件→消费清单(T-87 爆炸半径视图)。
 *
 * 局限(尽力解析,不阻断):组件标签解析走"文件顶层绑定/import 链",穿透 barrel 具名
 * re-export 与 export * from(深度上限 4);动态 import() 目标记入 dynamicImports(孤儿豁免),
 * 但 lazy 挂载的组件不产生 JSX 使用点;字段收集用 checker 类型展开到第二层;JSX 标签、
 * effect 清理均为静态启发式。
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const DIR = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(DIR, '..', '..', '..', 'frontend') // frontend/
// typescript 从 frontend/node_modules 解析(脚本已移出 frontend/scripts,ESM 包解析不再命中)
const require = createRequire(path.join(ROOT, 'tsconfig.app.json'))
const ts = require('typescript')
const SRC = path.join(ROOT, 'src')

/* ============ 常量 ============ */
/** 后端 naive ISO 时间字段(展示必须流经 utils/time.ts 单一事实源) */
const TIME_FIELDS = new Set([
  'triggered_at', 'as_of', 'updated_at',
  'triggeredAt', 'asOf', 'updatedAt',
  'quote_time', 'quoteTime',
])
/** 时间展示/排序出口函数(字段值经这些出口 = 合规;utils/time.ts 为单一事实源) */
const TIME_FNS = new Set([
  'resolveSignalMoment', 'formatSignalTime', 'formatSignalMinute',
  'formatFullTime', 'toMarketEpochMs',
])
/** 效果调用 → 对应清理调用名(EventSource 用 .close(),其余为顶层函数/属性名) */
const CLEANUP_OF = { setInterval: ['clearInterval'], EventSource: ['close'], addEventListener: ['removeEventListener'], visibilitychange: ['removeEventListener'] }
/** 组件标签解析时排除的路由/包装标识符(非业务组件) */
const ROUTE_STD = new Set(['Navigate', 'Route', 'Routes', 'Suspense', 'MainLayout', 'RouteFallback', 'QueryRedirect', 'ComponentType', 'L'])

/* ============ 工具 ============ */
const relOf = (abs) => (abs && abs.startsWith(SRC) ? path.relative(SRC, abs).split(path.sep).join('/') : null)
const lineOf = (node) => node.getSourceFile().getLineAndCharacterOfPosition(node.getStart()).line + 1
const isPascal = (s) => /^[A-Z]/.test(s)
/** 剥掉 as 断言/类型断言/括号(export default memo(X) as typeof X 等形态) */
const unwrapExpr = (e) => {
  while (e && (ts.isAsExpression(e) || ts.isSatisfiesExpression(e) || ts.isTypeAssertionExpression(e) || ts.isParenthesizedExpression(e))) e = e.expression
  return e
}
/** 是否动态 import() 调用 */
const isDynamicImport = (m) => ts.isCallExpression(m) && m.expression.kind === ts.SyntaxKind.ImportKeyword && m.arguments.length > 0
/** JSX 元素统一取标签名(JsxElement 走 openingElement;JSXSelfClosingElement 自带 tagName) */
const jsxTag = (n) => (ts.isJsxElement(n) ? n.openingElement.tagName : n.tagName)
/** JSX 元素统一取属性(JsxElement 走 openingElement.attributes;自闭合自带 attributes) */
const jsxAttrs = (n) => (ts.isJsxElement(n) ? n.openingElement.attributes : n.attributes)

/** 深度优先遍历(带进入/离开回调,state 贯穿) */
function dfs(node, onEnter, onExit, state) {
  if (!node) return
  if (onEnter) onEnter(node, state)
  ts.forEachChild(node, (c) => dfs(c, onEnter, onExit, state))
  if (onExit) onExit(node, state)
}

/** 调用表达式的 callee 名(setInterval / source.close / document.removeEventListener …) */
function calleeNameOf(n) {
  if (ts.isCallExpression(n)) {
    if (ts.isIdentifier(n.expression)) return n.expression.text
    if (ts.isPropertyAccessExpression(n.expression)) return n.expression.name.text
  }
  if (ts.isNewExpression(n) && ts.isIdentifier(n.expression)) return n.expression.text
  return null
}

function hasModifier(node, kind) {
  return ts.getModifiers(node)?.some((m) => m.kind === kind) ?? false
}
const EXPORT_KW = ts.SyntaxKind.ExportKeyword
const ASYNC_KW = ts.SyntaxKind.AsyncKeyword

/** 节点子树是否含 JSX 元素(组件定义判定) */
function containsJsx(node) {
  let found = false
  const v = (n) => {
    if (found) return
    if (ts.isJsxElement(n) || ts.isJsxFragment(n) || ts.isJsxSelfClosingElement(n)) { found = true; return }
    ts.forEachChild(n, v)
  }
  v(node)
  return found
}

/* ============ TS Program ============ */
const configPath = path.join(ROOT, 'tsconfig.app.json')
const parsed = ts.getParsedCommandLineOfConfigFile(configPath, {}, {
  ...ts.sys,
  onUnRecoverableConfigFileDiagnostic: () => {},
})
const program = ts.createProgram(parsed.fileNames, parsed.options)
const checker = program.getTypeChecker()

/** import 说明符 → 绝对文件路径(仅相对/绝对路径;裸包名返回 null) */
function resolveImport(spec, fromFile) {
  if (!spec.startsWith('.') && !spec.startsWith('/')) return null
  const res = ts.resolveModuleName(spec, fromFile, parsed.options, ts.sys)
  return res.resolvedModule?.resolvedFileName ?? null
}

/* ============ Phase 1:文件元信息(imports / 顶层绑定 / 导出) ============ */
/** relPath → { sf, imports:[], topBindings:Map, exports:Map } */
const fileInfos = new Map()
const sourceFiles = program.getSourceFiles()
  .filter((sf) => sf.fileName.startsWith(SRC + path.sep) && !sf.fileName.endsWith('.d.ts'))
  .sort((a, b) => a.fileName.localeCompare(b.fileName))

for (const sf of sourceFiles) {
  const rel = relOf(sf.fileName)
  if (!rel) continue
  const info = { sf, imports: [], topBindings: new Map(), exports: new Map(), reexports: new Map(), starFrom: [] }
  fileInfos.set(rel, info)

  for (const st of sf.statements) {
    /* import 声明:记录模块依赖 + 具名/默认/命名空间绑定 */
    if (ts.isImportDeclaration(st) && ts.isStringLiteralLike(st.moduleSpecifier)) {
      const spec = st.moduleSpecifier.text
      const resolved = resolveImport(spec, sf.fileName)
      const target = relOf(resolved)
      if (target) info.imports.push(target)
      const clause = st.importClause
      if (clause) {
        if (clause.name) info.topBindings.set(clause.name.text, { kind: 'import', from: target, exportName: 'default' })
        if (clause.namedBindings) {
          if (ts.isNamespaceImport(clause.namedBindings)) {
            info.topBindings.set(clause.namedBindings.name.text, { kind: 'ns', from: target })
          } else {
            for (const el of clause.namedBindings.elements) {
              const local = el.name.text
              const exp = el.propertyName ? el.propertyName.text : local
              info.topBindings.set(local, { kind: 'import', from: target, exportName: exp })
            }
          }
        }
      }
      continue
    }
    /* export { A as B } from './x' / export * from './x' / export { local } */
    if (ts.isExportDeclaration(st)) {
      const fromSpec = st.moduleSpecifier && ts.isStringLiteralLike(st.moduleSpecifier) ? st.moduleSpecifier.text : null
      const from = fromSpec ? relOf(resolveImport(fromSpec, sf.fileName)) : null
      if (st.exportClause && ts.isNamedExports(st.exportClause)) {
        for (const el of st.exportClause.elements) {
          const local = el.propertyName ? el.propertyName.text : el.name.text
          info.exports.set(el.name.text, local)
          if (from) info.reexports.set(el.name.text, { local, from })
        }
      } else if (!st.exportClause && from) {
        info.starFrom.push(from)
      }
      continue
    }
    /* 顶层声明绑定(函数/变量) */
    if (ts.isFunctionDeclaration(st) && st.name) {
      info.topBindings.set(st.name.text, { kind: 'decl' })
      if (hasModifier(st, EXPORT_KW)) info.exports.set(st.name.text, st.name.text)
      continue
    }
    if (ts.isVariableStatement(st)) {
      const exported = hasModifier(st, EXPORT_KW)
      for (const d of st.declarationList.declarations) {
        if (ts.isIdentifier(d.name)) {
          info.topBindings.set(d.name.text, { kind: 'decl' })
          if (exported) info.exports.set(d.name.text, d.name.text)
        }
      }
    }
  }
  /* export default X / export default function X / export default memo(X) */
  for (const st of sf.statements) {
    if (hasModifier(st, ts.SyntaxKind.DefaultKeyword)) {
      if (ts.isFunctionDeclaration(st) && st.name) info.exports.set('default', st.name.text)
      else if (ts.isVariableStatement(st)) {
        const d = st.declarationList.declarations[0]
        if (d && ts.isIdentifier(d.name)) info.exports.set('default', d.name.text)
      } else if (ts.isClassDeclaration(st) && st.name) {
        info.exports.set('default', st.name.text)
      }
    } else if (ts.isExportAssignment(st) && !st.isExportEquals) {
      /* export default DataTable / export default memo(DataTable) [as typeof X] */
      const e = unwrapExpr(st.expression)
      if (ts.isIdentifier(e)) info.exports.set('default', e.text)
      else if (ts.isCallExpression(e) && calleeNameOf(e) === 'memo' && ts.isIdentifier(e.arguments[0])) {
        info.exports.set('default', e.arguments[0].text)
      }
    }
  }
}

/* ============ 组件定义 + memo 定义 ============ */
/**
 * components: [{name, file, line, usedIn:[]}]
 * 口径:顶层 Pascal 声明(export 或私有,如模块内 memo 子组件)且体含 JSX → 组件;
 *       形态覆盖 function / const(含 memo(...)/forwardRef(...) 包裹)/ class extends Component;
 *       export default X / export default memo(X) 引用的本文件顶层声明同样算。
 * memoDefs: Map<组件名, 定义文件> —— memo(...) 包裹的组件(不限 export)。
 */
const components = []
const memoDefs = new Map()
for (const [rel, info] of fileInfos) {
  if (!rel.endsWith('.tsx')) continue
  for (const st of info.sf.statements) {
    let name = null
    let body = null
    if (ts.isFunctionDeclaration(st) && st.name) {
      name = st.name.text
      body = st.body
    } else if (ts.isVariableStatement(st)) {
      for (const d of st.declarationList.declarations) {
        if (!ts.isIdentifier(d.name) || !d.initializer) continue
        const init = d.initializer
        if (ts.isCallExpression(init) && (calleeNameOf(init) === 'memo' || calleeNameOf(init) === 'forwardRef')) {
          /* memo(箭头函数|函数声明) / memo(已有组件) / forwardRef(fn):定义位置均算本文件 */
          if (calleeNameOf(init) === 'memo') memoDefs.set(d.name.text, rel)
          const inner = init.arguments[0]
          if (ts.isFunctionLike(inner) && inner.body) { name = d.name.text; body = inner.body }
          else name = d.name.text // memo(已有组件):定义位置仍算本文件
          break
        }
        if (ts.isFunctionLike(init)) { name = d.name.text; body = init.body }
      }
    } else if (ts.isClassDeclaration(st) && st.name) {
      /* class X extends Component:体含 JSX(render 返回)即组件 */
      name = st.name.text
      body = st
    } else if (ts.isExportAssignment(st) && !st.isExportEquals) {
      /* export default memo(DataTable):组件名 = 被包裹标识符,归入 memo 契约 */
      const e = unwrapExpr(st.expression)
      let compName = null
      if (ts.isIdentifier(e)) compName = e.text
      else if (ts.isCallExpression(e) && calleeNameOf(e) === 'memo' && ts.isIdentifier(e.arguments[0])) compName = e.arguments[0].text
      if (compName) memoDefs.set(compName, rel)
      continue
    }
    if (!name || !isPascal(name) || ROUTE_STD.has(name)) continue
    if (body && containsJsx(body)) components.push({ name, file: rel, line: lineOf(st), usedIn: [] })
  }
}

/** JSX 标签名 → 组件定义文件(tsx 顶层绑定优先,再 import 链;尽力) */
function resolveTagDef(name, rel) {
  const b = fileInfos.get(rel)?.topBindings.get(name)
  if (!b) return null
  if (b.kind === 'decl') return rel
  if (b.kind !== 'import' || !b.from) return null
  return resolveExport(b.from, b.exportName)
}

/**
 * 解析 exportName 的真实定义文件:穿透 barrel 的 re-export 链
 * (export { A as B } from './x' / export * from './x'),深度上限 4 防环。
 */
function resolveExport(file, exportName, depth = 0) {
  if (depth > 4) return null
  const info = fileInfos.get(file)
  if (!info) return null
  const re = info.reexports.get(exportName)
  if (re) return resolveExport(re.from, re.local, depth + 1)
  if (info.exports.has(exportName)) return file
  for (const star of info.starFrom) {
    const hit = resolveExport(star, exportName, depth + 1)
    if (hit) return hit
  }
  return null
}

/** JSX 使用点:useSites [{file,line,name,defFile}] */
const useSites = []
for (const [rel, info] of fileInfos) {
  if (!rel.endsWith('.tsx')) continue
  dfs(info.sf, (n) => {
    if (ts.isJsxElement(n) || ts.isJsxSelfClosingElement(n)) {
      const tag = jsxTag(n)
      if (ts.isIdentifier(tag) && isPascal(tag.text)) {
        const def = resolveTagDef(tag.text, rel)
        if (def) useSites.push({ file: rel, line: lineOf(n), name: tag.text, defFile: def })
      }
    }
  })
}
const compIndex = new Map(components.map((c) => [`${c.name}@${c.file}`, c]))
for (const u of useSites) {
  const c = compIndex.get(`${u.name}@${u.defFile}`)
  if (c) c.usedIn.push({ file: u.file, line: u.line })
}

/* ============ apiEndpoints ============ */
/** src/api/*.ts 导出的 async 函数与 use* hook */
const endpoints = []
for (const rel of fileInfos.keys()) {
  if (!rel.startsWith('api/') || !rel.endsWith('.ts')) continue
  const info = fileInfos.get(rel)
  for (const st of info.sf.statements) {
    if (!hasModifier(st, EXPORT_KW)) continue
    if (ts.isFunctionDeclaration(st) && st.name) {
      const isHook = st.name.text.startsWith('use')
      const isAsync = hasModifier(st, ASYNC_KW)
      if (!isHook && !isAsync) continue
      endpoints.push({ name: st.name.text, file: rel, line: lineOf(st), isHook, callSites: [], fnNode: st })
      continue
    }
    if (ts.isVariableStatement(st)) {
      for (const d of st.declarationList.declarations) {
        if (!ts.isIdentifier(d.name) || !d.initializer) continue
        const init = d.initializer
        if (!ts.isFunctionLike(init)) continue
        const isHook = d.name.text.startsWith('use')
        let isAsync = false
        if (ts.isArrowFunction(init)) isAsync = hasModifier(init, ASYNC_KW)
        else if (ts.isFunctionExpression(init)) isAsync = hasModifier(init, ASYNC_KW)
        if (!isHook && !isAsync) continue
        endpoints.push({ name: d.name.text, file: rel, line: lineOf(st), isHook, callSites: [], fnNode: init })
      }
    }
  }
}

/** 全 src 扫描 api 函数调用点(callee 经本文件 import 绑定指向 api 文件) */
for (const [rel, info] of fileInfos) {
  dfs(info.sf, (n) => {
    if (!ts.isCallExpression(n)) return
    const cn = calleeNameOf(n)
    if (!cn) return
    const b = info.topBindings.get(cn)
    if (!b || b.kind !== 'import' || !b.from || !b.from.startsWith('api/')) return
    const ep = endpoints.find((e) => e.file === b.from && e.name === b.exportName)
    if (!ep) return
    ep.callSites.push({ node: n, rel })
  })
}

/** 展开返回类型字段到第二层(记顶层裸名 + 第二层 parent.child 与裸名);Promise 解包。
 *  仅展开可展开类型(对象/接口);原始类型/String/数组/Date 等的原型成员不收集,防字段污染。 */
const NON_EXPAND = ts.TypeFlags.StringLike | ts.TypeFlags.NumberLike | ts.TypeFlags.BooleanLike
  | ts.TypeFlags.BigIntLike | ts.TypeFlags.ESSymbolLike | ts.TypeFlags.Void
  | ts.TypeFlags.Undefined | ts.TypeFlags.Null | ts.TypeFlags.Never
function isExpandable(ty) {
  if (!ty) return false
  if (ty.isUnion()) return ty.types.some(isExpandable)
  if (ty.isIntersection()) return ty.types.some(isExpandable)
  if (ty.flags & NON_EXPAND) return false
  const sym = ty.aliasSymbol ?? ty.symbol
  if (sym) {
    const n = sym.getName()
    if (n === 'Promise') return true // 解包后继续判断
    if (n === 'Array' || n === 'ReadonlyArray' || n === 'Date' || n === 'Map' || n === 'Set' || n === 'WeakMap' || n === 'WeakSet' || n === 'RegExp' || n === 'Function') return false
  }
  return true
}

function collectReturnFields(ty, out, depth = 0, prefix = '') {
  if (!ty) return
  if (ty.isUnion()) { for (const m of ty.types) collectReturnFields(m, out, depth, prefix); return }
  if (ty.isIntersection()) { for (const m of ty.types) collectReturnFields(m, out, depth, prefix); return }
  /* async 函数调用 getTypeAtLocation 返回实例化 Promise(aliasSymbol 可能丢失,symbol 仍是 Promise) */
  const symName = ty.aliasSymbol?.getName() ?? ty.symbol?.getName()
  if (symName === 'Promise') {
    const args = checker.getTypeArguments(ty)
    if (args[0]) collectReturnFields(args[0], out, depth, prefix)
    return
  }
  /* 非可展开类型(数组/字符串/原始值等)不收集其原型成员 */
  if (!isExpandable(ty)) return
  const props = ty.getProperties?.()
  if (!props) return
  for (const p of props) {
    const name = p.getName()
    if (name === 'then' || name === 'catch' || name === 'finally' || name.startsWith('__@')) continue
    out.add(name)
    if (depth === 0) {
      const pt = checker.getTypeOfSymbolAtLocation(p, p.valueDeclaration ?? p.declarations?.[0])
      if (isExpandable(pt)) collectReturnFields(pt, out, 1, name)
    } else {
      out.add(`${prefix}.${name}`)
      out.add(name)
    }
  }
}

for (const ep of endpoints) {
  const consumed = new Set()
  const fields = new Set()
  const urls = new Set()
  for (const cs of ep.callSites) {
    consumed.add(cs.rel)
    try {
      const t = checker.getTypeAtLocation(cs.node)
      collectReturnFields(t, fields)
    } catch { /* 类型解析失败跳过(尽力) */ }
  }
  /* URL 从 api 函数体内收集(axios/fetch 调用参数;StringLiteral 与模板字符串的静态文本) */
  if (ep.fnNode) {
    const grab = (n) => {
      if (ts.isCallExpression(n)) {
        const cn = calleeNameOf(n)
        if (cn && ['get', 'post', 'put', 'patch', 'delete', 'request', 'fetch'].includes(cn)) {
          const a0 = n.arguments[0]
          if (a0) {
            if (ts.isStringLiteral(a0)) urls.add(a0.text)
            else if (ts.isNoSubstitutionTemplateLiteral(a0)) urls.add(a0.text)
            else if (ts.isTemplateExpression(a0)) {
              /* 拼接模板文本(插值位以 :p 占位,尽力) */
              let txt = ''
              txt += a0.head.text
              for (const sp of a0.templateSpans) txt += ':p' + sp.literal.text
              urls.add(txt)
            }
          }
        }
      }
      ts.forEachChild(n, grab)
    }
    grab(ep.fnNode)
  }
  ep.consumedBy = [...consumed]
  ep.fields = [...fields]
  ep.urls = [...urls]
  delete ep.callSites
  delete ep.fnNode
}

/* ============ effects ============ */
const effects = []
const effectCbSet = new Set() // useEffect 回调函数节点
for (const [rel, info] of fileInfos) {
  if (!/\.(ts|tsx)$/.test(rel)) continue
  const fileText = info.sf.getFullText()
  const fileTextHasVis = fileText.includes('document.visibilityState') || fileText.includes("'visibilitychange'")

  const state = { fns: [] }
  dfs(info.sf, (n, st) => {
    if (ts.isFunctionLike(n)) st.fns.push(n)
    if (ts.isCallExpression(n)) {
      const cn = calleeNameOf(n)
      if (cn === 'useEffect' && n.arguments.length && ts.isFunctionLike(n.arguments[0])) {
        effectCbSet.add(n.arguments[0])
      }
    }
  }, (n, st) => {
    if (ts.isFunctionLike(n)) st.fns.pop()
  }, state)

  /* 第二遍:定位效果调用 */
  const state2 = { fns: [] }
  dfs(info.sf, (n, st) => {
    if (ts.isFunctionLike(n)) st.fns.push(n)
    /* 效果调用判定 */
    let kind = null
    if (ts.isCallExpression(n) && ts.isIdentifier(n.expression)) {
      if (n.expression.text === 'setInterval') kind = 'setInterval'
      else if (n.expression.text === 'addEventListener') {
        const ev = n.arguments[1]
        kind = ev && ts.isStringLiteral(ev) && ev.text === 'visibilitychange' ? 'visibilitychange' : 'addEventListener'
      }
    } else if (ts.isCallExpression(n) && ts.isPropertyAccessExpression(n.expression) && n.expression.name.text === 'addEventListener') {
      const ev = n.arguments[1]
      kind = ev && ts.isStringLiteral(ev) && ev.text === 'visibilitychange' ? 'visibilitychange' : 'addEventListener'
    } else if (ts.isCallExpression(n) && ts.isPropertyAccessExpression(n.expression) && n.expression.name.text === 'setInterval' && ['window', 'globalThis', 'self'].includes(n.expression.expression.text)) {
      /* window.setInterval / globalThis.setInterval / self.setInterval:与裸调用同口径 */
      kind = 'setInterval'
    } else if (ts.isNewExpression(n) && ts.isIdentifier(n.expression) && n.expression.text === 'EventSource') {
      kind = 'EventSource'
    }
    if (!kind) return
    /* 效果容器:最内层所在(向上)的 useEffect 回调或 use* hook 函数 */
    let container = null
    for (let i = st.fns.length - 1; i >= 0; i--) {
      const fn = st.fns[i]
      if (effectCbSet.has(fn)) { container = fn; break }
      if (fn.name && fn.name.text.startsWith('use')) { container = fn; break }
    }
    /* 效果接收者(addEventListener 的挂载对象名,EventSource 实例豁免判定用) */
    let receiver = null
    if (kind === 'addEventListener' && ts.isCallExpression(n) && ts.isPropertyAccessExpression(n.expression) && ts.isIdentifier(n.expression.expression)) {
      receiver = n.expression.expression.text
    }
    if (!container) {
      /* 非 hook 上下文的 addEventListener(如 client.ts waitForJob 工具函数):
       *   ① EventSource 实例级 listener:随实例 close/销毁清理,不需 removeEventListener
       *   ② handler 具名 → 全文件搜 removeEventListener 第二实参同名(attach/detach 跨函数配对)
       *   ③ 模块级单次注册(不在任何函数内)→ 无累积,豁免
       *   其余(如 waitForJob 每次调用注册匿名 visibilitychange 监听)→ 泄漏 FAIL */
      if (kind === 'addEventListener' || kind === 'visibilitychange') {
        const handler = n.arguments[1]
        let selfCleanup = false
        /* ① EventSource 实例级监听 */
        if (receiver) {
          const fileText2 = info.sf.getFullText()
          selfCleanup = new RegExp(`\\b${receiver}\\s*=\\s*new\\s+EventSource\\b`).test(fileText2)
        }
        /* ② handler 具名:全文件同名 removeEventListener 配对 */
        if (!selfCleanup && handler && ts.isIdentifier(handler)) {
          let hit2 = false
          const v4 = (m) => {
            if (hit2) return
            if (ts.isCallExpression(m) && calleeNameOf(m) === 'removeEventListener'
              && m.arguments[1] && ts.isIdentifier(m.arguments[1]) && m.arguments[1].text === handler.text) {
              hit2 = true
              return
            }
            ts.forEachChild(m, v4)
          }
          v4(info.sf)
          selfCleanup = hit2
        }
        /* ③ 模块级单次注册 */
        if (!selfCleanup && st.fns.length === 0) selfCleanup = true
        effects.push({ kind, file: rel, line: lineOf(n), cleanup: selfCleanup })
      }
      return
    }
    /* 清理判定:容器函数体内(含 return 清理函数)存在对应清理调用;
       addEventListener 挂在 new EventSource 实例上时,实例 .close() 即视为已清理 */
    const targets = CLEANUP_OF[kind]
    let cleanup = false
    if (container.body) {
      const v = (m) => {
        if (cleanup) return
        if (ts.isCallExpression(m) || ts.isNewExpression(m)) {
          const c = calleeNameOf(m)
          if (c && targets.includes(c)) { cleanup = true; return }
        }
        ts.forEachChild(m, v)
      }
      v(container.body)
    }
    if (!cleanup && kind === 'addEventListener' && receiver) {
      /* EventSource 实例监听:容器内 new EventSource 赋值同名变量 + .close() → 生命周期随 close */
      let srcAssigned = false
      let srcClosed = false
      if (container.body) {
        const v = (m) => {
          if (ts.isVariableDeclaration(m) && ts.isIdentifier(m.name) && m.name.text === receiver && m.initializer
            && ts.isNewExpression(m.initializer) && ts.isIdentifier(m.initializer.expression) && m.initializer.expression.text === 'EventSource') {
            srcAssigned = true
            return
          }
          if (ts.isPropertyAccessExpression(m) && m.name.text === 'close' && ts.isIdentifier(m.expression) && m.expression.text === receiver) {
            srcClosed = true
            return
          }
          ts.forEachChild(m, v)
        }
        v(container.body)
      }
      if (srcAssigned && srcClosed) cleanup = true
    }
    /* setInterval 额外 visibility:容器体内或同文件存在 visibilityState 检查 */
    let visibility
    if (kind === 'setInterval') {
      visibility = fileTextHasVis
      if (!visibility && container.body) {
        const v2 = (m) => {
          if (visibility) return
          if (ts.isPropertyAccessExpression(m) && m.name.text === 'visibilityState') { visibility = true; return }
          if (ts.isStringLiteral(m) && m.text === 'visibilitychange') { visibility = true; return }
          ts.forEachChild(m, v2)
        }
        v2(container.body)
      }
    }
    effects.push({ kind, file: rel, line: lineOf(n), cleanup, visibility })
  }, (n, st) => {
    if (ts.isFunctionLike(n)) st.fns.pop()
  }, state2)
}

/* ============ timeFields ============ */
/**
 * 口径(展示路径判定,机制化):
 *   - 只有「JSX 展示上下文」内的时间字段访问才参与 R2——排序/搬运/存储等非 JSX
 *     上下文(columns sorter、rowModel 映射、data 层)不判,避免误报。
 *   - guarded = 字段访问所在的最内层 JSX 元素子树内出现时间出口调用(TIME_FNS)
 *     或时间守卫组件 SignalMomentCell(组件内部走 resolveSignalMoment)。
 *   - utils/time.ts 自身是事实源,豁免。
 */
const TIME_GUARD_TAGS = new Set(['SignalMomentCell'])

/** 子树内是否出现时间出口调用或守卫组件标签 */
function treeHasTimeExit(root) {
  let hit = false
  const v = (m) => {
    if (hit) return
    if (ts.isCallExpression(m)) {
      const c = calleeNameOf(m)
      if (c && TIME_FNS.has(c)) { hit = true; return }
    }
    if (ts.isJsxElement(m) || ts.isJsxSelfClosingElement(m)) {
      const tag = jsxTag(m)
      if (ts.isIdentifier(tag) && TIME_GUARD_TAGS.has(tag.text)) { hit = true; return }
    }
    ts.forEachChild(m, v)
  }
  v(root)
  return hit
}

const timeFields = []
for (const [rel, info] of fileInfos) {
  if (!/\.(ts|tsx)$/.test(rel)) continue
  if (rel === 'utils/time.ts') continue // 事实源自身:内部处理时间字段合法
  const st = { jsx: [] }
  dfs(info.sf, (n, s) => {
    if (ts.isJsxElement(n) || ts.isJsxSelfClosingElement(n) || ts.isJsxFragment(n)) s.jsx.push(n)
    let field = null
    let expr = null
    if (ts.isPropertyAccessExpression(n) && TIME_FIELDS.has(n.name.text)) {
      field = n.name.text
      expr = n.expression
    } else if (ts.isElementAccessExpression(n) && n.argumentExpression && ts.isStringLiteral(n.argumentExpression) && TIME_FIELDS.has(n.argumentExpression.text)) {
      field = n.argumentExpression.text
      expr = n.expression
    }
    if (!field) return
    if (s.jsx.length === 0) return // 非展示上下文(排序/搬运/存储):不判
    /* 展示上下文:最内层 JSX 元素子树含时间出口/守卫组件 = guarded */
    const guarded = treeHasTimeExit(s.jsx[s.jsx.length - 1])
    /* owner 尽力解析:表达式类型符号名 + 是否声明于 src/api */
    let owner = ''
    let ownerApi = false
    try {
      const t = checker.getTypeAtLocation(expr)
      if (t && t.symbol) {
        owner = t.symbol.getName()
        const decl = t.symbol.declarations?.[0]
        if (decl && /[\\/]api[\\/]/.test(decl.getSourceFile().fileName)) ownerApi = true
      }
    } catch { /* 忽略 */ }
    timeFields.push({ file: rel, line: lineOf(n), field, guarded, owner, ownerApi })
  }, (n, s) => {
    if (ts.isJsxElement(n) || ts.isJsxSelfClosingElement(n) || ts.isJsxFragment(n)) s.jsx.pop()
  }, st)
}

/* ============ memoProps ============ */
const memoProps = []
for (const u of useSites) {
  if (!memoDefs.has(u.name)) continue
  const info = fileInfos.get(u.file)
  if (!info) continue
  dfs(info.sf, (n) => {
    if (!ts.isJsxElement(n) && !ts.isJsxSelfClosingElement(n)) return
    if (!ts.isIdentifier(jsxTag(n)) || jsxTag(n).text !== u.name || lineOf(n) !== u.line) return
    for (const a of jsxAttrs(n).properties) {
      if (!ts.isJsxAttribute(a) || !ts.isIdentifier(a.name)) continue
      let inline = false
      if (a.initializer && ts.isJsxExpression(a.initializer) && a.initializer.expression) {
        const e = a.initializer.expression
        if (ts.isObjectLiteralExpression(e) || ts.isArrowFunction(e) || ts.isArrayLiteralExpression(e)) inline = true
      }
      memoProps.push({ file: u.file, line: u.line, component: u.name, prop: a.name.text, inline })
    }
  })
}

/* ============ renderImpure ============ */
const renderImpure = []
const impureSeen = new Set()
for (const [rel, info] of fileInfos) {
  if (!rel.endsWith('.tsx')) continue
  dfs(info.sf, (n) => {
    if (!ts.isJsxExpression(n) || !n.expression) return
    const collect = (m) => {
      if (ts.isCallExpression(m)) {
        const e = m.expression
        let api = null
        if (ts.isPropertyAccessExpression(e) && e.name.text === 'getBoundingClientRect') api = 'getBoundingClientRect'
        else if (ts.isPropertyAccessExpression(e) && ts.isIdentifier(e.expression) && e.expression.text === 'JSON' && e.name.text === 'stringify') api = 'JSON.stringify'
        if (api) {
          const key = `${rel}:${lineOf(m)}:${api}`
          if (!impureSeen.has(key)) {
            impureSeen.add(key)
            renderImpure.push({ file: rel, line: lineOf(m), api })
          }
        }
      }
      ts.forEachChild(m, collect)
    }
    collect(n.expression)
  })
}

/* ============ routes + menu ============ */
const routes = []
const menu = []
{
  const appInfo = fileInfos.get('App.tsx')
  if (appInfo) {
    const lazyMap = new Map() // 组件名 → 懒加载文件 rel
    for (const st of appInfo.sf.statements) {
      if (!ts.isVariableStatement(st)) continue
      for (const d of st.declarationList.declarations) {
        if (!ts.isIdentifier(d.name) || !d.initializer) continue
        const init = d.initializer
        if (!ts.isCallExpression(init)) continue
        const cn = calleeNameOf(init)
        if (cn !== 'load' && cn !== 'lazy') continue
        const arg0 = init.arguments[0]
        if (!arg0 || !ts.isArrowFunction(arg0)) continue
        let spec = null
        const find = (m) => {
          if (spec) return
          if (isDynamicImport(m) && ts.isStringLiteralLike(m.arguments[0])) {
            spec = m.arguments[0].text
            return
          }
          ts.forEachChild(m, find)
        }
        find(arg0)
        if (!spec) continue
        const resolved = resolveImport(spec, appInfo.sf.fileName)
        const target = relOf(resolved)
        if (target) lazyMap.set(d.name.text, target)
      }
    }
    /* Route 元素:path + element 中懒加载组件名 */
    dfs(appInfo.sf, (n) => {
      if (!ts.isJsxElement(n) && !ts.isJsxSelfClosingElement(n)) return
      if (!ts.isIdentifier(jsxTag(n)) || jsxTag(n).text !== 'Route') return
      let p = null
      let compName = null
      for (const a of jsxAttrs(n).properties) {
        if (!ts.isJsxAttribute(a) || !ts.isIdentifier(a.name)) continue
        if (a.name.text === 'path' && a.initializer && ts.isStringLiteral(a.initializer)) p = a.initializer.text
        else if (a.name.text === 'element' && a.initializer && ts.isJsxExpression(a.initializer) && a.initializer.expression) {
          /* 提取 element 表达式中的业务组件标识符(排除路由包装) */
          const found = []
          const find = (m) => {
            if (found.length) return
            if (ts.isJsxElement(m) && ts.isIdentifier(m.openingElement.tagName)) {
              const t = m.openingElement.tagName.text
              if (ROUTE_STD.has(t) || !isPascal(t)) return
              found.push(t)
              return
            }
            if (ts.isIdentifier(m) && isPascal(m.text) && !ROUTE_STD.has(m.text)) { found.push(m.text); return }
            ts.forEachChild(m, find)
          }
          find(a.initializer.expression)
          compName = found[0] ?? null
        }
      }
      if (p === null || p === '*') return
      const lazyFile = compName && lazyMap.has(compName) ? lazyMap.get(compName) : ''
      routes.push({ path: p, component: compName || '', file: lazyFile, line: lineOf(n) })
    })
  }
}
{
  const menuInfo = fileInfos.get('layouts/menu.tsx')
  if (menuInfo) {
    dfs(menuInfo.sf, (n) => {
      if (!ts.isObjectLiteralExpression(n)) return
      let key = null
      let label = null
      for (const p of n.properties) {
        if (!ts.isPropertyAssignment(p) || !ts.isIdentifier(p.name)) continue
        if (p.name.text === 'key' && ts.isStringLiteral(p.initializer) && p.initializer.text.startsWith('/')) key = p.initializer.text
        else if (p.name.text === 'label' && ts.isStringLiteral(p.initializer)) label = p.initializer.text
      }
      if (key) menu.push({ path: key, label: label || '', line: lineOf(n) })
    })
  }
}

/* ============ orphans ============ */
/* 被路由懒加载引用的页面组件不算孤儿(它们有路由消费,挂载点不在 JSX 使用点内) */
const routeComps = new Set(routes.filter((r) => r.component).map((r) => r.component))
/* 动态 import() 目标文件(懒加载引用;其组件可能经 lazy() 挂载,JSX 使用点不在静态图内) */
const dynamicImportTargets = new Set()
for (const [rel, info] of fileInfos) {
  dfs(info.sf, (n) => {
    if (!isDynamicImport(n) || !ts.isStringLiteralLike(n.arguments[0])) return
    const res = resolveImport(n.arguments[0].text, info.sf.fileName)
    const t = relOf(res)
    if (t) dynamicImportTargets.add(t)
  })
}
const orphans = components
  .filter((c) => c.usedIn.length === 0 && !routeComps.has(c.name) && !dynamicImportTargets.has(c.file))
  .map((c) => ({ name: c.name, file: c.file, line: c.line }))

/* ============ 组装图 ============ */
const graph = {
  schema: 1,
  root: ROOT,
  stats: {
    files: fileInfos.size,
    components: components.length,
    endpoints: endpoints.length,
    effects: effects.length,
    timeFields: timeFields.length,
    memoProps: memoProps.length,
    renderImpure: renderImpure.length,
    orphans: orphans.length,
    routes: routes.length,
    menu: menu.length,
  },
  modules: [...fileInfos].map(([file, info]) => ({ file, imports: info.imports })),
  components,
  dynamicImports: [...dynamicImportTargets].sort(),
  apiEndpoints: endpoints.map(({ name, file, line, isHook, consumedBy, fields, urls }) => ({ name, file, line, isHook, consumedBy, fields, urls })),
  effects,
  timeFields,
  memoProps,
  renderImpure,
  orphans,
  routes,
  menu,
}

const statsLine = `图构建完成: 文件 ${graph.stats.files} | 组件 ${graph.stats.components} | 端点 ${graph.stats.endpoints} | effects ${graph.stats.effects} | timeFields ${graph.stats.timeFields} | memoProps ${graph.stats.memoProps} | renderImpure ${graph.stats.renderImpure} | routes ${graph.stats.routes} | 菜单 ${graph.stats.menu}`

/* ============ CLI(仅主模块运行时执行;被 graph-audit.mjs import 时跳过) ============ */
/**
 * 用法:
 *   node build.mjs --out <file>          图 JSON 落盘(graph-audit.mjs --graph 复用)
 *   node build.mjs --components          基板组件(components/ui/)→消费文件清单(T-87 爆炸半径视图)
 *   node build.mjs --usedby <名称>        反查任意组件的 JSX 消费点(文件:行,页面标注 [页面])
 *   node build.mjs --help                本说明
 * 无参数:stdout 输出全量图 JSON。
 */
const UI_BASE = 'components/ui/'

/** 消费点行:file:line,页面消费标 [页面](其余为组件间互用) */
function usageLine(u) {
  return `  ${u.file}:${u.line}${u.file.startsWith('pages/') ? ' [页面]' : ''}`
}

/** 消费点按 文件:行 排序(输出稳定,便于逐条核对) */
const byLoc = (a, b) => (a.file === b.file ? a.line - b.line : a.file < b.file ? -1 : 1)

/** 基板组件→消费文件清单:按消费数降序,动态引用/孤儿分区(T-87) */
function printComponentConsumers() {
  const base = components.filter((c) => c.file.startsWith(UI_BASE))
  const used = base.filter((c) => c.usedIn.length > 0).sort((a, b) => b.usedIn.length - a.usedIn.length)
  const dynamicOnly = base.filter((c) => c.usedIn.length === 0 && dynamicImportTargets.has(c.file))
  const orphanBase = base.filter((c) => c.usedIn.length === 0 && !dynamicImportTargets.has(c.file))
  const consumerFiles = new Set(used.flatMap((c) => c.usedIn.map((u) => u.file)))
  const usageTotal = used.reduce((s, c) => s + c.usedIn.length, 0)
  const lines = [`基板组件→消费清单(${UI_BASE}${base.length} 件;消费点 ${usageTotal} 处;消费文件 ${consumerFiles.size} 个;动态引用 ${dynamicOnly.length} 件;孤儿 ${orphanBase.length} 件)`]
  for (const c of used) {
    lines.push('', `${c.name} [${c.usedIn.length} 处] ${c.file}:${c.line}`)
    lines.push(...c.usedIn.slice().sort(byLoc).map(usageLine))
  }
  if (dynamicOnly.length) {
    lines.push('', `动态 import 引用(无静态 JSX 使用点,${dynamicOnly.length} 件):`)
    for (const c of dynamicOnly) lines.push(`  ${c.name} ${c.file}:${c.line}`)
  }
  if (orphanBase.length) {
    lines.push('', `孤儿(无 JSX 使用点且无动态引用,${orphanBase.length} 件):`)
    for (const c of orphanBase) lines.push(`  ${c.name} ${c.file}:${c.line}`)
  }
  console.log(lines.join('\n'))
}

/** 反查任意组件(name 全图匹配,可重名多文件) */
function printUsedBy(name) {
  const hits = components.filter((c) => c.name === name)
  if (!hits.length) {
    console.error(`组件 ${name} 未找到(组件全集 ${components.length} 件)`)
    process.exitCode = 1
    return
  }
  for (const c of hits) {
    console.log(`${c.name} [${c.usedIn.length} 处] ${c.file}:${c.line}`)
    console.log(c.usedIn.slice().sort(byLoc).map(usageLine).join('\n'))
  }
}

if (path.resolve(process.argv[1] ?? '') === fileURLToPath(import.meta.url)) {
  const outIdx = process.argv.indexOf('--out')
  const usedbyIdx = process.argv.indexOf('--usedby')
  const paramOf = (idx) => (idx >= 0 && idx + 1 < process.argv.length && !process.argv[idx + 1].startsWith('--')) ? process.argv[idx + 1] : null
  if (process.argv.includes('--help')) {
    console.log('用法: build.mjs [--out <file>] [--components] [--usedby <名称>] [--help]')
    console.log('  --out <file>     图 JSON 落盘(graph-audit.mjs --graph 复用)')
    console.log('  --components     基板组件→消费文件清单(按消费数降序,动态引用/孤儿分区)')
    console.log('  --usedby <名称>  反查任意组件的 JSX 消费点')
    console.log('  无参数           stdout 输出全量图 JSON')
  } else if (process.argv.includes('--components')) {
    printComponentConsumers()
  } else if (usedbyIdx >= 0) {
    const name = paramOf(usedbyIdx)
    if (!name) {
      console.error('错误: --usedby 缺少组件名参数(用法: --usedby <名称>)')
      process.exitCode = 1
    } else {
      printUsedBy(name)
    }
  } else if (outIdx >= 0) {
    const outFileArg = paramOf(outIdx)
    if (!outFileArg) {
      console.error('错误: --out 缺少文件路径参数(用法: --out <file>)')
      process.exitCode = 1
    } else {
      const outFile = path.resolve(DIR, outFileArg)
      fs.mkdirSync(path.dirname(outFile), { recursive: true })
      fs.writeFileSync(outFile, JSON.stringify(graph, null, 2))
      console.log(statsLine)
      console.log(`graph 已写入 ${outFile}`)
    }
  } else {
    console.log(JSON.stringify(graph, null, 2))
    console.error(statsLine)
  }
}

/** 供 graph-audit.mjs 复用的构建入口(不经过 CLI;图在模块加载时已构建) */
export function buildGraph() {
  return graph
}
