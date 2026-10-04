/**
 * R1 契约闭合:前端 apiEndpoints.fields(消费字段)与后端 contract.fields(契约字段)差集。
 *   - 前端消费了契约没有的字段 → FAIL(契约漂移:前端在消费后端未声明的字段)
 *   - 契约有前端未消费的字段 → WARN(汇总,可能是契约未收紧)
 *   - 契约存在但无逐端点响应字段(后端路由无 response_model)→ FAIL(契约对账不可核);
 *     T-112 阶段二已为热点路由补 response_model,契约逐路径可核(硬对账生效)
 *   - URL 匹配(T-112):段数最接近的契约 path 优先,避免父/子路径互抢
 * contract 结构宽容解析:数组 / {endpoints:[{path,fields}]} / {paths:[...]} /
 *   {[path]: [fields]} / schema_dump 实际形态 {[path]: {METHOD: {fields: {...}}}}(逐路径响应模型)。
 * 端点匹配:api 函数调用点的 URL 字面量(/stocks/... )与契约 path 对齐(去掉 :param,前缀匹配,尽力)。
 * contract 不存在 → 单条 WARN 跳过(由编排器补 emit)。
 */
export const name = '契约闭合'

/** 白名单前缀匹配(精确或 item 前缀) */
const inWl = (wl, item) => wl.has(item) || [...wl].some((w) => item.startsWith(w))

/** 规范化 URL/契约 path:去参数占位、尾斜杠,统一小写前缀 */
const norm = (p) => String(p).replace(/:[^/]+/g, ':p').replace(/\/+$/, '')
/**
 * 契约 path 与调用点 URL 是否对齐(T-112 修复):
 * - 精确相等优先;否则前缀匹配(契约 path 可带参数,url 是其前缀或反之);
 * - 子路径不被父路径误抢(/signals/events/page 不会被 /signals/events 匹配),
 *   通过「取所有命中契约 path 中最长者」在调用处解决(见 R1.run 的 _pickCep)。
 */
const urlMatch = (url, cpath) => {
  const u = norm(url)
  const c = norm(cpath)
  if (u === c) return true
  const seg = (p) => p.split('/').filter(Boolean).length
  /* 前缀匹配仅允许段级边界:u 是 c 的延续(更多段)或 c 是 u 的延续 */
  if (u.startsWith(c + '/') || c.startsWith(u + '/')) return true
  return false
}

const isPlainObject = (o) => o && typeof o === 'object' && !Array.isArray(o)

/** 从契约 path 条目值提取字段名列表:兼容数组、{fields:[...]}、{fields:{...}}、{METHOD:{fields:...}} */
function extractFields(value) {
  if (Array.isArray(value)) return value.map(String)
  if (!isPlainObject(value)) return []
  if (Array.isArray(value.fields)) return value.fields.map(String)
  if (isPlainObject(value.fields)) return Object.keys(value.fields)
  const out = new Set()
  for (const v of Object.values(value)) {
    if (isPlainObject(v)) {
      if (Array.isArray(v.fields)) v.fields.forEach((f) => out.add(String(f)))
      else if (isPlainObject(v.fields)) Object.keys(v.fields).forEach((f) => out.add(f))
    }
  }
  return [...out]
}

/** 提取契约端点列表:[{path, fields:[]}] —— 宽容解析多种结构 */
function extractContractEndpoints(contract) {
  const out = []
  const push = (pathKey, fields) => {
    if (Array.isArray(fields)) out.push({ path: pathKey, fields: fields.map(String) })
  }
  if (Array.isArray(contract)) {
    for (const e of contract) if (e && e.fields) push(e.path ?? e.url ?? e.name ?? '', e.fields)
  } else if (contract && typeof contract === 'object') {
    if (Array.isArray(contract.endpoints)) {
      for (const e of contract.endpoints) if (e && e.fields) push(e.path ?? e.url ?? '', e.fields)
    } else if (Array.isArray(contract.paths)) {
      for (const e of contract.paths) if (e && e.fields) push(e.path ?? e.url ?? '', e.fields)
    } else {
      /* { path: [fields] } 或 { path: {METHOD: {fields}} } 形态;跳过 _meta/_schemas 等元段 */
      for (const [k, v] of Object.entries(contract)) {
        if (k === 'endpoints' || k === 'paths' || k === 'meta' || k.startsWith('_')) continue
        const fields = extractFields(v)
        if (fields.length) push(k, fields)
      }
    }
  }
  return out
}

