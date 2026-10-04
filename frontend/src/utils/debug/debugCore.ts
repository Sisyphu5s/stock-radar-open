/**
 * Debug 模式纯函数层：全部检测/几何逻辑的单一事实源。
 *
 * DOM 隔离约定：
 * - rect 相关函数接受 Element（只依赖 getBoundingClientRect / offsetParent / getComputedStyle 标准接口）；
 * - 几何函数（rectsIntersect / snapToEdges 等）只接受 Rect，node 断言脚本可直接编译后测真实实现。
 * 常量与断点统一在此声明，debugLayout（薄封装）与 fe-dbg-ui/fe-dbg-mount 从此处消费。
 */
import type {
  BoxModel,
  BoxQuad,
  BreakpointCross,
  CollisionPair,
  DebugReport,
  OverflowInfo,
  PositionedInfo,
  QuadPoint,
  Rect,
} from './debugTypes'

/** antd 断点全集（响应式唯一事实源，与前端主题规范 一致） */
export const BREAKPOINTS = [480, 576, 768, 992, 1200, 1600, 1920]

/** 贴边判定阈值：与可视容器左/上边距 < 此值视为紧贴边缘 */
export const EDGE_MARGIN = 3

/** 贴边元素标记类（debugLayout visual 模式打标 + UI 审计取色） */
export const FLUSH_CLS = 'sr-debug-flush'

/** 元素选择器描述：`tag.cls#id`，截断 60 字符 */
export function describe(el: Element): string {
  const tag = typeof el.tagName === 'string' ? el.tagName.toLowerCase() : '?'
  const cls = el.classList && el.classList.length > 0 ? `.${el.classList[0]}` : ''
  const id = el.id ? `#${el.id}` : ''
  const s = `${tag}${cls}${id}`
  return s.length > 60 ? `${s.slice(0, 57)}…` : s
}

/** getBoundingClientRect 归一：补齐 right/bottom（浮点小数保留原样，比较时注意精度） */
export function rectOf(el: Element): Rect {
  const r = el.getBoundingClientRect()
  return {
    left: r.left,
    top: r.top,
    right: r.right,
    bottom: r.bottom,
    width: r.width,
    height: r.height,
  }
}

/** 元素三种坐标系矩形：viewport（相对视口）/ offsetParent（相对定位祖先）/ doc（视口+滚动偏移） */
export function relRectOf(el: Element): { viewport: Rect; doc: Rect; offsetParent: Rect | null } {
  const viewport = rectOf(el)
  const sx = typeof window !== 'undefined' ? window.scrollX : 0
  const sy = typeof window !== 'undefined' ? window.scrollY : 0
  const doc: Rect = {
    ...viewport,
    left: viewport.left + sx,
    top: viewport.top + sy,
    right: viewport.right + sx,
    bottom: viewport.bottom + sy,
  }
  // offsetParent 是 HTMLElement 属性（DOM 中所有 Element 均实际存在），用类型断言取
  const op = 'offsetParent' in el ? (el as HTMLElement).offsetParent : null
  const offsetParent = op instanceof Element ? rectOf(op) : null
  return { viewport, doc, offsetParent }
}

/** 定位祖先链（relative/absolute/fixed/sticky），含 fixed；offsetX/Y=祖先相对其 offsetParent 的位移 */
export function offsetChainOf(el: Element): { el: string; offsetX: number; offsetY: number }[] {
  const chain: { el: string; offsetX: number; offsetY: number }[] = []
  let cur: HTMLElement | null = el.parentElement
  while (cur instanceof HTMLElement) {
    const pos = getComputedStyle(cur).position
    if (pos === 'absolute' || pos === 'fixed' || pos === 'relative' || pos === 'sticky') {
      chain.push({ el: describe(cur), offsetX: cur.offsetLeft, offsetY: cur.offsetTop })
    }
    cur = cur.parentElement
  }
  return chain
}

/** 解析 computed px 值（invalid/auto 兜底 0） */
function px(v: string): number {
  const n = parseFloat(v)
  return Number.isFinite(n) ? n : 0
}

/* ---------------- 盒模型：getBoxQuads 真实几何（优先）+ 内缩算术（旧浏览器回退） ---------------- */

/** DOMQuad 最小结构：TS lib.dom 未声明 Element.getBoxQuads，局部接口适配（只消费 p1..p4 四角） */
interface DOMQuadLike {
  p1: { x: number; y: number }
  p2: { x: number; y: number }
  p3: { x: number; y: number }
  p4: { x: number; y: number }
}

/** getBoxQuads 的 box 参数与盒模型层一一对应 */
type BoxLayer = 'margin' | 'border' | 'padding' | 'content'
const BOX_LAYERS: readonly BoxLayer[] = ['margin', 'border', 'padding', 'content']

