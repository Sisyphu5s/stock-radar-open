/**
 * T-14/T-68/T-117 K 线画线工具引擎（无 React）：基于 lightweight-charts v5 pane primitive 自绘八种工具——
 * 趋势线（两点线段）/ 水平线（一点）/ 垂直线（一点）/ 射线（两点+单向延伸）/ 平行通道（两点+双平行线延伸）/
 * 斐波那契回撤（两点+比例带）/ 测量（两点+价差百分比标签）/ 矩形（两点对角+轻填充）。
 *
 * 实现取舍（任务卡 §5：官方 plugin-examples 画线 POC 与 5.2 API 不直接兼容 + 官方声明 POC 不保证生产级，
 * 故自绘 primitive；项目 klinePrimitives.ts 已有 pane primitive 画线基础，沿用同一模式）：
 * - 数据模型：每线 { id, type, points:[{time,price}], style? }——time 为库 Time（日/周/月 'YYYY-MM-DD'
 *   字符串、分钟线 UTC 秒），price 为价格；style 为样式（颜色/线宽/线型），旧数据缺省兼容（渲染走默认）。
 *   像素坐标每帧经 chart.timeScale().timeToCoordinate / series.priceToCoordinate 现算（不缓存），
 *   数据轮询/缩放/平移后 time 仍有效 → 线自动跟随缩放平移。
 * - 持久化：localStorage 键 `sr-kline-tools:{code}`，上限 100 条（超出删最旧），挂载恢复/删除/清空同步写。
 * - 交互状态机（三段，全部经库公开坐标转换，不碰私有 API）：
 *   idle（tool='none'）：mousedown 命中（端点→线段，捕获阶段 preventDefault+stopPropagation 拦截库平移拖拽）
 *     = 选中 + 可拖动；点击空白（库 click）= 取消选中；Del/Backspace 删除选中；Esc 取消选中。
 *   placing（工具激活）：click 落第 1 点 → crosshairMove 虚线预览 → click 落第 2 点成型
 *     （水平线 1 点即成型）；成型后工具保持激活可连续画；Esc 取消当前绘制（工具保持）。
 *     T-68 落点磁吸：placing 落点/预览的价格在 ±0.3% 内吸附到最近 bar 的 OHLC 之一（MAGNET_RATIO，0=关闭）。
 *   dragging：mousedown 命中端点/线段 → crosshairMove 实时改点（端点拖 / body 按像素偏移整体平移）→
 *     mouseup 结束并持久化；Esc 取消拖动（还原拖动前快照）。
 * - T-68 增强：
 *   · 右键菜单：contextmenu 命中已画线 → 回调 React 层（clientX/clientY + lineId），
 *     「删除此线/清除全部」由 React 层调 controller.deleteLine/clearAll；右键不参与拖拽（仅左键）。
 *   · hover 反馈：crosshairMove 复用 hitTest 做命中检测，悬停线/端点 → onCursorChange('pointer') +
 *     悬停线高亮（tokens.lineColors[1] 蓝，区别于选中态 up 红）；放置中光标 'crosshair'。
 *   · 样式：每条线可设颜色（黄/红/蓝/白，双主题从 chartColors 解析）+ 线宽（1/2/3px）+ 线型（实线/虚线），
 *     controller.setLineStyle 写入 style 字段持久化；选中变化经 onSelectionChange 通知 React 层样式面板。
 * - 主题：isDark 由引擎 applyTheme 同步，渲染色从 chartColors 读取（主色 lineColors[0] 黄、
 *   选中强调 up 红、标签用 label 令牌）——canvas 不解析 CSS var()。
 *
 * 自维护范围（官方画线 plugin 仅 POC，本项目自绘）：命中容差、拖拽拦截、fib 比例带/标签绘制、
 * 测量标签文案、样式渲染、磁吸——库升级（primitive 接口/坐标 API 变化）需随包回归。
 */
import type {
  IChartApi, ISeriesApi, IPanePrimitive, IPanePrimitivePaneView, IPrimitivePaneRenderer,
  PaneAttachedParameter, MouseEventParams,
} from 'lightweight-charts'
import type { Time } from '../../utils/klineSeries'
import type { CanvasRenderingTarget2D } from 'fancy-canvas'
import { chartColors } from '../../utils/chartTheme'

// —— 常量（可变性显式化） ——
const STORAGE_PREFIX = 'sr-kline-tools:' // T-14 持久化键前缀（键 = 前缀 + 股票代码）
const MAX_LINES = 100 // 单股画线上限：超出删最旧（滚动窗口）
const HIT_TOL = 7 // 线段命中容差（px）
const END_TOL = 9 // 端点命中容差（px，比线段大——手柄需易点；T-68 把手放大后保持）
const HANDLE = 8 // 端点手柄半宽（px，T-68 从 5 放大到 8，触屏/鼠标易点）
const FONT = '11px sans-serif'
const LABEL_H = 16 // 标签高（px，与 klinePrimitives 浮标一致）
/** T-68 落点价格磁吸：placing 落点/预览价格与最近 bar OHLC 偏差 ≤ ±0.3% 时吸附到最近档位（0 = 关闭） */
const MAGNET_RATIO = 0.003
/** 斐波那契回撤比例（0→1 顺序） */
const FIB_RATIOS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1] as const
/** 标签文本宽度缓存（文本集合有限；字体固定 → 跨实例/跨主题安全；renderer 每帧新建故须模块级） */
const textWidthCache = new Map<string, number>()