export function run(graph, contract, whitelist) {
  const wl = new Set(whitelist.r1_fields || [])
  const issues = []
  if (!contract) return { name, summary: '契约文件缺失,跳过', counts: { fail: 0, total: 0 }, issues: [] }

  const eps = extractContractEndpoints(contract)
  if (!eps.length) {
    /* 契约存在但无逐端点响应字段:后端路由无 response_model,逐路径对账不可核。
       硬对账:不可核即 FAIL(T-112 后 r1_uncov 豁免已移除,契约逐路径可核)。 */
    const meta = contract?._meta ?? contract?.meta ?? null
    const note = meta && meta.paths_total > 0
      ? `后端 ${meta.paths_total} 个端点仅 ${meta.paths_with_schema} 个有响应 schema(无 response_model),契约对账不可核`
      : '契约无可用端点(fields 缺失),跳过'
    const issues = meta && meta.paths_total > 0
      ? [{ status: 'FAIL', loc: '_meta', detail: note }]
      : []
    return { name, summary: note, counts: { fail: issues.length, total: 0 }, issues }
  }

  let total = 0
  let backOnly = 0
  for (const ep of graph.apiEndpoints || []) {
    /* 取与 URL 段数最接近的契约 path(T-112 修复):
       - /signals/events/page 不被 /signals/events 父路径误抢;
       - /market/watchlist/groups 不被更深子路径 groups/:p/items/:p 误抢。
       段数差异最小优先;同差取更长(父契约优先于祖契约)。 */
    const cep = (ep.urls || [])
      .map((u) => {
        const nu = norm(u)
        const nuSegs = nu.split('/').filter(Boolean).length
        const cands = eps.filter((e) => urlMatch(u, e.path))
          .map((e) => ({ e, diff: Math.abs(nuSegs - norm(e.path).split('/').filter(Boolean).length) }))
          .sort((a, b) => a.diff - b.diff || b.e.path.length - a.e.path.length)
        return cands[0]?.e
      })
      .filter(Boolean)[0]
    if (!cep) continue
    const cf = new Set(cep.fields)
    for (const f of ep.fields || []) {
      total++
      const parts = f.split('.')
      /* 前端字段拆点:裸名/复合名/任一段在契约即视为已声明 */
      const declared = cf.has(f) || cf.has(parts[parts.length - 1]) || parts.some((p) => cf.has(p))
      if (declared) continue
      const loc = `${ep.file}:${ep.line}`
      if (!inWl(wl, loc)) issues.push({ status: 'FAIL', loc, detail: `字段「${f}」被前端消费,但契约端点 ${cep.path} 未声明` })
    }
    /* 后端有前端无 → WARN(汇总计数,不逐条) */
    for (const f of cf) {
      const used = (ep.fields || []).some((x) => x === f || x.endsWith('.' + f) || x.split('.')[0] === f || x.includes('.' + f))
      if (!used) backOnly++
    }
  }

  const fail = issues.filter((i) => i.status === 'FAIL').length
  return {
    name,
    summary: `前端消费字段 ${total} 项(端点匹配 ${eps.length} 契约端点),前端独有 FAIL ${fail} 项,后端独有字段 ${backOnly} 项(WARN)`,
    counts: { fail, total, backOnly },
    issues,
  }
}