/** 能力检测：getBoxQuads 可用性（模块加载时求值；readBoxQuads 内再做动态判定以支持测试注入/移除） */
export const HAS_GET_BOX_QUADS: boolean =
  typeof Element !== 'undefined'
  && typeof (Element.prototype as { getBoxQuads?: unknown }).getBoxQuads === 'function'

/** 单层 DOMQuad → BoxQuad：corners 直取四角，rect=四角 min/max 包围矩形 */
export function quadToBoxQuad(q: DOMQuadLike): BoxQuad {
  const p1: QuadPoint = { x: q.p1.x, y: q.p1.y }
  const p2: QuadPoint = { x: q.p2.x, y: q.p2.y }
  const p3: QuadPoint = { x: q.p3.x, y: q.p3.y }
  const p4: QuadPoint = { x: q.p4.x, y: q.p4.y }
  const xs = [p1.x, p2.x, p3.x, p4.x]
  const ys = [p1.y, p2.y, p3.y, p4.y]
  const left = Math.min(...xs)
  const right = Math.max(...xs)
  const top = Math.min(...ys)
  const bottom = Math.max(...ys)
  return {
    corners: [p1, p2, p3, p4],
    rect: { left, top, right, bottom, width: right - left, height: bottom - top },
  }
}

/** 读取四层真实几何（margin/border/padding/content）；能力不可用或任一层读不到返回 null */
export function readBoxQuads(el: Element): BoxQuad[] | null {
  const gqb = (el as Element & { getBoxQuads?: (opts: { box: BoxLayer }) => DOMQuadLike[] }).getBoxQuads
  if (typeof gqb !== 'function') return null
  const out: BoxQuad[] = []
  for (const box of BOX_LAYERS) {
    const list = gqb.call(el, { box })
    if (!Array.isArray(list) || list.length === 0) return null
    out.push(quadToBoxQuad(list[0]))
  }
  return out
}

/** 轴对齐 Rect → BoxQuad（回退路径输出；corners=rect 四角） */
function quadFromRect(rect: Rect): BoxQuad {
  return {
    corners: [
      { x: rect.left, y: rect.top },
      { x: rect.right, y: rect.top },
      { x: rect.right, y: rect.bottom },
      { x: rect.left, y: rect.bottom },
    ],
    rect,
  }
}

/** 层是否轴对齐：顶边水平（p1.y=p2.y）且左边垂直（p1.x=p4.x） */
function isAxisAligned(q: BoxQuad): boolean {
  const [p1, p2, , p4] = q.corners
  return p1.y === p2.y && p1.x === p4.x
}

/** 盒模型纯函数核心：四层 quad → BoxModel（包围矩形 + transformed 检测）。
 *  quads=null 时按 el 的 border rect + computed style 走内缩算术（仅旧浏览器回退，transform 不准确）。 */
export function boxModelFromQuads(quads: BoxQuad[] | null, el?: Element): BoxModel {
  if (quads && quads.length === 4) {
    const [margin, border, padding, content] = quads
    const transformed = [margin, border, padding, content].some((q) => !isAxisAligned(q))
    return { margin, border, padding, content, transformed }
  }
  // 回退：border=getBoundingClientRect，margin/padding/content 逐层内缩
  const border = rectOf(el!)
  const cs = getComputedStyle(el!)
  const m = { l: px(cs.marginLeft), t: px(cs.marginTop), r: px(cs.marginRight), b: px(cs.marginBottom) }
  const b = { l: px(cs.borderLeftWidth), t: px(cs.borderTopWidth), r: px(cs.borderRightWidth), b: px(cs.borderBottomWidth) }
  const p = { l: px(cs.paddingLeft), t: px(cs.paddingTop), r: px(cs.paddingRight), b: px(cs.paddingBottom) }
  const margin: Rect = {
    left: border.left - m.l,
    top: border.top - m.t,
    right: border.right + m.r,
    bottom: border.bottom + m.b,
    width: border.width + m.l + m.r,
    height: border.height + m.t + m.b,
  }
  const padding: Rect = {
    left: border.left + b.l,
    top: border.top + b.t,
    right: border.right - b.r,
    bottom: border.bottom - b.b,
    width: border.width - b.l - b.r,
    height: border.height - b.t - b.b,
  }
  const content: Rect = {
    left: padding.left + p.l,
    top: padding.top + p.t,
    right: padding.right - p.r,
    bottom: padding.bottom - p.b,
    width: padding.width - p.l - p.r,
    height: padding.height - p.t - p.b,
  }
  return {
    margin: quadFromRect(margin),
    border: quadFromRect(border),
    padding: quadFromRect(padding),
    content: quadFromRect(content),
    transformed: false,
  }
}