// —— 公共类型与持久化 ——
/**
 * 画线工具全集（T-117 扩展）：一点成型 = horizontal/vertical；两点成型 = trend/fib/measure/ray/channel/rectangle。
 * 新增工具语义：
 * - vertical 垂直线：一点 → 该时点整列竖线（顶部时点标签，拖动改时点）
 * - ray 射线：两点 → 从 p0 经 p1 方向无限延伸（p0 拖动整体平移，p1 拖动旋转方向）
 * - channel 平行通道：两点 → 过 p0/p1 的两条平行线沿方向延伸（p0 拖动整体平移，p1 拖动改方向与宽度）
 * - rectangle 矩形：两点（对角）→ 矩形框 + 轻填充
 */
export type DrawingTool = 'none' | 'trend' | 'horizontal' | 'fib' | 'measure'
  | 'vertical' | 'ray' | 'channel' | 'rectangle'
/** 画线悬停/放置光标（React 壳层消费：图表容器 CSS cursor；变化才回调） */
export type DrawingCursor = 'default' | 'pointer' | 'crosshair'
/** T-68 线样式色档（持久化存 key，渲染按主题从 chartColors 解析——双主题适配） */
export type LineColorKey = 'yellow' | 'red' | 'blue' | 'white'
export interface DrawingStyle {
  color: LineColorKey
  width: 1 | 2 | 3
  dash: boolean
}
export const DEFAULT_STYLE: DrawingStyle = { color: 'yellow', width: 1, dash: false }

const isStyle = (v: unknown): v is DrawingStyle => {
  if (!v || typeof v !== 'object') return false
  const s = v as Partial<DrawingStyle>
  if (s.color !== 'yellow' && s.color !== 'red' && s.color !== 'blue' && s.color !== 'white') return false
  if (s.width !== 1 && s.width !== 2 && s.width !== 3) return false
  return typeof s.dash === 'boolean'
}

export interface DrawingPoint {
  time: Time
  price: number
}
export interface DrawingLine {
  id: string
  type: Exclude<DrawingTool, 'none'>
  points: DrawingPoint[]
  /** T-68 线样式（旧数据缺省 → DEFAULT_STYLE 渲染） */
  style?: DrawingStyle
}

/** 一点成型的工具（其余均为两点成型） */
const ONE_POINT_TOOLS = new Set<DrawingLine['type']>(['horizontal', 'vertical'])

const isLine = (v: unknown): v is DrawingLine => {
  if (!v || typeof v !== 'object') return false
  const o = v as Partial<DrawingLine>
  if (typeof o.id !== 'string') return false
  if (o.type !== 'trend' && o.type !== 'horizontal' && o.type !== 'fib' && o.type !== 'measure'
    && o.type !== 'vertical' && o.type !== 'ray' && o.type !== 'channel' && o.type !== 'rectangle') return false
  if (!Array.isArray(o.points) || o.points.length === 0) return false
  if (o.style !== undefined && !isStyle(o.style)) return false
  return o.points.every((p) => !!p && typeof p === 'object'
    && (typeof (p as DrawingPoint).time === 'string' || typeof (p as DrawingPoint).time === 'number')
    && typeof (p as DrawingPoint).price === 'number' && Number.isFinite((p as DrawingPoint).price))
}

/** 挂载恢复：解析失败/损坏返回空（不抛错打断图表） */
export function loadDrawings(code: string): DrawingLine[] {
  if (!code) return []
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + code)
    if (!raw) return []
    const v: unknown = JSON.parse(raw)
    if (!Array.isArray(v)) return []
    return v.filter(isLine).slice(0, MAX_LINES)
  } catch {
    return []
  }
}

/** 同步写 localStorage（配额满/隐私模式静默丢弃——画线是辅助数据，不阻塞图表） */
function saveDrawings(code: string, lines: DrawingLine[]): void {
  if (!code) return
  try {
    localStorage.setItem(STORAGE_PREFIX + code, JSON.stringify(lines.slice(0, MAX_LINES)))
  } catch {
    /* ignore */
  }
}

const uid = (): string => Math.random().toString(36).slice(2, 10) + Date.now().toString(36)

// —— 内部状态（controller 与 primitive 共享同一引用；requestUpdate 由 attached 注入） ——
type DragPart = 'p0' | 'p1' | 'body'
interface Placing { type: DrawingLine['type']; points: DrawingPoint[]; preview: DrawingPoint | null }
interface DragState {
  id: string
  part: DragPart
  startX: number // 拖动起点内容区 x（px，body 平移像素偏移基准）
  startPrice: number // 拖动起点价格（body 平移价格偏移基准）
  origPoints: DrawingPoint[] // 拖动开始时的线快照（body 平移按快照重算，防偏移累积）
}
interface DrawState {
  chart: IChartApi
  series: ISeriesApi<'Candlestick'>
  tool: DrawingTool
  code: string
  lines: DrawingLine[]
  placing: Placing | null
  selectedId: string | null
  /** T-68 hover 命中线 id（渲染高亮 + cursor 'pointer'） */
  hoverId: string | null
  drag: DragState | null
  isDark: boolean
  /** 最近一次 crosshair 位置（mousedown 命中测试用——鼠标按下前必已触发 crosshairMove） */
  lastPoint: { x: number; y: number; pane: number } | null
  requestUpdate: (() => void) | null
}

export interface KlineDrawingController {
  /** 切换工具 + 股票代码（代码变化时重新加载该股画线并清选中） */
  setTool(tool: DrawingTool, code: string): void
  /** 清除当前股票全部画线（工具栏「清除全部」/右键菜单共用） */
  clearAll(): void
  /** 删除当前选中线（键盘 Del/Backspace 删除共用；工具栏清除按钮走 clearAll） */
  deleteSelected(): void
  /** T-68 删除指定线（右键菜单「删除此线」：无需先选中） */
  deleteLine(id: string): void
  /** T-68 设置线样式（样式面板驱动；写入 style 字段持久化） */
  setLineStyle(id: string, style: DrawingStyle): void
  getCount(): number
  /** 主题切换（引擎 applyTheme 同步调用，渲染色随之更新） */
  setTheme(isDark: boolean): void
  dispose(): void
}

