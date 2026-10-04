/* ============================================================
   DebugOverlay 覆盖层:全屏诊断层(pointer-events:none,不拦截交互)
   - canvas 绘制:①悬停矩形(1px 蓝)②选中元素 BoxModel 四层嵌套框
     ③测量线+端点④碰撞交叠斜线区⑤吸附参考虚线
   - DOM 标签层:悬停 tooltip / 选中坐标(绝对+相对双行)/ 测量距离标签
   - 状态全部来自 useDebugOverlay 单例 store(hook 自监听 document)
   - 层级:z-index 1900(低于 Debug 面板 2000)
   - 依赖注入:无——组件与面板各自 useDebugOverlay() 即共享状态
   ============================================================ */

import { useEffect, useRef, useState } from 'react'
import { useThemeStore } from '../../stores/useAppStore'
import { useDebugOverlay } from '../../utils/debug/debugOverlay'
import type { Point } from '../../utils/debug/debugOverlay'
import { boxModelOf, collectPositioned, describe, rectOf, relRectOf, snapToEdges } from '../../utils/debug/debugCore'
import type { Rect } from '../../utils/debug/debugTypes'
import './debug-overlay.css'

/* canvas 不解析 CSS var():诊断层颜色用 JS 常量。
   BoxModel 四层色为层语义色(margin 橙/border 黄/padding 绿/content 蓝),双主题通用;
   hover/吸附线随 isDark 切换(亮 #1668dc / 暗 #4c9aff,与 --sr-accent 一致)。 */
const HOVER_COLOR = { light: '#1668dc', dark: '#4c9aff' }
const SNAP_COLOR = { light: 'rgba(22, 104, 220, 0.55)', dark: 'rgba(76, 154, 255, 0.55)' }
const BOX_LAYERS = ['margin', 'border', 'padding', 'content'] as const
type BoxLayer = (typeof BOX_LAYERS)[number]
const BOX_COLORS: Record<BoxLayer, string> = {
  margin: '#f59e0b',
  border: '#eab308',
  padding: '#22c55e',
  content: '#3b82f6',
}
const BOX_LAYER_ORDER: readonly BoxLayer[] = BOX_LAYERS
const MEASURE_COLOR = '#ef4444'
const COLLIDE_COLOR = '#ef4444'
const HIGHLIGHT_COLOR = '#f97316'

const withAlpha = (hex: string, a: number): string => {
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  return `rgba(${r},${g},${b},${a})`
}

/** 测量段:红线 + 端点圆点(未完成段虚线) */
function drawSegment(ctx: CanvasRenderingContext2D, p1: Point, p2: Point, dashed: boolean): void {
  ctx.strokeStyle = MEASURE_COLOR
  ctx.lineWidth = 1.5
  if (dashed) ctx.setLineDash([4, 4])
  ctx.beginPath()
  ctx.moveTo(p1.x, p1.y)
  ctx.lineTo(p2.x, p2.y)
  ctx.stroke()
  ctx.setLineDash([])
  ctx.fillStyle = MEASURE_COLOR
  for (const p of [p1, p2]) {
    ctx.beginPath()
    ctx.arc(p.x, p.y, 3.5, 0, Math.PI * 2)
    ctx.fill()
  }
}

/** 碰撞交叠区:半透明填充 + 斜线纹 + 描边 */
function hatchRect(ctx: CanvasRenderingContext2D, r: Rect, color: string): void {
  ctx.save()
  ctx.fillStyle = withAlpha(color, 0.10)
  ctx.fillRect(r.left, r.top, r.width, r.height)
  ctx.strokeStyle = withAlpha(color, 0.7)
  ctx.lineWidth = 1
  ctx.strokeRect(r.left, r.top, r.width, r.height)
  ctx.beginPath()
  const step = 5
  for (let x = r.left - r.height; x < r.right + r.height; x += step) {
    ctx.moveTo(x, r.bottom)
    ctx.lineTo(x + r.height, r.top)
  }
  ctx.stroke()
  ctx.restore()
}

