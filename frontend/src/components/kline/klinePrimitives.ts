/**
 * K 线图 pane 级 primitive（无 React）：十字准星横线 + 右缘 y→值浮标 + 顶部竖线列标签。
 * 库原生 crosshair 轴标签已关闭（labelVisible:false），全部 pane（主图/成交量/副图）统一用本 primitive；
 * 引擎持有 CrosshairState 并随主题切换更新 isDark，primitive 常驻不重建（颜色经 chartColors 读取）。
 *
 * 注意：primitive 绘制在 pane 内容层 canvas，crosshair 移动只重绘覆盖层——
 * 必须用 attached() 的 requestUpdate() 主动触发刷新（否则标签永不更新）。
 */
import type {
  IPanePrimitive, IPanePrimitivePaneView, IPrimitivePaneRenderer, ISeriesApi, PaneAttachedParameter,
} from 'lightweight-charts'
import type { Time } from '../../utils/klineSeries'
import type { CanvasRenderingTarget2D } from 'fancy-canvas'
import { chartColors } from '../../utils/chartTheme'

/** 十字准星共享状态（引擎持有、primitive 读取；isDark 随主题切换由引擎更新） */
export interface CrosshairState {
  logical: number // 竖线列 bar 索引（-1 = 无 hover，隐藏全部标签）
  yByPane: Record<number, number> // 每 pane 局部 y（横线/浮标位置）
  mousePane: number // 鼠标所在 pane（-1 = 离开图表）
  vertX: number | null // 竖线列在内容区的 x 坐标（所有 pane 同一时间尺度共用；null = 未就绪）
  isDark: boolean // 主题标记：轴星浮标/十字线颜色随主题切换
}

/** 从数据点取竖线列数值：Line/Histogram 取 value、Candlestick 取 close（主图） */
type PointValueOf = (point: Record<string, unknown>) => number | undefined
export const valueOfValue: PointValueOf = (p) => ('value' in p ? (p.value as number | undefined) : undefined)
export const valueOfClose: PointValueOf = (p) => ('close' in p ? (p.close as number | undefined) : undefined)

/** 顶部竖线列标签的一个系列段（x 标签 = 该 pane 全部 series 段拼接一行） */
export interface XSpec {
  series: ISeriesApi<'Line' | 'Histogram' | 'Candlestick'>
  label?: string // 段前缀（副图如「K:」；主图/成交量无前缀）
  color?: string // 段文本色（副图系列色）；缺省用令牌文字色
  fmt: (v: number) => string
  valueOf: PointValueOf // 从数据点取列值（close/value）
}

// measureText 宽度缓存：文本集合有限（价格/指标值格式化串），无界可接受；
// 字体固定 '11px sans-serif'（主题不影响字体尺寸）→ 跨实例/跨主题共享安全。
// 必须模块级：库每次绘制都经 paneView.renderer() 新建 renderer 实例，实例级缓存每次重绘即失效。
const textWidthCache = new Map<string, number>()

