#!/usr/bin/env node
/**
 * UI 设计系统静态审计:六检 + 六闸门(G1-G6)+ 白名单防增量。
 *
 * 这是"原型库(src/components/ui/ 薄壳基板)+ 组合调用(页面仅组装薄壳)"架构的强制闸门:
 *   - 页面层禁止 import 已被 ui/ 薄壳覆盖的 Mantine 组件(有替身不用 = 违规);
 *     官方原子(ActionIcon/Button/Select/Tag/Tooltip/Text/Drawer/Modal/Switch 等)
 *     按 DESIGN-CONTRACT.md §3.1「官方直连,无需封装」直接使用,不拦。
 *     存量违规记录在白名单,差集非空即 fail——只拦"新增违规",存量豁免。
 *
 * 六检语义(见文件尾注释):
 *   ① 契约存在性(硬)     契约主表 CONTRACT_LIST 声明的薄壳组件必须在 src/components/ui/ 落地
 *   ② 页面禁已覆盖组件(硬) src/pages/** import 的 '@mantine/core' 名命中 COVERED 清单 = 违规;官方原子直连合规(DESIGN §3.1);与白名单 pages 求差,差集非空 = fail
 *   ③ 页面私有 CSS(警告) src/pages/** 私有 css 不在白名单 css → WARN;--strict 下 = fail
 *   ④ 双份组件检测(报告) src/components/ 根级 import antd 且用 Statistic 的"封装嫌疑";StatCard 复活 = fail
 *   ⑤ 文档↔实现 diff(报告) DESIGN-CONTRACT.md §3.3 / UI-TEMPLATES.md 表二 声明组件 vs ui/ 实际文件差集
 *   ⑥ 模板契约(混合)    TEMPLATE_CONTRACT 声明的复合模板须在 src/templates/ 落地;缺失 WARN(并行在途容忍),表外模板 FAIL
 *   附加 死组件警告:ui/ 组件在 src/ 中零引用 → WARN(如 TagBar / ScoreBadge / FormRow)
 *
 * 六闸门 G1-G6(docs/DESIGN-CONTRACT.md §2 布局机制契约;layout contract):
 *   G1 表格宽度范式(硬) DataTable 调用点未传 scrollX 且未 fillWidth → FAIL(白名单 g1_tables,Drawer/Modal/固定容器内调用)
 *   G2 分栏断点(硬)     CSS @media 宽度断点值出白名单 {480/576/768/991/992/1200/1600/1920} → FAIL;Grid span 对象含 lg/xl 键 → FAIL(白名单 g2_spans,组件内部微调)
 *   G3 尺寸混排(硬)     同一 JSX 行出现 ≥2 种不同 size="…" 字面量 → FAIL(白名单 g3_sizes)
 *   G4 窗口宽误用(硬)   window.innerWidth/outerWidth/screen.width → FAIL(白名单 g4_window,浮窗钳制/调试工具)
 *   G5 折叠审计(硬)     <Accordion JSX 使用 + 「查看全部」「显示更多」文案 → FAIL(白名单 g5_collapse,既有裁决保留项)
 *   G6 偏移双写(硬)     CSS scroll-margin-top: Npx 且同目录 tsx 存在 N±2 常量(带 scroll/offset/anchor 语境)→ FAIL(白名单 g6_offsets)
 *   全部默认拦截,豁免走 audit-whitelist.json 对应区块(只删不增纪律保持)。
 *
 * 用法: node scripts/design-system-audit.mjs [--strict]
 *   --strict  把检③页面私有 CSS 从 WARN 升级为 FAIL(验证增量闸门语义)
 * 环境变量: 无(白名单固定读取 scripts/audit-whitelist.json;缺失视为空数组)
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'frontend')
const SRC = path.join(ROOT, 'src')
const PAGES = path.join(SRC, 'pages')
const UI = path.join(SRC, 'components', 'ui')
const WHITELIST_FILE = path.join(path.dirname(fileURLToPath(import.meta.url)), 'audit-whitelist.json')
const DESIGN_MD = path.join(ROOT, '..', 'docs', 'DESIGN-CONTRACT.md')
const TEMPLATES_MD = path.join(ROOT, 'UI-TEMPLATES.md')
const STRICT = process.argv.includes('--strict')

/**
 * 契约主表:docs/DESIGN-CONTRACT.md §3.3 薄壳基板声明的组件(归档文档,契约表稳定)。
 * 来源:docs/DESIGN-CONTRACT.md §3.3(2026-08-13 入表;PageHeader / FormRow 缺失即 fail)。
 * 25 项为当前契约全集,新增组件必须同时入表与入目录。
 */