interface OverlayCtx {
  hoverEl: Element | null
  selectedEl: Element | null
  pointer: Point | null
  measureMode: boolean
  pendingPoint: Point | null
  measures: { p1: Point; p2: Point }[]
  collisionItems: { overlap?: Rect }[]
  highlightEl: Element | null
}

function draw(ctx: CanvasRenderingContext2D, s: OverlayCtx, isDark: boolean, vw: number, vh: number): void {
  ctx.clearRect(0, 0, vw, vh)
  const hover = HOVER_COLOR[isDark ? 'dark' : 'light']

  // ① 悬停元素矩形框(1px 蓝;与选中元素同元素时不再重复描)
  if (s.hoverEl && s.hoverEl !== s.selectedEl) {
    const r = rectOf(s.hoverEl)
    ctx.strokeStyle = hover
    ctx.lineWidth = 1
    ctx.strokeRect(r.left + 0.5, r.top + 0.5, r.width - 1, r.height - 1)
  }

  // ② 选中元素 BoxModel 四层嵌套框(2px 半透明;四角连线 path,transform 下仍精确)
  if (s.selectedEl) {
    const bm = boxModelOf(s.selectedEl)
    for (const key of BOX_LAYER_ORDER) {
      const q = bm[key]
      const [p1, p2, p3, p4] = q.corners
      ctx.beginPath()
      ctx.moveTo(p1.x, p1.y)
      ctx.lineTo(p2.x, p2.y)
      ctx.lineTo(p3.x, p3.y)
      ctx.lineTo(p4.x, p4.y)
      ctx.closePath()
      ctx.fillStyle = withAlpha(BOX_COLORS[key], 0.06)
      ctx.fill()
      ctx.strokeStyle = withAlpha(BOX_COLORS[key], 0.8)
      ctx.lineWidth = 2
      ctx.stroke()
    }
  }

  // ③ 测量线 + 端点标记(红;未完成段跟随光标虚线)
  for (const m of s.measures) drawSegment(ctx, m.p1, m.p2, false)
  if (s.measureMode && s.pendingPoint && s.pointer) drawSegment(ctx, s.pendingPoint, s.pointer, true)

  // ④ 碰撞交叠区域(棋盘格/斜线)
  for (const it of s.collisionItems) {
    if (it.overlap) hatchRect(ctx, it.overlap, COLLIDE_COLOR)
  }

  // 碰撞点击高亮:目标元素虚线框
  if (s.highlightEl) {
    const r = rectOf(s.highlightEl)
    ctx.strokeStyle = HIGHLIGHT_COLOR
    ctx.lineWidth = 2
    ctx.setLineDash([6, 3])
    ctx.strokeRect(r.left + 1, r.top + 1, r.width - 2, r.height - 2)
    ctx.setLineDash([])
  }

  // ⑤ 对齐吸附参考虚线(选中元素左上角 vs 定位元素边缘,core snapToEdges 返回 {x,y,guides})
  if (s.selectedEl) {
    const rect = rectOf(s.selectedEl)
    ctx.strokeStyle = SNAP_COLOR[isDark ? 'dark' : 'light']
    ctx.lineWidth = 1
    ctx.setLineDash([4, 4])
    const snap = snapToEdges({ x: rect.left, y: rect.top }, collectPositioned().map(rectOf))
    for (const g of snap.guides) {
      ctx.beginPath()
      if (g.dir === 'left' || g.dir === 'right' || g.dir === 'vmid') {
        ctx.moveTo(g.value, 0)
        ctx.lineTo(g.value, vh)
      } else {
        ctx.moveTo(0, g.value)
        ctx.lineTo(vw, g.value)
      }
      ctx.stroke()
    }
    ctx.setLineDash([])
  }
}

interface LabelSpec {
  id: string
  cls: string
  left: number
  top: number
  text: string
}