export interface KlineDrawingOptions {
  chart: IChartApi
  /** 主图 K 线 series（价格换算：coordinateToPrice/priceToCoordinate 以它为准；磁吸取 bar OHLC） */
  series: ISeriesApi<'Candlestick'>
  /** 图表容器（mousedown 捕获阶段拦截拖拽；与引擎 wheel 监听同元素） */
  container: HTMLElement
  isDark: boolean
  /** 数量变化回调（React 壳层消费：清除按钮可用性等） */
  onDrawingChange?: (count: number) => void
  /** T-68 hover cursor 回调（React 壳层消费：图表容器 CSS cursor；变化才回调） */
  onCursorChange?: (cursor: DrawingCursor) => void
  /** T-68 右键菜单回调（React 壳层消费：x/y 为 clientX/clientY 屏幕坐标；lineId 为命中的线） */
  onContextMenu?: (info: { x: number; y: number; lineId: string }) => void
  /** T-68 选中线变化回调（React 壳层消费：样式面板显隐与受控值；null = 无选中） */
  onSelectionChange?: (sel: { id: string; style: DrawingStyle } | null) => void
}

// —— 样式解析（双主题从 chartColors 取：canvas 不解析 CSS var()） ——
const styleOf = (line: DrawingLine | null | undefined): DrawingStyle => line?.style ?? DEFAULT_STYLE
export const styleColor = (key: LineColorKey, tokens: ReturnType<typeof chartColors>): string => {
  switch (key) {
    case 'yellow': return tokens.lineColors[0] // 金（chartTokens 既有色板首色）
    case 'red': return tokens.up // 红涨色（选中态同源）
    case 'blue': return tokens.lineColors[1] // 蓝
    case 'white': return tokens.label.text // 中性（暗色亮字/亮色深字——亮底白线不可见故用文字色反转）
  }
}
/** hex 颜色加 alpha 后缀（rgba 形式原样返回，防拼接坏串） */
const withAlpha = (hex: string, suffix: string): string => (hex.length === 7 ? hex + suffix : hex)

// —— 像素换算（渲染/命中测试共用：time 或 price 不可换算 → 整条不画/不命中） ——
interface LineCtx { x: number; y: number }
const linePixels = (st: DrawState, line: DrawingLine): LineCtx[] => {
  const ts = st.chart.timeScale()
  const out: LineCtx[] = []
  for (const p of line.points) {
    const x = ts.timeToCoordinate(p.time)
    const y = st.series.priceToCoordinate(p.price)
    if (x == null || y == null) return [] // 任一点不可换算 → 整条跳过（数据轮询间隙短暂不可见可接受）
    out.push({ x, y })
  }
  return out
}

// —— primitive：渲染全部画线（每帧现算坐标，跟随缩放/平移） ——
class DrawingRenderer implements IPrimitivePaneRenderer {
  private st: DrawState
  constructor(st: DrawState) { this.st = st }
  draw(target: CanvasRenderingTarget2D): void {
    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      const st = this.st
      const tokens = chartColors(st.isDark)
      for (const line of st.lines) {
        drawLine(context, mediaSize, line, st.selectedId === line.id, false, tokens, st)
      }
      // 放置中预览：第一点 + 当前鼠标位（虚线，样式用当前默认）
      const pl = st.placing
      if (pl && pl.points.length > 0 && pl.preview) {
        drawLine(context, mediaSize, { id: 'preview', type: pl.type, points: [pl.points[0], pl.preview] }, false, true, tokens, st)
      }
    })
  }
}
class DrawingView implements IPanePrimitivePaneView {
  private st: DrawState
  constructor(st: DrawState) { this.st = st }
  renderer(): IPrimitivePaneRenderer { return new DrawingRenderer(this.st) }
}
class DrawingPrimitive implements IPanePrimitive<Time> {
  private st: DrawState
  constructor(st: DrawState) { this.st = st }
  attached(param: PaneAttachedParameter): void { this.st.requestUpdate = param.requestUpdate }
  detached(): void { this.st.requestUpdate = null }
  updateAllViews(): void { /* 渲染循环自动重绘 */ }
  paneViews(): IPanePrimitivePaneView[] { return [new DrawingView(this.st)] }
}

// —— 绘制原语（canvas，坐标相对 pane 内容区，与 mediaSize 一致） ——
const measure = (ctx: CanvasRenderingContext2D, text: string): number => {
  let w = textWidthCache.get(text)
  if (w === undefined) { w = Math.ceil(ctx.measureText(text).width); textWidthCache.set(text, w) }
  return w
}
const drawHandle = (ctx: CanvasRenderingContext2D, x: number, y: number, tokens: ReturnType<typeof chartColors>): void => {
  ctx.fillStyle = '#ffffff' // 手柄白底灰框：双主题固定中性（canvas 不解析 var()）
  ctx.fillRect(x - HANDLE, y - HANDLE, HANDLE * 2, HANDLE * 2)
  ctx.strokeStyle = tokens.axis
  ctx.lineWidth = 1
  ctx.strokeRect(x - HANDLE + 0.5, y - HANDLE + 0.5, HANDLE * 2 - 1, HANDLE * 2 - 1)
}
/** 右缘贴轴标签（水平线价格/fib 比例共用；位置随 y 收敛防出界） */
const drawRightLabel = (ctx: CanvasRenderingContext2D, media: { width: number; height: number }, y: number, text: string, color: string, tokens: ReturnType<typeof chartColors>): void => {
  const w = measure(ctx, text) + 12
  const x = media.width - w - 2
  const y2 = Math.max(0, Math.min(media.height - LABEL_H, y - LABEL_H / 2))
  ctx.fillStyle = tokens.label.bg
  ctx.fillRect(x, y2, w, LABEL_H)
  ctx.strokeStyle = tokens.label.border
  ctx.lineWidth = 1
  ctx.strokeRect(x + 0.5, y2 + 0.5, w - 1, LABEL_H - 1)
  ctx.fillStyle = color
  ctx.textAlign = 'left'
  ctx.textBaseline = 'middle'
  ctx.fillText(text, x + 6, y2 + LABEL_H / 2)
}