class PaneLabelRenderer implements IPrimitivePaneRenderer {
  private state: CrosshairState
  private paneIndex: number
  private ySeries: ISeriesApi<'Line' | 'Histogram' | 'Candlestick'>
  private yFmt: (v: number) => string
  private xSpecs: XSpec[]
  constructor(
    state: CrosshairState,
    paneIndex: number,
    ySeries: ISeriesApi<'Line' | 'Histogram' | 'Candlestick'>,
    yFmt: (v: number) => string,
    xSpecs: XSpec[],
  ) {
    this.state = state
    this.paneIndex = paneIndex
    this.ySeries = ySeries
    this.yFmt = yFmt
    this.xSpecs = xSpecs
  }
  draw(target: CanvasRenderingTarget2D): void {
    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      const st = this.state
      // 轴星浮标样式 + 十字线色（亮/暗两套令牌，从主题表读取——primitive 常驻，主题切换无需重建）
      const tokens = chartColors(st.isDark)
      const label = tokens.label
      const crosshairColor = tokens.crosshair
      const y = st.yByPane[this.paneIndex]
      const yValid = y != null && Number.isFinite(y)
      // ① 贯穿横线：同一绝对 y 贯穿（跳过鼠标所在 pane——库原生横线负责该 pane，防重复）
      if (yValid && y >= 0 && y <= mediaSize.height && this.paneIndex !== st.mousePane) {
        context.save()
        context.strokeStyle = crosshairColor
        context.lineWidth = 1
        context.setLineDash([4, 4])
        context.beginPath()
        context.moveTo(0, y + 0.5)
        context.lineTo(mediaSize.width, y + 0.5) // 到内容区右缘（轴带左缘）
        context.stroke()
        context.restore()
      }
      context.font = '11px sans-serif'
      const labelH = 16
      // ② 右缘 y→值浮标：鼠标 y 在该视图坐标轴映射的值（随 y 实时变化）；
      //    y 在 pane 外时该 pane 无对应值，不画
      if (yValid && y >= 0 && y <= mediaSize.height) {
        const v = this.ySeries.coordinateToPrice(y)
        if (v != null) {
          const text = this.yFmt(v)
          let tw = textWidthCache.get(text)
          if (tw === undefined) { tw = Math.ceil(context.measureText(text).width); textWidthCache.set(text, tw) }
          const w = tw + 12
          const x = mediaSize.width - w - 2 // 右缘贴轴（轴带外侧）
          const y2 = Math.max(0, Math.min(mediaSize.height - labelH, y - labelH / 2))
          context.fillStyle = label.bg
          context.fillRect(x, y2, w, labelH)
          context.strokeStyle = label.border
          context.lineWidth = 1
          context.strokeRect(x + 0.5, y2 + 0.5, w - 1, labelH - 1) // 细边框，浮标与图表底色区隔
          context.fillStyle = label.text
          context.textAlign = 'left'
          context.textBaseline = 'middle'
          context.fillText(text, x + 6, y2 + labelH / 2)
        }
      }
      // ③ 顶部竖线列标签：跟随竖线 x，显示该 bar 的值（主图收盘价/成交量/副图全部线值）
      if (st.logical < 0 || st.vertX == null || st.vertX < 0 || st.vertX > mediaSize.width) return
      const parts: { text: string; color?: string }[] = []
      for (const spec of this.xSpecs) {
        const point = spec.series.dataByIndex(st.logical, -1 /* MismatchDirection.NearestLeft */)
        const v = point ? spec.valueOf(point as unknown as Record<string, unknown>) : undefined
        if (v == null || Number.isNaN(v)) continue // 指标 NaN（除零等）跳过该段
        parts.push({ text: spec.label ? `${spec.label} ${spec.fmt(v)}` : spec.fmt(v), color: spec.color })
      }
      if (parts.length === 0) return
      let w = 0
      for (const p of parts) {
        let tw = textWidthCache.get(p.text)
        if (tw === undefined) { tw = Math.ceil(context.measureText(p.text).width); textWidthCache.set(p.text, tw) }
        w += tw
      }
      w += (parts.length - 1) * 8 + 12 // 段间距 8 + 左右 padding
      const maxW = mediaSize.width - 4
      if (w > maxW) w = maxW
      // 贴竖线右侧（x + 4），右缘/左缘空间不足时自动收敛
      const x = Math.max(2, Math.min(mediaSize.width - w - 2, st.vertX + 4))
      const y2 = 2 // pane 顶部
      context.fillStyle = label.bg
      context.fillRect(x, y2, w, labelH)
      context.strokeStyle = label.border
      context.lineWidth = 1
      context.strokeRect(x + 0.5, y2 + 0.5, w - 1, labelH - 1)
      context.textAlign = 'left'
      context.textBaseline = 'middle'
      let cx = x + 6
      for (const p of parts) {
        context.fillStyle = p.color ?? label.text
        context.fillText(p.text, cx, y2 + labelH / 2)
        let tw = textWidthCache.get(p.text)
        if (tw === undefined) { tw = Math.ceil(context.measureText(p.text).width); textWidthCache.set(p.text, tw) }
        cx += tw + 8
      }
    })
  }
}
class PaneLabelView implements IPanePrimitivePaneView {
  private state: CrosshairState
  private paneIndex: number
  private ySeries: ISeriesApi<'Line' | 'Histogram' | 'Candlestick'>
  private yFmt: (v: number) => string
  private xSpecs: XSpec[]
  constructor(
    state: CrosshairState,
    paneIndex: number,
    ySeries: ISeriesApi<'Line' | 'Histogram' | 'Candlestick'>,
    yFmt: (v: number) => string,
    xSpecs: XSpec[],
  ) {
    this.state = state
    this.paneIndex = paneIndex
    this.ySeries = ySeries
    this.yFmt = yFmt
    this.xSpecs = xSpecs
  }
  renderer(): IPrimitivePaneRenderer {
    return new PaneLabelRenderer(this.state, this.paneIndex, this.ySeries, this.yFmt, this.xSpecs)
  }
}
export class PaneLabelPrimitive implements IPanePrimitive<Time> {
  private state: CrosshairState
  private paneIndex: number
  private ySeries: ISeriesApi<'Line' | 'Histogram' | 'Candlestick'>
  private yFmt: (v: number) => string
  private xSpecs: XSpec[]
  private requestUpdate: (() => void) | null = null
  constructor(
    state: CrosshairState,
    paneIndex: number,
    ySeries: ISeriesApi<'Line' | 'Histogram' | 'Candlestick'>,
    yFmt: (v: number) => string,
    xSpecs: XSpec[],
  ) {
    this.state = state
    this.paneIndex = paneIndex
    this.ySeries = ySeries
    this.yFmt = yFmt
    this.xSpecs = xSpecs
  }
  attached(param: PaneAttachedParameter): void {
    this.requestUpdate = param.requestUpdate
  }
  detached(): void {
    this.requestUpdate = null
  }
  /** crosshair 移动后主动触发内容层重绘（内容层不随 crosshair 自动重绘） */
  notify(): void {
    this.requestUpdate?.()
  }
  paneViews(): IPanePrimitivePaneView[] {
    return [new PaneLabelView(this.state, this.paneIndex, this.ySeries, this.yFmt, this.xSpecs)]
  }
  updateAllViews(): void { /* 渲染循环自动重绘（无状态缓存） */ }
}