export default function DebugOverlay() {
  const { state } = useDebugOverlay()
  const isDark = useThemeStore((s) => s.theme === 'dark')
  const [size, setSize] = useState(() => ({ w: window.innerWidth, h: window.innerHeight }))
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    const onResize = (): void => setSize({ w: window.innerWidth, h: window.innerHeight })
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // canvas 重绘:依赖 store 状态(isDark 放依赖,主题切换即重绘)
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const dpr = window.devicePixelRatio || 1
    canvas.width = size.w * dpr
    canvas.height = size.h * dpr
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.scale(dpr, dpr)
    draw(ctx, state, isDark, size.w, size.h)
  }, [state, isDark, size])

  if (state.mode !== 'visual') return null

  const labels: LabelSpec[] = []
  const sizeRef = size
  const clampX = (left: number, w: number): number => Math.min(Math.max(left, 4), sizeRef.w - w - 4)
  const clampY = (top: number, h: number): number => Math.min(Math.max(top, 4), sizeRef.h - h - 4)

  // 悬停 tooltip:tag.cls + WxH
  if (state.hoverEl && state.hoverEl !== state.selectedEl) {
    const r = rectOf(state.hoverEl)
    let left = r.right + 8
    if (left + 220 > sizeRef.w - 4) left = Math.max(4, r.left - 220 - 8)
    labels.push({
      id: 'hover',
      cls: 'sr-dbg-tag',
      left,
      top: clampY(r.top, 22),
      text: `${describe(state.hoverEl)}  ${Math.round(r.width)}×${Math.round(r.height)}`,
    })
  }

  // 选中元素坐标标签:绝对 + 相对 offsetParent 双行(core relRectOf 返回三坐标系,相对坐标=viewport−offsetParent)
  if (state.selectedEl) {
    const r = rectOf(state.selectedEl)
    const rel = relRectOf(state.selectedEl)
    const op = rel.offsetParent
    const relX = op ? Math.round(r.left - op.left) : Math.round(r.left)
    const relY = op ? Math.round(r.top - op.top) : Math.round(r.top)
    labels.push({
      id: 'sel',
      cls: 'sr-dbg-coord',
      left: clampX(r.left, 240),
      top: Math.max(4, r.top - 40),
      text: `绝对 (${Math.round(r.left)}, ${Math.round(r.top)})\n相对 offsetParent (${relX}, ${relY})`,
    })
  }

  // 测量距离标签(dx/dy/距离);未完成段跟随光标
  const measureText = (p1: Point, p2: Point): string => {
    const dx = Math.round(p2.x - p1.x)
    const dy = Math.round(p2.y - p1.y)
    return `dx ${dx} · dy ${dy} · ${Math.round(Math.hypot(dx, dy))}px`
  }
  state.measures.forEach((m, i) => {
    const cx = (m.p1.x + m.p2.x) / 2
    const cy = (m.p1.y + m.p2.y) / 2
    labels.push({
      id: `m${i}`,
      cls: 'sr-dbg-measure-label',
      left: clampX(cx + 8, 180),
      top: clampY(cy - 20, 18),
      text: measureText(m.p1, m.p2),
    })
  })
  if (state.measureMode && state.pendingPoint && state.pointer) {
    labels.push({
      id: 'm-pending',
      cls: 'sr-dbg-measure-label',
      left: clampX(state.pendingPoint.x + 8, 180),
      top: clampY(state.pendingPoint.y - 20, 18),
      text: measureText(state.pendingPoint, state.pointer),
    })
  }

  // 碰撞高亮元素标签
  if (state.highlightEl) {
    const r = rectOf(state.highlightEl)
    labels.push({
      id: 'hl',
      cls: 'sr-dbg-hl-label',
      left: clampX(r.right - 220, 220),
      top: clampY(r.bottom + 6, 18),
      text: describe(state.highlightEl),
    })
  }

  return (
    <div className="sr-dbg-root">
      <canvas ref={canvasRef} className="sr-dbg-canvas" aria-hidden />
      <div className="sr-dbg-labels">
        {labels.map((l) => (
          <div key={l.id} className={l.cls} style={{ left: l.left, top: l.top }}>{l.text}</div>
        ))}
      </div>
    </div>
  )
}