/** 顶部时点标签（垂直线共用；沿 x 收敛防出界，与 drawRightLabel 同构） */
const drawTopLabel = (ctx: CanvasRenderingContext2D, media: { width: number; height: number }, x: number, text: string, color: string, tokens: ReturnType<typeof chartColors>): void => {
  const w = measure(ctx, text) + 12
  const x2 = Math.max(0, Math.min(media.width - w - 2, x - w / 2))
  ctx.fillStyle = tokens.label.bg
  ctx.fillRect(x2, 2, w, LABEL_H)
  ctx.strokeStyle = tokens.label.border
  ctx.lineWidth = 1
  ctx.strokeRect(x2 + 0.5, 2.5, w - 1, LABEL_H - 1)
  ctx.fillStyle = color
  ctx.textAlign = 'left'
  ctx.textBaseline = 'middle'
  ctx.fillText(text, x2 + 6, 2 + LABEL_H / 2)
}

/**
 * T-117 射线/通道延伸裁剪：从 a 经 b 方向延伸至可视区边缘（b.x >= a.x → 右缘，否则左缘）。
 * dx=0（垂直方向）退化返回 b——仅画线段，不延伸（避免除零/无限延伸线）。
 */
const clipToEdge = (a: LineCtx, b: LineCtx, width: number): LineCtx => {
  const dx = b.x - a.x
  if (dx === 0) return b
  const edgeX = dx > 0 ? width : 0
  const t = (edgeX - a.x) / dx
  return { x: edgeX, y: a.y + (b.y - a.y) * t }
}

/** Time → 时点文本（垂直线顶部标签；string=日线 YYYY-MM-DD，number=分钟 UTC 字段直取，不碰浏览器时区） */
const fmtTimeText = (t: Time): string => {
  if (typeof t === 'string') return t
  if (typeof t === 'number') {
    const d = new Date(t * 1000)
    return `${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')} ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`
  }
  return `${t.year}-${String(t.month).padStart(2, '0')}-${String(t.day).padStart(2, '0')}`
}

/** T-68 线色/线宽/线型统一解析：selected（up 红）> hovered（蓝高亮）> 常态（style 色）；hover 线加粗一档 */
const lineColorOf = (
  selected: boolean,
  hovered: boolean,
  s: DrawingStyle,
  tokens: ReturnType<typeof chartColors>,
): string => (selected ? tokens.up : hovered ? tokens.lineColors[1] : styleColor(s.color, tokens))

const drawLine = (
  ctx: CanvasRenderingContext2D,
  media: { width: number; height: number },
  line: DrawingLine,
  selected: boolean,
  preview: boolean,
  tokens: ReturnType<typeof chartColors>,
  st: DrawState,
): void => {
  const hovered = !selected && st.hoverId === line.id
  const s = styleOf(line)
  ctx.save()
  ctx.font = FONT
  ctx.lineWidth = selected ? s.width + 0.6 : hovered ? s.width + 0.4 : s.width
  ctx.strokeStyle = lineColorOf(selected, hovered, s, tokens)
  ctx.setLineDash(preview ? [4, 4] : s.dash ? [5, 4] : [])

  if (line.type === 'horizontal') {
    const c = linePixels(st, line)[0]
    if (!c) { ctx.restore(); return }
    ctx.beginPath()
    ctx.moveTo(0, c.y + 0.5)
    ctx.lineTo(media.width, c.y + 0.5)
    ctx.stroke()
    drawRightLabel(ctx, media, c.y, line.points[0].price.toFixed(2), selected ? tokens.up : tokens.label.text, tokens)
    if (selected) drawHandle(ctx, media.width, c.y, tokens)
  } else if (line.type === 'vertical') {
    // 垂直线：一点 → 该时点整列竖线 + 顶部时点标签 + 锚点手柄
    const c = linePixels(st, line)[0]
    if (!c) { ctx.restore(); return }
    ctx.beginPath()
    ctx.moveTo(c.x + 0.5, 0)
    ctx.lineTo(c.x + 0.5, media.height)
    ctx.stroke()
    if (!preview) drawTopLabel(ctx, media, c.x, fmtTimeText(line.points[0].time), selected ? tokens.up : tokens.label.text, tokens)
    if (selected) drawHandle(ctx, c.x, c.y, tokens)
  } else if (line.type === 'ray') {
    // 射线：p0 → p1 方向延伸至可视边缘（p0 拖动=整体平移，p1 拖动=旋转）
    const cs = linePixels(st, line)
    if (cs.length < 2) { ctx.restore(); return }
    const end = clipToEdge(cs[0], cs[1], media.width)
    ctx.beginPath()
    ctx.moveTo(cs[0].x, cs[0].y)
    ctx.lineTo(end.x, end.y)
    ctx.stroke()
    if (selected) { drawHandle(ctx, cs[0].x, cs[0].y, tokens); drawHandle(ctx, cs[1].x, cs[1].y, tokens) }
  } else if (line.type === 'channel') {
    // 平行通道：过 p0/p1 的两条平行线沿方向延伸 + 左端连接段（p0 拖动=整体平移，p1 拖动=改方向/宽度）
    const cs = linePixels(st, line)
    if (cs.length < 2) { ctx.restore(); return }
    const aEnd = clipToEdge(cs[0], cs[1], media.width)
    const bEnd = clipToEdge(cs[1], { x: cs[1].x + (cs[1].x - cs[0].x), y: cs[1].y + (cs[1].y - cs[0].y) }, media.width)
    ctx.beginPath()
    ctx.moveTo(cs[0].x, cs[0].y)
    ctx.lineTo(aEnd.x, aEnd.y)
    ctx.stroke()
    ctx.beginPath()
    ctx.moveTo(cs[1].x, cs[1].y)
    ctx.lineTo(bEnd.x, bEnd.y)
    ctx.stroke()
    ctx.beginPath()
    ctx.moveTo(cs[0].x, cs[0].y)
    ctx.lineTo(cs[1].x, cs[1].y)
    ctx.stroke()
    if (selected) { drawHandle(ctx, cs[0].x, cs[0].y, tokens); drawHandle(ctx, cs[1].x, cs[1].y, tokens) }
  } else if (line.type === 'rectangle') {
    // 矩形：两点对角 + 轻填充（跟随线色 12% alpha）
    const cs = linePixels(st, line)
    if (cs.length < 2) { ctx.restore(); return }
    const x0 = Math.min(cs[0].x, cs[1].x)
    const y0 = Math.min(cs[0].y, cs[1].y)
    const x1 = Math.max(cs[0].x, cs[1].x)
    const y1 = Math.max(cs[0].y, cs[1].y)
    const fill = withAlpha(styleColor(s.color, tokens), '1f')
    ctx.fillStyle = fill
    ctx.fillRect(x0, y0, x1 - x0, y1 - y0)
    ctx.strokeRect(x0 + 0.5, y0 + 0.5, x1 - x0 - 1, y1 - y0 - 1)
    if (selected) { drawHandle(ctx, cs[0].x, cs[0].y, tokens); drawHandle(ctx, cs[1].x, cs[1].y, tokens) }
  } else {
    const cs = linePixels(st, line)
    if (cs.length < 2) { ctx.restore(); return }
    ctx.beginPath()
    ctx.moveTo(cs[0].x, cs[0].y)
    ctx.lineTo(cs[1].x, cs[1].y)
    ctx.stroke()
    if (line.type === 'fib') {
      drawFibBands(ctx, media, line.points, tokens, st, s)
    } else if (line.type === 'measure' && !preview) {
      drawMeasureLabel(ctx, media, cs[0], cs[1], line.points, tokens)
    }
    if (selected) { drawHandle(ctx, cs[0].x, cs[0].y, tokens); drawHandle(ctx, cs[1].x, cs[1].y, tokens) }
  }
  ctx.restore()
}