const CONTRACT_LIST = [
  'Toolbar', 'PageShell', 'DataTable', 'EmptyState', 'CardShell', 'IconTextButton',
  'TriStateGroup', 'HScroll', 'SkeletonBlock', 'InfiniteScrollToggle',
  'InfiniteScrollSentinel', 'MetricStat', 'CardState', 'SignalTag',
  'StatusTag', 'TaskProgress', 'DatasetSelector', 'FormulaText', 'PageHeader', 'FormRow',
  'SignalMomentCell', 'PageState', 'StatStrip', 'SegmentToolbar', 'ExprEditor',
]

/**
 * 模板契约表:UI-TEMPLATES.md §三 特殊模板的组合载体(src/templates/ 复合模板层)。
 * 来源:UI-TEMPLATES.md T1/T4/T5(2026-08-13;settings=T4 设置表单卡片组、dock=T5 工作台面板、
 * page-table=T1 页头+工具条+卡片表格)。
 * 新增复合模板必须同时入表与入目录;表外模板 = FAIL(防"旁路组合"),契约声明缺失 = WARN(并行批在途容忍)。
 */
const TEMPLATE_CONTRACT = ['settings', 'dock', 'page-table']

const whitelist = loadWhitelist()

/** 读取白名单;文件缺失视为空数组(降级为全部 WARN/FAIL,不中断审计) */
function loadWhitelist() {
  try {
    return JSON.parse(fs.readFileSync(WHITELIST_FILE, 'utf8'))
  } catch {
    return { pages: [], css: [] }
  }
}

/** 递归收集目录下全部文件(绝对路径) */
function walk(dir, out = []) {
  if (!fs.existsSync(dir)) return out
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name)
    if (entry.isDirectory()) walk(p, out)
    else out.push(p)
  }
  return out
}

/** 相对 base 的路径,统一正斜杠 */
const rel = (p, base) => path.relative(base, p).split(path.sep).join('/')

let failed = 0

/** 打印一行审计结论;FAIL 累加 failed(非 --strict 的 WARN 不计数) */
function emit(status, line, detail = []) {
  console.log(`[${status}] ${line}`)
  for (const d of detail) console.log(`    - ${d}`)
  if (status === 'FAIL') failed++
}

/* ---------- 检① 契约存在性(硬) ---------- */
const missing = CONTRACT_LIST.filter((name) => !fs.existsSync(path.join(UI, `${name}.tsx`)))
const s1Pass = CONTRACT_LIST.length - missing.length
emit(
  missing.length ? 'FAIL' : 'PASS',
  `检① 契约存在性: ${s1Pass}/${CONTRACT_LIST.length} 薄壳组件已落地`,
  missing.map((name) => `${name}: 契约声明但 src/components/ui/${name}.tsx 缺失`),
)

/* ---------- 检② 页面禁已覆盖组件(硬,增量制) ---------- */
/**
 * 已被 ui/ 薄壳覆盖的 Mantine 组件:页面直接 import = 违规(有替身不用)。
 * 覆盖关系(design migration 重映射,antd 已退出;以 src/components/ui/ 实际薄壳为准):
 * CardShell→Card,DataTable→Table,EmptyState(ui/)→Mantine EmptyState。
 * 注意:Badge/Indicator/Pagination/Loader/Skeleton/Select/Switch 等属 DESIGN-CONTRACT.md §3.1
 * 「官方直连(无需封装,直接用)」清单或 ui/ 无对应薄壳的原子组件,不在 COVERED 内;
 * 页面直接使用 Mantine Badge(36 处)等为官方原子直连,合规。
 */
const COVERED = ['Card', 'Table', 'EmptyState']