/** CSS 盒模型四层分解：优先 getBoxQuads 真实几何（含 transform），旧内缩算法作回退 */
export function boxModelOf(el: Element): BoxModel {
  if (HAS_GET_BOX_QUADS) {
    const quads = readBoxQuads(el)
    if (quads) return boxModelFromQuads(quads)
  }
  return boxModelFromQuads(null, el)
}

/** 两矩形相交判定：areaPct=交叠面积/较小者面积（内含时=1，分离时=0） */
export function rectsIntersect(a: Rect, b: Rect): { hit: boolean; overlap: Rect | null; areaPct: number } {
  const left = Math.max(a.left, b.left)
  const top = Math.max(a.top, b.top)
  const right = Math.min(a.right, b.right)
  const bottom = Math.min(a.bottom, b.bottom)
  const width = right - left
  const height = bottom - top
  if (width <= 0 || height <= 0) return { hit: false, overlap: null, areaPct: 0 }
  const overlap: Rect = { left, top, right, bottom, width, height }
  const aArea = a.width * a.height
  const bArea = b.width * b.height
  const smaller = Math.min(aArea, bArea)
  return { hit: true, overlap, areaPct: smaller > 0 ? (width * height) / smaller : 0 }
}

/** 一位小数取整，消除浮点噪音 */
function round1(n: number): number {
  return Math.round(n * 10) / 10
}

/** 元素相对视口四方向溢出（vw/vh 可注入，默认 window.innerWidth/Height） */
export function viewportOverflow(el: Element, vw?: number, vh?: number): OverflowInfo[] {
  const r = rectOf(el)
  const width = vw ?? (typeof window !== 'undefined' ? window.innerWidth : r.width)
  const height = vh ?? (typeof window !== 'undefined' ? window.innerHeight : r.height)
  const d = describe(el)
  const out: OverflowInfo[] = []
  if (r.left < 0) out.push({ el: d, dir: 'left', px: round1(-r.left) })
  if (r.right > width) out.push({ el: d, dir: 'right', px: round1(r.right - width) })
  if (r.top < 0) out.push({ el: d, dir: 'top', px: round1(-r.top) })
  if (r.bottom > height) out.push({ el: d, dir: 'bottom', px: round1(r.bottom - height) })
  return out
}

/** 元素 vs 最近滚动容器（overflow auto/scroll）content 区（border box 内缩 clientLeft/Top）溢出 */
export function containerOverflow(el: Element): OverflowInfo[] {
  const r = rectOf(el)
  const d = describe(el)
  let c: HTMLElement | null = el.parentElement
  while (c instanceof HTMLElement) {
    const cs = getComputedStyle(c)
    const scrollable = cs.overflowX === 'auto' || cs.overflowX === 'scroll'
      || cs.overflowY === 'auto' || cs.overflowY === 'scroll'
    if (scrollable) {
      const cr = c.getBoundingClientRect()
      const content: Rect = {
        left: cr.left + c.clientLeft,
        top: cr.top + c.clientTop,
        right: cr.left + c.clientLeft + c.clientWidth,
        bottom: cr.top + c.clientTop + c.clientHeight,
        width: c.clientWidth,
        height: c.clientHeight,
      }
      const out: OverflowInfo[] = []
      if (r.left < content.left) out.push({ el: d, dir: 'left', px: round1(content.left - r.left) })
      if (r.right > content.right) out.push({ el: d, dir: 'right', px: round1(r.right - content.right) })
      if (r.top < content.top) out.push({ el: d, dir: 'top', px: round1(content.top - r.top) })
      if (r.bottom > content.bottom) out.push({ el: d, dir: 'bottom', px: round1(r.bottom - content.bottom) })
      return out
    }
    c = c.parentElement
  }
  return []
}

/** 元素边界与 BREAKPOINTS 相交（left < bp < right 严格内部跨越） */
export function breakpointCross(el: Element): BreakpointCross[] {
  const r = rectOf(el)
  const d = describe(el)
  const out: BreakpointCross[] = []
  for (const bp of BREAKPOINTS) {
    if (r.left < bp && bp < r.right) out.push({ el: d, bp })
  }
  return out
}

type Guide = { dir: 'left' | 'right' | 'top' | 'bottom' | 'vmid' | 'hmid'; value: number }

/** 吸附：pos 到 targets 边缘（left/right/vmid, top/bottom/hmid）距离 ≤threshold(默认4)px 时吸附，
 *  x 系（left/right/vmid）与 y 系（top/bottom/hmid）各独立取 |delta| 最小的一条吸附并记入 guides。 */