/** 斐波那契回撤：7 条比例水平线 + 交替填充带 + 右缘比例/价位标签（首尾斜线由 drawLine 画） */
const drawFibBands = (
  ctx: CanvasRenderingContext2D,
  media: { width: number; height: number },
  pts: DrawingPoint[],
  tokens: ReturnType<typeof chartColors>,
  st: DrawState,
  s: DrawingStyle,
): void => {
  const lo = Math.min(pts[0].price, pts[1].price)
  const hi = Math.max(pts[0].price, pts[1].price)
  if (hi - lo < 1e-9) return
  const fill = withAlpha(styleColor(s.color, tokens), '26') // 15% alpha 交替带（跟随线色）
  let prevY: number | null = null
  for (let i = 0; i < FIB_RATIOS.length; i++) {
    const r = FIB_RATIOS[i]
    const y = st.series.priceToCoordinate(lo + (hi - lo) * r)
    if (y == null) { prevY = null; continue }
    if (prevY != null && i % 2 === 1) {
      ctx.fillStyle = fill
      ctx.fillRect(0, Math.min(y, prevY), media.width, Math.abs(y - prevY))
    }
    ctx.beginPath()
    ctx.moveTo(0, y + 0.5)
    ctx.lineTo(media.width, y + 0.5)
    ctx.stroke()
    drawRightLabel(ctx, media, y, `${r.toFixed(3)}  ${(lo + (hi - lo) * r).toFixed(2)}`, tokens.label.text, tokens)
    prevY = y
  }
}

/** 测量标签：线段中点（价格差 + 有符号百分比，A 股红涨绿跌语义） */
const drawMeasureLabel = (
  ctx: CanvasRenderingContext2D,
  media: { width: number; height: number },
  a: LineCtx,
  b: LineCtx,
  pts: DrawingPoint[],
  tokens: ReturnType<typeof chartColors>,
): void => {
  const diff = Math.abs(pts[1].price - pts[0].price)
  const pct = pts[0].price !== 0 ? ((pts[1].price - pts[0].price) / pts[0].price) * 100 : 0
  const text = `Δ${diff.toFixed(2)}  ${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`
  const w = measure(ctx, text) + 12
  const x = Math.max(2, Math.min(media.width - w - 2, (a.x + b.x) / 2 - w / 2))
  const y2 = Math.max(0, Math.min(media.height - LABEL_H, (a.y + b.y) / 2 - LABEL_H / 2))
  ctx.fillStyle = tokens.label.bg
  ctx.fillRect(x, y2, w, LABEL_H)
  ctx.strokeStyle = tokens.label.border
  ctx.lineWidth = 1
  ctx.strokeRect(x + 0.5, y2 + 0.5, w - 1, LABEL_H - 1)
  ctx.fillStyle = pts[1].price - pts[0].price >= 0 ? tokens.up : tokens.down
  ctx.textAlign = 'left'
  ctx.textBaseline = 'middle'
  ctx.fillText(text, x + 6, y2 + LABEL_H / 2)
}