/** 提取 `import { A, B as C } from '@mantine/core'` 的具名集合(处理 as 别名) */
function mantineNamedImports(code) {
  const names = new Set()
  for (const m of code.matchAll(/import\s*\{([^}]*)\}\s*from\s*['"]@mantine\/core['"]/g)) {
    for (const part of m[1].split(',')) {
      const n = part.trim().split(/\s+as\s+/).pop().trim()
      if (n) names.add(n)
    }
  }
  return names
}

const barePages = walk(PAGES)
  .filter((f) => f.endsWith('.tsx'))
  .filter((f) => {
    const names = mantineNamedImports(fs.readFileSync(f, 'utf8'))
    return [...names].some((n) => COVERED.includes(n))
  })
  .map((f) => rel(f, PAGES))
const wlPages = new Set(whitelist.pages || [])
const pageDiff = barePages.filter((r) => !wlPages.has(r))
emit(
  pageDiff.length ? 'FAIL' : 'PASS',
  `检② 页面禁已覆盖组件: 违规页 ${barePages.length}(白名单 ${wlPages.size}),差集 ${pageDiff.length} 个页面直连被覆盖组件`,
  pageDiff,
)

/* ---------- 检③ 页面私有 CSS(警告制,--strict 变硬) ---------- */
const pageCss = walk(PAGES).filter((f) => f.endsWith('.css')).map((f) => rel(f, PAGES))
const wlCss = new Set(whitelist.css || [])
const privCss = pageCss.filter((r) => !wlCss.has(r))
const cssStatus = privCss.length === 0 ? 'PASS' : (STRICT ? 'FAIL' : 'WARN')
emit(
  cssStatus,
  `检③ 页面私有 CSS: ${privCss.length} 个页面级 css 未入白名单${STRICT && privCss.length ? '(--strict 按 FAIL 计)' : ''}`,
  privCss,
)

/* ---------- 检④ 双份组件检测(报告;StatCard 复活 = fail) ---------- */
const rootComps = fs.readdirSync(path.join(SRC, 'components'), { withFileTypes: true })
  .filter((e) => e.isFile() && e.name.endsWith('.tsx'))
  .map((e) => path.join(SRC, 'components', e.name))
const statSuspects = rootComps.filter((f) => {
  const c = fs.readFileSync(f, 'utf8')
  return /from\s+['"]antd['"]/.test(c) && /\bStatistic\b/.test(c)
})
const statCardRevived = fs.existsSync(path.join(SRC, 'components', 'StatCard.tsx'))
const s4Status = statCardRevived ? 'FAIL' : (statSuspects.length ? 'WARN' : 'PASS')
emit(
  s4Status,
  `检④ 双份组件检测: Statistic 封装嫌疑 ${statSuspects.length} 个,StatCard${statCardRevived ? ' 已复活(禁止)' : ' 已删除(双份已消灭)'}`,
  [...statSuspects.map((f) => rel(f, path.join(SRC, 'components'))), ...(statCardRevived ? ['components/StatCard.tsx 存在(禁止复活)'] : [])],
)

/* ---------- 检⑤ 文档↔实现 diff(报告) ---------- */
/** 提取 markdown 中 [startRe, 下一同级标题) 区间表格首列组件名(支持 `A` / `B` 合并行) */
function extractTableComponents(md, startRe, endRe) {
  const lines = fs.readFileSync(md, 'utf8').split('\n')
  const start = lines.findIndex((l) => startRe.test(l))
  if (start < 0) return []
  const names = new Set()
  for (const line of lines.slice(start + 1)) {
    if (endRe.test(line)) break
    if (!/^\|\s*`/.test(line)) continue
    // 首列 = 第一个 | 与第二个 | 之间,收集该列全部反引号组(`A` / `B` 形式各为独立组)
    const col = line.slice(line.indexOf('|') + 1, line.indexOf('|', line.indexOf('|') + 1))
    for (const bt of col.matchAll(/`([^`]+)`/g)) {
      for (const s of bt[1].split('/')) names.add(s.trim())
    }
  }
  return [...names]
}

const uiNames = walk(UI).filter((f) => f.endsWith('.tsx')).map((f) => path.basename(f, '.tsx'))

/** 计算双向差集 { docOnly, implOnly } */
function diffSet(doc, implList) {
  const implSet = new Set(implList)
  return {
    docOnly: doc.filter((n) => !implSet.has(n)),
    implOnly: implList.filter((n) => !doc.includes(n)),
  }
}

const designDiff = diffSet(extractTableComponents(DESIGN_MD, /### 3\.3/, /^### /), uiNames)
const templDiff = diffSet(extractTableComponents(TEMPLATES_MD, /^## 二、/, /^## /), uiNames)
const driftCount = designDiff.docOnly.length + designDiff.implOnly.length
  + templDiff.docOnly.length + templDiff.implOnly.length
const s5Detail = [
  ...designDiff.docOnly.map((n) => `DESIGN §3.3 文档有实现无: ${n}`),
  ...designDiff.implOnly.map((n) => `DESIGN §3.3 实现有文档无: ${n}`),
  ...templDiff.docOnly.map((n) => `UI-TEMPLATES 表二 文档有实现无: ${n}`),
  ...templDiff.implOnly.map((n) => `UI-TEMPLATES 表二 实现有文档无: ${n}`),
]
emit(
  driftCount ? 'WARN' : 'PASS',
  `检⑤ 文档↔实现 diff: 漂移 ${driftCount} 处(DESIGN §3.3 ${designDiff.docOnly.length + designDiff.implOnly.length} + UI-TEMPLATES 表二 ${templDiff.docOnly.length + templDiff.implOnly.length})`,
  s5Detail,
)

/* ---------- 检⑥ 模板契约(缺失 WARN,表外 FAIL) ---------- */
const TPL = path.join(SRC, 'templates')
const tplNames = fs.existsSync(TPL)
  ? walk(TPL).filter((f) => f.endsWith('.tsx')).map((f) => path.basename(f, '.tsx'))
  : []
const tplLanded = TEMPLATE_CONTRACT.filter((n) => tplNames.includes(n))
const tplMissing = TEMPLATE_CONTRACT.filter((n) => !tplNames.includes(n))
const tplOutside = tplNames.filter((n) => !TEMPLATE_CONTRACT.includes(n))
const s6Status = tplOutside.length ? 'FAIL' : (tplMissing.length ? 'WARN' : 'PASS')
emit(
  s6Status,
  `检⑥ 模板契约: ${tplLanded.length}/${TEMPLATE_CONTRACT.length} 已落地`,
  [
    ...tplOutside.map((n) => `${n}.tsx 未入契约表(新增复合模板必须入表,防旁路组合)`),
    ...tplMissing.map((n) => `${n}.tsx 契约声明但未落地(并行批在途容忍,集成时由主 agent 确认全存在)`),
  ],
)

/* ---------- 附加:死组件警告(ui/ 零引用;含 ui/ 内部互引,排除组件自身) ---------- */
const srcTsx = walk(SRC).filter((f) => f.endsWith('.tsx'))
const dead = uiNames.filter((name) => {
  // 注意:模板字符串内 \\b 才是正则单词边界(\b 会被字符串转义为退格符,曾致 FormRow 误报,2026-08-13 修复)
  const re = new RegExp(`components/ui/${name}['"]|<${name}\\b`)
  // 引用范围 = 全 src(含 ui/ 内部互引,如 DataTable→HScroll),仅排除组件自身文件
  return !srcTsx.some((f) => path.basename(f, '.tsx') !== name && re.test(fs.readFileSync(f, 'utf8')))
})
emit(
  dead.length ? 'WARN' : 'PASS',
  `附加 死组件: ui/ 下 ${dead.length} 个组件在 src/ 零引用`,
  dead,
)

/* ================= 闸门 G1-G6(docs/DESIGN-CONTRACT.md §2;layout contract) =================
 * 全部默认拦截(违例=FAIL),豁免走 audit-whitelist.json 对应区块(只删不增纪律保持)。
 * 检测均为静态启发式,口径与局限见各检注释与文件尾「闸门语义速查」。
 */
const wlG1 = new Set(whitelist.g1_tables || [])
const wlG2 = new Set(whitelist.g2_spans || [])
const wlG3 = new Set(whitelist.g3_sizes || [])
const wlG4 = new Set(whitelist.g4_window || [])
const wlG5 = new Set(whitelist.g5_collapse || [])
const wlG6 = new Set(whitelist.g6_offsets || [])

/** 白名单按 `rel:line` 精确匹配;带值违例(如 G3 的 size 组合)取 `rel:line` 前缀 */
const inWl = (wl, item) => wl.has(item) || [...wl].some((w) => item.startsWith(w))

/** JSX 片段提取(线性扫描):从 '<' 处起,到根自闭合 /> 或 </Tag> 闭合为止。
 *  局限:字符串/模板字面量内的 '<Tag>' 文本会被当作真实标签计深度(项目内罕见;误判只扩大片段,
 *  不影响 fillWidth/scrollX 存在性检查)。泛型 <DataTable<T>> 与 props 内比较符 > 均正确处理。 */
function jsxSnippet(code, from) {
  const tagRe = /<\/?[A-Za-z][\w.:-]*[^>]*?>|\/>/g
  tagRe.lastIndex = from
  let depth = 0
  let first = true
  let m
  while ((m = tagRe.exec(code))) {
    const tok = m[0]
    if (tok === '/>') {
      depth--
      if (depth === 0) return code.slice(from, m.index + 2)
      continue
    }
    const selfClose = tok.endsWith('/>')
    if (first) {
      if (selfClose) return code.slice(from, m.index + tok.length) // 根自闭合
      depth = 1 // 根开标签
    } else if (selfClose) {
      /* 后代自闭合:深度不变 */
    } else if (tok.startsWith('</')) {
      depth--
      if (depth === 0) return code.slice(from, m.index + tok.length)
    } else depth++
    first = false
  }
  return code.slice(from)
}

/** 剥离行内 // 注释与跨行块注释(逐行状态机,供文案/标识符类检测避免注释误报)。
 *  局限:字符串字面量内的 '//' 或 '/*'(反斜杠星号)会被误判为注释起点(URL 等),审计场景属宽松偏差(只漏报不误报)。 */
function stripComments(lines) {
  let inBlock = false
  return lines.map((ln) => {
    let out = ''
    for (let i = 0; i < ln.length; i++) {
      if (inBlock) {
        if (ln[i] === '*' && ln[i + 1] === '/') { inBlock = false; i++ }
        continue
      }
      if (ln[i] === '/' && ln[i + 1] === '/') break
      if (ln[i] === '/' && ln[i + 1] === '*') { inBlock = true; i++; continue }
      out += ln[i]
    }
    return out
  })
}

/* ---------- 检⑦ G1 表格宽度范式(硬) ---------- */
/**
 * 口径:M1 纪律(10 章 §1 M1)——DataTable 调用点必须满足其一:①数值 scrollX ②fillWidth
 *   ③false(容器 100% 宽且内容必不溢出,如抽屉/模态)④显式 max-content(须注释理由)。
 *   默认不传 = max-content 视为违例。实现:提取 <DataTable … 根闭合片段,无 fillWidth 且无 scrollX= 即违例。
 * 局限:纯文本启发式,不解析 TS 类型/宏;片段内 fillWidth 或 scrollX 出现即豁免(含被注释掉的 props 也会误豁免,
 *   属已知宽松偏差)。白名单 g1_tables 承载存量遗留(逐处理由见白名单 _g1)。
 */
const g1Violations = []
for (const f of srcTsx) {
  const code = fs.readFileSync(f, 'utf8')
  const re = /<DataTable\b/g
  let m
  while ((m = re.exec(code))) {
    const snip = jsxSnippet(code, m.index)
    if (/\bfillWidth\b/.test(snip) || /\bscrollX\s*=/.test(snip)) continue
    g1Violations.push(`${rel(f, SRC)}:${code.slice(0, m.index).split('\n').length}`)
  }
}
const g1Diff = g1Violations.filter((v) => !inWl(wlG1, v))
emit(
  g1Diff.length ? 'FAIL' : 'PASS',
  `检⑦ G1 表格宽度范式: 违例调用点 ${g1Violations.length}(白名单 ${wlG1.size}),差集 ${g1Diff.length} 处 DataTable 未传 scrollX/fillWidth`,
  g1Diff,
)

/* ---------- 检⑧ G2 分栏断点(硬) ---------- */
/**
 * 口径:M3(10 章 §1 M3)——分栏/堆叠决策禁媒体查询硬编码断点,容器宽驱动(ResearchSplit)。
 *   ①CSS @media 宽度断点(min/max-width)值必须在断点白名单 {480,576,768,991,992,1200,1600,1920}
 *     (991/992 并存由 responsive layout decision 裁决,两者合法);高度断点(max/min-height)不检(浮窗高度钳制合法)。
 *   ②Mantine Grid span 对象含 lg/xl 键 → 违例(M3:span 断点对象仅允许组件内部微调,禁配置|结果分栏)。
 * 局限:不区分断点语义(组件内部微调 vs 分栏决策),CSS 数值在名单内即放行;span 检测限
 *   `span={{…}}` 单对象字面量(≤200 字符,跨行容错),表达式形态(如 span={spanObj})不检。
 */
const BREAKPOINT_WL = new Set([480, 576, 768, 991, 992, 1200, 1600, 1920])
const g2CssViolations = []
for (const f of walk(SRC).filter((p) => p.endsWith('.css'))) {
  const lines = fs.readFileSync(f, 'utf8').split('\n')
  lines.forEach((ln, i) => {
    for (const bm of ln.matchAll(/\((min|max)-width\s*:\s*(\d+)px\)/g)) {
      if (!BREAKPOINT_WL.has(Number(bm[2]))) g2CssViolations.push(`${rel(f, SRC)}:${i + 1}(${bm[1]}-width:${bm[2]}px)`)
    }
  })
}
const g2SpanViolations = []
for (const f of srcTsx) {
  const code = fs.readFileSync(f, 'utf8')
  const re = /span=\{\{[\s\S]{0,200}?\}\}/g
  let m
  while ((m = re.exec(code))) {
    if (!/\b(?:lg|xl)\s*:/.test(m[0])) continue
    g2SpanViolations.push(`${rel(f, SRC)}:${code.slice(0, m.index).split('\n').length}(span lg/xl)`)
  }
}
const g2Diff = [...g2CssViolations, ...g2SpanViolations].filter((v) => !inWl(wlG2, v))
emit(
  g2Diff.length ? 'FAIL' : 'PASS',
  `检⑧ G2 分栏断点: 违例 ${g2CssViolations.length} CSS 断点 + ${g2SpanViolations.length} span-lg/xl(白名单 ${wlG2.size}),差集 ${g2Diff.length} 处`,
  g2Diff,
)

/* ---------- 检⑨ G3 尺寸混排(硬) ---------- */
/**
 * 口径:M2(10 章 §1 M2)——同一操作带内控件高度必须同档(基准 30px = xs)。静态启发式简化:
 *   同一 JSX 行出现 ≥2 种不同 size="…" 字符串字面量即违例(同文件不同操作带难判,取行级粒度,
 *   宁可误报人工审查,不放过同排混档)。compact-sm/compact-xs 亦计(非 xs 档混排同行为禁止)。
 * 局限:行级粒度——不同操作带在同一行(多段 JSX 一行)会误报;size 表达式(非字面量)不检。
 */
const g3Violations = []
for (const f of srcTsx) {
  const lines = fs.readFileSync(f, 'utf8').split('\n')
  lines.forEach((ln, i) => {
    const sizes = [...ln.matchAll(/size="([a-z]+)"/g)].map((x) => x[1])
    const uniq = [...new Set(sizes)]
    if (uniq.length >= 2) g3Violations.push(`${rel(f, SRC)}:${i + 1}(${uniq.join('/')})`)
  })
}
const g3Diff = g3Violations.filter((v) => !inWl(wlG3, v))
emit(
  g3Diff.length ? 'FAIL' : 'PASS',
  `检⑨ G3 尺寸混排: 混排行 ${g3Violations.length}(白名单 ${wlG3.size}),差集 ${g3Diff.length} 行含 ≥2 种 size`,
  g3Diff,
)

/* ---------- 检⑩ G4 窗口宽误用(硬) ---------- */
/**
 * 口径:M4(10 章 §1 M4)——视口宽(window.innerWidth/outerWidth/screen.width)仅允许浮窗/弹层钳制
 *   与调试工具;布局宽度一律容器实测(useElementSize)。命中即违例,白名单 g4_window 承载豁免。
 * 局限:纯标识符匹配,不区分用途(浮窗钳制 vs 布局计算)——豁免由白名单逐处背书。
 */
const g4Violations = []
for (const f of [...walk(SRC).filter((p) => p.endsWith('.tsx')), ...walk(SRC).filter((p) => p.endsWith('.ts'))]) {
  const lines = fs.readFileSync(f, 'utf8').split('\n')
  stripComments(lines).forEach((ln, i) => {
    if (/\bwindow\.(?:innerWidth|outerWidth)\b|\bscreen\.width\b/.test(ln)) {
      g4Violations.push(`${rel(f, SRC)}:${i + 1}`)
    }
  })
}
const g4Diff = g4Violations.filter((v) => !inWl(wlG4, v))
emit(
  g4Diff.length ? 'FAIL' : 'PASS',
  `检⑩ G4 窗口宽误用: 使用点 ${g4Violations.length}(白名单 ${wlG4.size}),差集 ${g4Diff.length} 处 window/screen 宽度`,
  g4Diff,
)

/* ---------- 检⑪ G5 折叠审计(硬) ---------- */
/**
 * 口径:flat-list policy 平铺执行——折叠(Accordion/「查看全部」「显示更多」)违反平铺优先,
 *   保留须注释理由并入白名单 g5_collapse。实现:JSX 实际使用 <Accordion(注释不算)+
 *   「查看全部」「显示更多」文案命中。
 * 局限:文案命中含非折叠语义(如 JobBar「查看全部任务」为导航链接),由白名单逐处背书。
 */
const g5Violations = []
for (const f of srcTsx) {
  const lines = fs.readFileSync(f, 'utf8').split('\n')
  stripComments(lines).forEach((ln, i) => {
    if (/<Accordion\b/.test(ln) || /查看全部|显示更多/.test(ln)) {
      g5Violations.push(`${rel(f, SRC)}:${i + 1}`)
    }
  })
}
const g5Diff = g5Violations.filter((v) => !inWl(wlG5, v))
emit(
  g5Diff.length ? 'FAIL' : 'PASS',
  `检⑪ G5 折叠审计: 折叠使用点 ${g5Violations.length}(白名单 ${wlG5.size}),差集 ${g5Diff.length} 处`,
  g5Diff,
)

/* ---------- 检⑫ G6 偏移双写(硬) ---------- */
/**
 * 口径:M4(10 章 §1 M4)——「同一事实两个值」禁止:CSS scroll-margin-top 数值与 TS 侧数值常量并存。
 *   实现:css 中 scroll-margin-top: Npx(calc/var 不计,已收敛)——同目录 tsx 中若存在 (N±2) 数值
 *   且所在行含 scroll/offset/anchor/sticky 语境关键词 → 违例。
 * 局限:TS 侧常量命名不遵循语境关键词时漏检(反向宽松);跨目录共享常量不检(同目录限定)。
 */
const g6Violations = []
for (const f of walk(SRC).filter((p) => p.endsWith('.css'))) {
  const cssDir = path.dirname(f)
  const cssLines = fs.readFileSync(f, 'utf8').split('\n')
  const peers = walk(cssDir).filter((p) => p.endsWith('.tsx'))
  cssLines.forEach((ln, i) => {
    for (const sm of ln.matchAll(/scroll-margin-top\s*:\s*(\d+)px/g)) {
      const n = Number(sm[1])
      const ctxRe = new RegExp(`\\b(?:scrollMargin|scroll-margin|offsetTop|offset|anchor|ANCHOR|sticky)\\b[^\\n]{0,40}?\\b(${n - 2}|${n - 1}|${n}|${n + 1}|${n + 2})\\b|\\b(${n - 2}|${n - 1}|${n}|${n + 1}|${n + 2})\\b[^\\n]{0,40}?(?:scrollMargin|scroll-margin|offsetTop|offset|anchor|ANCHOR|sticky)\\b`)
      const hit = peers.some((p) => ctxRe.test(fs.readFileSync(p, 'utf8')))
      if (hit) g6Violations.push(`${rel(f, SRC)}:${i + 1}(scroll-margin-top:${n}px 与 TS 常量并存)`)
    }
  })
}
const g6Diff = g6Violations.filter((v) => !inWl(wlG6, v))
emit(
  g6Diff.length ? 'FAIL' : 'PASS',
  `检⑫ G6 偏移双写: 违例 ${g6Violations.length}(白名单 ${wlG6.size}),差集 ${g6Diff.length} 处 CSS/TS 偏移值并存`,
  g6Diff,
)

/* ---------- Score 汇总 ---------- */
console.log(
  `Score: S1 存在性 ${s1Pass}/${CONTRACT_LIST.length} | S2 裸Mantine页 ${barePages.length}(白名单 ${wlPages.size}) | `
  + `S3 私有css ${privCss.length} | S4 双份嫌疑 ${statSuspects.length} | S5 文档漂移 ${driftCount} | T6 模板 ${tplLanded.length}/${TEMPLATE_CONTRACT.length} | 死组件 ${dead.length} | `
  + `G1 表格宽 ${g1Violations.length} | G2 分栏断点 ${g2CssViolations.length + g2SpanViolations.length} | G3 尺寸混排 ${g3Violations.length} | `
  + `G4 窗口宽 ${g4Violations.length} | G5 折叠 ${g5Violations.length} | G6 偏移双写 ${g6Violations.length}`,
)
console.log(failed ? `审计未通过: ${failed} 项 FAIL` : '审计通过')
process.exit(failed ? 1 : 0)

/*
 * 六检语义速查(防语义漂移):
 * ① 契约存在性:CONTRACT_LIST = docs/DESIGN-CONTRACT.md §3.3 薄壳基板,缺文件即 fail——契约与实现强一致。
 * ② 页面禁已覆盖组件:页面 import 的 '@mantine/core' 名命中 COVERED(Card/Table/EmptyState,
 *   均有 ui/ 薄壳替身)= 违规;官方原子直连合规(DESIGN §3.1 官方优先)。白名单承载存量违规页,差集=新增违规,非空 fail。
 * ③ 页面私有 CSS:页面 css 越过了令牌体系,默认 WARN(存量),--strict 下 fail(证明增量闸门可收紧)。
 * ④ 双份组件检测:根级组件再包 Statistic = 与 ui/MetricStat 双份,报告不 fail;StatCard 已删除,复活即 fail。
 * ⑤ 文档↔实现 diff:DESIGN-CONTRACT.md §3.3 / UI-TEMPLATES.md 表二 与 ui/ 目录双向差集,只报告(文档同步卡消费)。
 * ⑥ 模板契约:TEMPLATE_CONTRACT = UI-TEMPLATES.md §三 特殊模板的组合载体(src/templates/ 复合模板层),
 *   缺失 WARN(并行批在途容忍,集成时主 agent 确认),表外模板 FAIL(新增复合模板必须入表,防"旁路组合")。
 * 附加 死组件:ui/ 组件零引用 = 未接入组合调用,输出 WARN 提示(零消费属预期时人工确认)。
 *
 * 白名单制:audit-whitelist.json 承载存量豁免(pages 违规页 / css 私有 / g1_tables / g2_spans /
 * g3_sizes / g4_window / g5_collapse / g6_offsets),差集非空即 fail——只拦新增违规,存量豁免。
 * templates/ 层职责:复合模板允许组合 antd 原子与 ui/ 薄壳(它本身即"薄壳组合"的载体);
 *   页面层仍禁已覆盖组件(检②只管 src/pages/**,模板层不在其列,但表外模板由检⑥兜底)。
 *
 * 闸门语义速查(docs/DESIGN-CONTRACT.md §2,防语义漂移):
 * G1 表格宽度范式:DataTable 调用点缺 scrollX/fillWidth = 默认 max-content 违例(M1 纪律④例外经白名单逐处背书)。
 * G2 分栏断点:CSS 宽度断点值出白名单集 / Grid span 对象含 lg/xl 键(禁配置|结果分栏;字段对等内部微调豁免)。
 * G3 尺寸混排:同 JSX 行 ≥2 种 size 字面量(M2 同排同档;行级粒度,跨操作带同行为已知误报面)。
 * G4 窗口宽误用:window.innerWidth/outerWidth/screen.width(布局宽度禁视口测量,浮窗/调试豁免)。
 * G5 折叠审计:<Accordion 与「查看全部」「显示更多」(平铺优先,折叠保留须白名单注释理由)。
 * G6 偏移双写:CSS scroll-margin-top 数值与同目录 TS 常量并存(同一事实两值,应收敛 calc(var))。
 * 运行时对应:G7 表格宽度断言 / G8 堆叠行为断言 在 scripts/ui-audit.mjs(+G7/+G8 独立计数)。
 */