export function snapToEdges(
  pos: { x: number; y: number },
  targets: Rect[],
  threshold = 4,
): { x: number; y: number; guides: Guide[] } {
  const candidates: { dir: Guide['dir']; value: number; delta: number }[] = []
  for (const t of targets) {
    candidates.push({ dir: 'left', value: t.left, delta: pos.x - t.left })
    candidates.push({ dir: 'right', value: t.right, delta: pos.x - t.right })
    candidates.push({ dir: 'vmid', value: t.left + t.width / 2, delta: pos.x - (t.left + t.width / 2) })
    candidates.push({ dir: 'top', value: t.top, delta: pos.y - t.top })
    candidates.push({ dir: 'bottom', value: t.bottom, delta: pos.y - t.bottom })
    candidates.push({ dir: 'hmid', value: t.top + t.height / 2, delta: pos.y - (t.top + t.height / 2) })
  }
  const isX = (d: Guide['dir']) => d === 'left' || d === 'right' || d === 'vmid'
  const nearest = (list: typeof candidates) => {
    const near = list.filter((c) => Math.abs(c.delta) <= threshold)
    if (near.length === 0) return null
    return near.reduce((a, b) => (Math.abs(a.delta) <= Math.abs(b.delta) ? a : b))
  }
  let x = pos.x
  let y = pos.y
  const guides: Guide[] = []
  const bx = nearest(candidates.filter((c) => isX(c.dir)))
  if (bx) {
    x = bx.value
    guides.push({ dir: bx.dir, value: bx.value })
  }
  const by = nearest(candidates.filter((c) => !isX(c.dir)))
  if (by) {
    y = by.value
    guides.push({ dir: by.dir, value: by.value })
  }
  return { x, y, guides }
}

/** 全树 fixed/absolute 定位元素（root 可限定子树，默认 document） */
export function collectPositioned(root: ParentNode = document): HTMLElement[] {
  if (typeof document === 'undefined') return []
  const base: ParentNode = root ?? document
  const out: HTMLElement[] = []
  if (typeof base.querySelectorAll !== 'function') return out
  base.querySelectorAll<HTMLElement>('*').forEach((el) => {
    const p = getComputedStyle(el).position
    if (p === 'fixed' || p === 'absolute') out.push(el)
  })
  return out
}

/** 是否紧贴可视父容器边缘（边距 < EDGE_MARGIN）且带可见视觉（border/背景/非文本内联元素） */
export function isFlushable(el: HTMLElement): boolean {
  if (el.classList.contains(FLUSH_CLS)) return false
  const rect = el.getBoundingClientRect()
  if (rect.width < 1 || rect.height < 1) return false
  const cs = getComputedStyle(el)
  if (cs.display === 'none' || cs.visibility === 'hidden') return false
  // 可视父容器：offsetParent（定位祖先）优先，fixed/异常布局退回父元素
  const container = (el.offsetParent as HTMLElement | null) ?? el.parentElement
  if (!container) return false
  const crect = container.getBoundingClientRect()
  if (rect.left - crect.left >= EDGE_MARGIN && rect.top - crect.top >= EDGE_MARGIN) return false
  const borderW = parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderTopWidth)
  const hasBg = cs.backgroundColor !== 'transparent' && cs.backgroundColor !== 'rgba(0, 0, 0, 0)'
  const isInlineBox = cs.display === 'inline-block' || cs.display === 'inline-flex' || cs.display === 'table-cell'
  return borderW > 0 || hasBg || isInlineBox
}

/** 聚合检查（只读，无 DOM 副作用）：fixed 两两碰撞 / 全树视口溢出 / 断点穿越 / 贴边计数 / 定位元素 */
export function runChecks(): DebugReport {
  if (typeof document === 'undefined' || typeof getComputedStyle === 'undefined') {
    return { collisions: [], overflows: [], flushCount: 0, breakpointCrosses: [], positioned: [] }
  }
  const all = Array.from(document.querySelectorAll<HTMLElement>('*'))

  const fixed = all.filter((el) => getComputedStyle(el).position === 'fixed')
  const collisions: CollisionPair[] = []
  for (let i = 0; i < fixed.length; i++) {
    for (let j = i + 1; j < fixed.length; j++) {
      const r = rectsIntersect(rectOf(fixed[i]), rectOf(fixed[j]))
      if (r.hit) {
        collisions.push({
          a: describe(fixed[i]),
          b: describe(fixed[j]),
          overlap: r.overlap!,
          areaPct: round1(r.areaPct),
        })
      }
    }
  }

  const overflows: OverflowInfo[] = []
  const breakpointCrosses: BreakpointCross[] = []
  for (const el of all) {
    overflows.push(...viewportOverflow(el))
    breakpointCrosses.push(...breakpointCross(el))
  }

  const positioned: PositionedInfo[] = collectPositioned(document).map((el) => ({
    el: describe(el),
    tag: el.tagName.toLowerCase(),
    cls: typeof el.className === 'string' ? el.className : String(el.className),
  }))

  const flushCount = all.filter(isFlushable).length

  return { collisions, overflows, flushCount, breakpointCrosses, positioned }
}