// —— controller ——
export function createKlineDrawing(opts: KlineDrawingOptions): KlineDrawingController {
  const { chart, series, container, isDark, onDrawingChange, onCursorChange, onContextMenu, onSelectionChange } = opts
  const st: DrawState = {
    chart,
    series,
    tool: 'none',
    code: '',
    lines: [],
    placing: null,
    selectedId: null,
    hoverId: null,
    drag: null,
    isDark,
    lastPoint: null,
    requestUpdate: null,
  }

  const requestUpdate = (): void => { st.requestUpdate?.() }
  const persist = (): void => saveDrawings(st.code, st.lines)
  const notifyCount = (): void => onDrawingChange?.(st.lines.length)
  /** 选中线变化通知（React 层样式面板受控值）：id 存在且线仍在 → 带当前 style；否则 null */
  const notifySelection = (): void => {
    const line = st.selectedId ? st.lines.find((l) => l.id === st.selectedId) : null
    onSelectionChange?.(line ? { id: line.id, style: styleOf(line) } : null)
  }
  /** 选中封装：值变化才更新 + 通知（覆盖全部选中变更路径：点击/拖拽选中、画完自动选中、删除/清空/换股取消） */
  const setSelected = (id: string | null): void => {
    if (st.selectedId !== id) {
      st.selectedId = id
      notifySelection()
    }
  }

  // 鼠标坐标 → (time, price)：x 用 timeScale.coordinateToTime（内容区坐标，与 timeToCoordinate 同基准）；
  // y 用主 series.coordinateToPrice（param.point 相对鼠标所在 pane 顶部，pane 0 即主图内容区）；
  // magnet=true 时（placing 落点/预览）对价格做 OHLC 轻量磁吸
  const magnetPrice = (x: number, price: number): number => {
    if (MAGNET_RATIO <= 0) return price
    const logical = chart.timeScale().coordinateToLogical(x)
    if (logical == null) return price
    const bar = series.dataByIndex(Math.round(logical), -1)
    if (!bar || !('close' in bar)) return price
    let best = price
    let bestRel = Infinity
    const base = Math.max(Math.abs(price), 1e-9)
    for (const v of [bar.open, bar.high, bar.low, bar.close]) {
      if (v == null || !Number.isFinite(v)) continue
      const rel = Math.abs(v - price) / base
      if (rel <= MAGNET_RATIO && rel < bestRel) { best = v; bestRel = rel }
    }
    return best
  }
  const pointToData = (x: number, y: number, magnet = false): DrawingPoint | null => {
    const time = chart.timeScale().coordinateToTime(x)
    const rawPrice = series.coordinateToPrice(y)
    if (time == null || rawPrice == null || !Number.isFinite(rawPrice)) return null
    const price = magnet ? magnetPrice(x, Number(rawPrice)) : Number(rawPrice)
    return { time, price }
  }

  // —— 命中测试（像素空间；从后往前 = 后画的在上层优先命中） ——
  const dist = (x: number, y: number, p: LineCtx): number => Math.hypot(x - p.x, y - p.y)
  const segDist = (x: number, y: number, a: LineCtx, b: LineCtx): number => {
    const dx = b.x - a.x
    const dy = b.y - a.y
    if (dx === 0 && dy === 0) return dist(x, y, a)
    const t = Math.max(0, Math.min(1, ((x - a.x) * dx + (y - a.y) * dy) / (dx * dx + dy * dy)))
    return dist(x, y, { x: a.x + t * dx, y: a.y + t * dy })
  }
  const hitTest = (x: number, y: number): { id: string; part: DragPart } | null => {
    for (let i = st.lines.length - 1; i >= 0; i--) {
      const line = st.lines[i]
      const cs = linePixels(st, line)
      if (cs.length < (ONE_POINT_TOOLS.has(line.type) ? 1 : 2)) continue
      if (line.type === 'horizontal') {
        if (dist(x, y, cs[0]) <= END_TOL) return { id: line.id, part: 'p0' }
        if (Math.abs(y - cs[0].y) <= HIT_TOL) return { id: line.id, part: 'body' }
      } else if (line.type === 'vertical') {
        if (dist(x, y, cs[0]) <= END_TOL) return { id: line.id, part: 'p0' }
        if (Math.abs(x - cs[0].x) <= HIT_TOL) return { id: line.id, part: 'body' }
      } else {
        if (dist(x, y, cs[0]) <= END_TOL) return { id: line.id, part: 'p0' }
        if (dist(x, y, cs[1]) <= END_TOL) return { id: line.id, part: 'p1' }
        // 射线/通道延伸段命中：以内容区宽（timeScale().width()）裁剪，与渲染同源
        const cw = chart.timeScale().width()
        if (line.type === 'fib') {
          // 比例带内（y 在两端点价位之间）→ 整体拖动
          const a = cs[0].y <= cs[1].y ? cs[0] : cs[1]
          const b = a === cs[0] ? cs[1] : cs[0]
          if (y >= a.y - HIT_TOL && y <= b.y + HIT_TOL) return { id: line.id, part: 'body' }
        } else if (line.type === 'ray') {
          if (segDist(x, y, cs[0], clipToEdge(cs[0], cs[1], cw)) <= HIT_TOL) return { id: line.id, part: 'body' }
        } else if (line.type === 'channel') {
          const aEnd = clipToEdge(cs[0], cs[1], cw)
          const bEnd = clipToEdge(cs[1], { x: cs[1].x + (cs[1].x - cs[0].x), y: cs[1].y + (cs[1].y - cs[0].y) }, cw)
          if (segDist(x, y, cs[0], aEnd) <= HIT_TOL || segDist(x, y, cs[1], bEnd) <= HIT_TOL
            || segDist(x, y, cs[0], cs[1]) <= HIT_TOL) return { id: line.id, part: 'body' }
        } else if (line.type === 'rectangle') {
          const x0 = Math.min(cs[0].x, cs[1].x)
          const y0 = Math.min(cs[0].y, cs[1].y)
          const x1 = Math.max(cs[0].x, cs[1].x)
          const y1 = Math.max(cs[0].y, cs[1].y)
          if (x >= x0 - HIT_TOL && x <= x1 + HIT_TOL && y >= y0 - HIT_TOL && y <= y1 + HIT_TOL
            && (Math.abs(x - x0) <= HIT_TOL || Math.abs(x - x1) <= HIT_TOL
              || Math.abs(y - y0) <= HIT_TOL || Math.abs(y - y1) <= HIT_TOL)) {
            return { id: line.id, part: 'body' }
          }
        } else if (segDist(x, y, cs[0], cs[1]) <= HIT_TOL) {
          return { id: line.id, part: 'body' }
        }
      }
    }
    return null
  }

  // —— 交互：click（绘制落点 / 空白取消选中） ——
  const onChartClick = (param: MouseEventParams<Time>): void => {
    if (!param.point || param.paneIndex !== 0) return // 画线仅主 pane
    if (st.tool !== 'none') { handlePlace(param); return }
    if (st.drag) return // 刚拖完（mouseup 已收尾），跳过本 click 的重复选择
    const hit = hitTest(param.point.x, param.point.y)
    setSelected(hit ? hit.id : null)
  }
  const handlePlace = (param: MouseEventParams<Time>): void => {
    if (!param.point) return // 离开图表无坐标（onChartClick 已判空，但函数签名独立，TS 窄化不跨函数）
    const d = pointToData(param.point.x, param.point.y, true)
    if (!d) return
    const type = st.tool as Exclude<DrawingTool, 'none'>
    let pl = st.placing
    if (!pl || pl.type !== type) { pl = { type, points: [], preview: null }; st.placing = pl }
    if (ONE_POINT_TOOLS.has(type)) {
      // 一点成型（水平线/垂直线）
      addLine({ id: uid(), type, points: [d] })
      st.placing = null
      requestUpdate()
      return
    }
    if (pl.points.length === 0) {
      pl.points.push(d)
    } else {
      pl.points.push(d)
      addLine({ id: uid(), type, points: [...pl.points] })
      st.placing = null
    }
    requestUpdate()
  }
  const addLine = (line: DrawingLine): void => {
    if (st.lines.length >= MAX_LINES) st.lines.shift() // 上限 100：删最旧
    st.lines.push(line)
    setSelected(line.id) // 画完立即选中，可直接拖动微调
    persist()
    notifyCount()
    requestUpdate()
  }
  const deleteLine = (id: string): void => {
    st.lines = st.lines.filter((l) => l.id !== id)
    if (st.selectedId === id) setSelected(null)
    if (st.hoverId === id) st.hoverId = null
    persist()
    notifyCount()
    requestUpdate()
  }

  // —— 交互：crosshairMove（放置预览 / 拖动实时改点 / hover 命中与光标 / 缓存最后坐标） ——
  let lastCursor: DrawingCursor = 'default'
  const updateHover = (): void => {
    const pt = st.lastPoint
    let cursor: DrawingCursor = 'default'
    let hoverId: string | null = null
    if (pt && pt.pane === 0) {
      if (st.drag) {
        cursor = 'pointer' // 拖拽中恒指向（命中测试可能随快速移动脱靶，保持光标稳定）
      } else if (st.tool !== 'none') {
        cursor = 'crosshair'
      } else {
        const hit = hitTest(pt.x, pt.y)
        if (hit) { hoverId = hit.id; cursor = 'pointer' }
      }
    }
    if (hoverId !== st.hoverId) {
      st.hoverId = hoverId
      requestUpdate() // hover 线高亮重绘
    }
    if (cursor !== lastCursor) {
      lastCursor = cursor
      onCursorChange?.(cursor)
    }
  }
  const onCrosshairMove = (param: MouseEventParams<Time>): void => {
    st.lastPoint = param.point
      ? { x: param.point.x, y: param.point.y, pane: param.paneIndex ?? -1 }
      : null
    updateHover()
    if (st.drag && st.lastPoint && st.lastPoint.pane === 0) {
      const cur = pointToData(st.lastPoint.x, st.lastPoint.y)
      if (cur) { applyDrag(cur, st.lastPoint.x); requestUpdate() }
    }
    const pl = st.placing
    if (pl && pl.points.length > 0 && st.lastPoint && st.lastPoint.pane === 0) {
      const d = pointToData(st.lastPoint.x, st.lastPoint.y, true)
      if (d && (!pl.preview || pl.preview.time !== d.time || pl.preview.price !== d.price)) {
        pl.preview = d
        requestUpdate()
      }
    }
  }
  const applyDrag = (cur: DrawingPoint, curX: number): void => {
    const d = st.drag
    if (!d) return
    const line = st.lines.find((l) => l.id === d.id)
    if (!line) return
    // 端点拖动：p1 恒为端点改点；p0 对线段类工具是端点改点，射线/通道的 p0 = 锚点（整体平移，保持方向/形状）
    if (d.part === 'p1' || (d.part === 'p0' && line.type !== 'ray' && line.type !== 'channel')) {
      const idx = d.part === 'p0' ? 0 : 1
      if (line.points.length > idx) line.points[idx] = cur
      return
    }
    // body（或射线/通道 p0）整体平移：基于拖动前快照，价格差 + 像素偏移重算每点（time 用 coordinateToTime 换算，
    // 视觉完全跟随鼠标；快照防偏移累积）
    const priceD = cur.price - d.startPrice
    const xD = curX - d.startX
    const ts = chart.timeScale()
    for (let i = 0; i < d.origPoints.length && i < line.points.length; i++) {
      const op = d.origPoints[i]
      const origX = ts.timeToCoordinate(op.time)
      if (origX == null) continue // 原时间不可换算（数据范围外）：该点保持
      const t = ts.coordinateToTime(origX + xD)
      if (t != null) line.points[i] = { time: t, price: op.price + priceD }
    }
  }

  // —— 交互：mousedown 捕获阶段拦截（左键命中线 → 选中 + 启动拖动；不命中/非左键放行库平移/点击） ——
  const onMouseDown = (e: MouseEvent): void => {
    if (e.button !== 0) return // 仅左键参与拖拽（右键留给 contextmenu 菜单）
    if (st.tool !== 'none') return // 绘制态不拦截（落点走 click；拖动平移保留库行为）
    const pt = st.lastPoint
    if (!pt || pt.pane !== 0) return
    const hit = hitTest(pt.x, pt.y)
    if (!hit) return
    const line = st.lines.find((l) => l.id === hit.id)
    if (!line) return
    // 捕获阶段拦截：库收不到 mousedown → 不会启动 pressedMouseMove 平移
    e.preventDefault()
    e.stopPropagation()
    setSelected(hit.id)
    const start = pointToData(pt.x, pt.y)
    st.drag = {
      id: hit.id,
      part: hit.part,
      startX: pt.x,
      startPrice: start?.price ?? line.points[0].price,
      origPoints: line.points.map((p) => ({ ...p })),
    }
    requestUpdate()
  }
  const onMouseUp = (): void => {
    if (st.drag) { st.drag = null; persist() } // 拖动结束落盘
  }

  // —— 交互：右键菜单（命中已画线 → 自绘菜单回调 React 层；右键不放行拖拽已在 onMouseDown 排除） ——
  const handleContextMenu = (e: MouseEvent): void => {
    if (st.tool !== 'none') return // 放置中不弹（右键可保留为画线期间的辅助，不引入菜单）
    const rect = container.getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    const hit = hitTest(x, y)
    if (!hit) return // 未命中画线：放行浏览器默认菜单
    e.preventDefault()
    e.stopPropagation()
    onContextMenu?.({ x: e.clientX, y: e.clientY, lineId: hit.id })
  }

  // —— 交互：键盘（Esc 取消放置/拖动/选中；Del/Backspace 删除选中；输入态不劫持） ——
  const onKeyDown = (e: KeyboardEvent): void => {
    const tgt = e.target as HTMLElement | null
    if (tgt && (tgt.tagName === 'INPUT' || tgt.tagName === 'TEXTAREA' || tgt.tagName === 'SELECT' || tgt.isContentEditable)) return
    if (e.key === 'Escape') {
      let consumed = false
      if (st.drag) {
        // 取消拖动：还原快照（applyDrag 已原地改点）
        const line = st.lines.find((l) => l.id === st.drag!.id)
        if (line) st.drag.origPoints.forEach((p, i) => { if (line.points[i]) line.points[i] = p })
        st.drag = null
        consumed = true
      } else if (st.placing) {
        st.placing = null // 取消当前绘制，工具保持激活
        consumed = true
      } else if (st.selectedId) {
        setSelected(null)
        consumed = true
      }
      requestUpdate()
      // T-117 全屏 Esc 退出共存：被画线消费的 Esc 不再冒泡（window 级全屏退出监听不会误触发）；
      // 无画线可取消时不拦截——全屏下按 Esc 正常退出
      if (consumed) e.stopPropagation()
      return
    }
    if ((e.key === 'Delete' || e.key === 'Backspace') && st.selectedId) {
      e.preventDefault() // 拦截 Backspace 浏览器后退
      deleteLine(st.selectedId)
    }
  }

  // —— primitive 挂主 pane（跟随缩放/平移：pane primitive 随内容层重绘） ——
  const prim = new DrawingPrimitive(st)
  const pane = chart.panes()[0]
  pane?.attachPrimitive(prim)

  // —— 订阅 ——
  chart.subscribeClick(onChartClick)
  chart.subscribeCrosshairMove(onCrosshairMove)
  container.addEventListener('mousedown', onMouseDown, { capture: true })
  container.addEventListener('contextmenu', handleContextMenu)
  window.addEventListener('mouseup', onMouseUp)
  document.addEventListener('keydown', onKeyDown)

  const controller: KlineDrawingController = {
    setTool(tool, code) {
      if (code !== st.code) {
        st.code = code
        st.lines = loadDrawings(code) // 挂载恢复（切换股票重载该股画线）
        setSelected(null)
        st.placing = null
        st.drag = null
        notifyCount()
      }
      if (tool !== st.tool) {
        st.tool = tool
        st.placing = null // 换工具丢弃半成品
        st.hoverId = null
        lastCursor = 'default'
        onCursorChange?.('default')
      }
      requestUpdate()
    },
    clearAll() {
      if (st.lines.length === 0) return
      st.lines = []
      setSelected(null)
      st.placing = null
      st.drag = null
      st.hoverId = null
      persist()
      notifyCount()
      requestUpdate()
    },
    deleteSelected() {
      if (st.selectedId) deleteLine(st.selectedId)
    },
    deleteLine(id) {
      if (st.lines.some((l) => l.id === id)) deleteLine(id)
    },
    setLineStyle(id, style) {
      const line = st.lines.find((l) => l.id === id)
      if (!line) return
      line.style = style
      persist()
      notifySelection() // 面板受控值跟随（选中线 style 变化即时回写）
      requestUpdate()
    },
    getCount() {
      return st.lines.length
    },
    setTheme(dark) {
      st.isDark = dark
      requestUpdate()
    },
    dispose() {
      chart.unsubscribeClick(onChartClick)
      chart.unsubscribeCrosshairMove(onCrosshairMove)
      container.removeEventListener('mousedown', onMouseDown, { capture: true })
      container.removeEventListener('contextmenu', handleContextMenu)
      window.removeEventListener('mouseup', onMouseUp)
      document.removeEventListener('keydown', onKeyDown)
      pane?.detachPrimitive(prim)
    },
  }
  return controller
}
