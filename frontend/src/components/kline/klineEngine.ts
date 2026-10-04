/**
 * K 线图引擎层（无 React）：createKlineEngine(container) 创建 lightweight-charts v5 图表实例并管理其
 * 全部生命周期——主图 K 线/成交量常驻 series、动态主图叠加与副图 pane 系统、十字准星 primitive、
 * wheel 缩放/横滚、视野记录、主题/周期/布局重置。
 *
 * React 侧只做状态编排：引擎方法全部参数化（data/overlays/period/isDark…由调用方传入），
 * 引擎自身不缓存业务状态（仅持有图表实例引用与结构键）；crosshair 移动经 callbacks
 * 通知 React 层（rAF 节流 / setState 由壳层负责）。
 *
 * 行为守恒要点（与重构前 KlineChart 一致）：
 * - autoSize:true ResizeObserver 跟随容器；主图/成交量常驻、副图全量重建（removePane 防索引漂移）
 * - 轮询增量（P2-72）：新 data 的 dates 前缀不变（追加/末 bar 更新）→ 全 series 走 update() 尾部增量，
 *   不重建数组、不写回视野；前缀失效（换股/换周期/数据修订）才全量 setData + 重置默认视野
 * - 叠加/副图结构键含 indicators 字段指纹（fetchIndicators 异步就绪后强制重建）
 * - 主题切换走 applyOptions 增量更新（primitive 常驻经 CrosshairState.isDark 换色）
 * - crosshair 布局缓存（P2-70）：pane 顶部偏移缓存 + MutationObserver 失效，交互帧零 getBoundingClientRect
 */
import {
  ColorType, CrosshairMode, LineStyle,
  CandlestickSeries, HistogramSeries, LineSeries,
  createChart, createSeriesMarkers,
} from 'lightweight-charts'
import type {
  IChartApi, ISeriesApi, LogicalRange, MouseEventParams, AutoscaleInfo,
  ISeriesMarkersPluginApi, SeriesMarker,
} from 'lightweight-charts'
import {
  klineCandles, klineVolume, klineLine, klineHistogram, klineTickFormatter, klineTime, fmtVol,
  type Time, type KlineData,
} from '../../utils/klineSeries'
import { SUB_KEYS, SUB_ANNOT, SUB_THRESHOLDS, subHasData } from '../../utils/klineSubConfig'
import { chartColors, resolveChartColor } from '../../utils/chartTheme'
import { isMinutePeriod } from '../../utils/periods'
import { formatShanghaiFullTime } from '../../utils/time'
import { PaneLabelPrimitive, valueOfClose, valueOfValue, type CrosshairState, type XSpec } from './klinePrimitives'
import { createKlineDrawing, type DrawingTool, type DrawingCursor, type DrawingStyle, type KlineDrawingController } from './klineDrawing'

// —— pane 索引约定（副图 pane 系统扩展点，从 PANE_SUB_BASE 起追加）——
const PANE_MAIN = 0 // 主图：K 线 + 均线/BOLL/SAR 叠加
const PANE_VOL = 1 // 成交量：histogram（固定小高度，分隔线可拖）
/** Step 2 副图 pane 起始索引（macd/kdj/rsi… 各占一个 pane）：导出为副图系统接入契约 */
export const PANE_SUB_BASE = 2
// pane 高度分配（stretchFactor 比例制）：
// setHeight 内部会均摊压缩其他 pane（后设置的保留、前面的被压扁——macd 36px/kdj 132px 的不均匀来源）；
// 改用相同 factor → 布局按比例分配，空间不足时所有副图等比例压缩（严格均匀）。
// 主图 2.4（比库默认 2 略大：多副图场景主图 K 线更可读）
const MAIN_STRETCH = 2.4 // 主图 pane 拉伸因子
const VOL_STRETCH = 0.8 // 成交量 pane 拉伸因子
const SUB_STRETCH = 1.0 // 每个副图 pane 拉伸因子（全部相同 → 高度均匀）
// 同屏副图上限：超过时按页窗口取（每页 MAX_SUB_PANES 个），防副图过多挤压主图/成交量至不可读
export const MAX_SUB_PANES = 4
// T-89 wheel 缩放灵敏度：exp(-deltaY*ZOOM_SPEED)，deltaY 像素制（120=一格大幅、2=像素级微调）
const ZOOM_SPEED = 0.0022
// 键盘缩放灵敏度（P1-a11y-4）：单格滚轮因子 exp(-120*ZOOM_SPEED)≈0.77，与 wheel 缩放同源（React 壳层键盘共用）
export const KEY_ZOOM_FACTOR = Math.exp(-120 * ZOOM_SPEED)

// 主图叠加均线 key（ma/ema/expma；均不在 SUB_KEYS 中；图例条共用）
export const MAIN_LINE_KEYS = ['ma5', 'ma10', 'ma20', 'ma30', 'ma60', 'ma120', 'ma250',
  'ema12', 'ema26', 'expma12', 'expma50'] as const
// BOLL 三轨字段（indicators 字段名 → 显示名；overlays 开关 key 为 'boll'；图例条共用）
export const BOLL_KEYS = [['boll_upper', '上轨'], ['boll_mid', '中轨'], ['boll_lower', '下轨']] as const

// 自适应初始视野（P1-11 语义）：K 线根数或 period 变化时重置到默认视野；
// 轮询产生新 data 数组（引用恒变）但根数不变 → 沿用用户缩放，不被每 60s 重置
const INIT_BARS: Record<string, number> = {
  '1': 240, '5': 240, '15': 240, '30': 240, '60': 240,
  daily: 120, weekly: 120, monthly: 80,
}

/**
 * P2-72 轮询增量判定：新 data 的 dates 是否为旧 data 的尾部扩展（前缀相同）。
 * 仅前缀相同（轮询追加新 bar / 末 bar 在途更新）才可走 series.update 尾部增量；换股/换周期/
 * 数据修订（前缀失效）必须全量 setData，否则历史数据错位/缺失。
 */
function isPrefix(prev: string[], next: string[]): boolean {
  if (prev.length > next.length) return false
  for (let i = 0; i < prev.length; i++) {
    if (prev[i] !== next[i]) return false
  }
  return true
}

/** 从 from 起切片 K 线数据（indicator 数组与 dates 等长对齐，backend compute_all 逐根对齐，按索引切安全） */
function sliceKline(data: KlineData, from: number): KlineData {
  const indicators: Record<string, number[]> | undefined = data.indicators
    ? Object.fromEntries(Object.entries(data.indicators).map(([k, v]) => [k, v.slice(from)]))
    : undefined
  return {
    dates: data.dates.slice(from),
    open: data.open.slice(from),
    high: data.high.slice(from),
    low: data.low.slice(from),
    close: data.close.slice(from),
    volume: data.volume.slice(from),
    indicators,
  }
}

/** crosshair 移动通知（React 壳层做 rAF 节流 + setState；null = 离开图表，保留最后值语义） */
export interface KlineEngineCallbacks {
  onCrosshairMove?: (logical: number | null) => void
  /** 画线数量变化（T-14：React 壳层消费——清除按钮可用性等） */
  onDrawingChange?: (count: number) => void
  /** T-68 画线 hover 光标（React 壳层消费：图表容器 CSS cursor；变化才回调） */
  onCursorChange?: (cursor: DrawingCursor) => void
  /** T-68 画线右键菜单（React 壳层消费：x/y 为 clientX/clientY 屏幕坐标；lineId 为命中的线） */
  onContextMenu?: (info: { x: number; y: number; lineId: string }) => void
  /** T-68 画线选中变化（React 壳层消费：样式面板显隐与受控值；null = 无选中） */
  onSelectionChange?: (sel: { id: string; style: DrawingStyle } | null) => void
}

/**
 * T-117 信号标记输入（设计落地，暂不接线显示）：time=bar 标签时点（naive ISO 串，如 '2026-08-07' 或
 * '2026-08-07 15:00:00'）；direction=方向（up 买入→bar 下方向上箭头，down 卖出→bar 上方向下箭头）；
 * text=标记文字（信号名等）；color=标记色（缺省按 direction 用涨/跌色）。引擎按当前周期转 Time
 * 并经 createSeriesMarkers（v5）渲染；「显示开关」= 工作台是否传 prop，机制本身始终可用。
 */
export interface SignalMarkerInput {
  time: string
  direction: 'up' | 'down'
  text?: string
  color?: string
}

/** 引擎对外 API（React 壳层 useImperativeHandle 与各 effect 消费） */
export interface KlineEngine {
  getChart(): IChartApi | null
  /** 刷 K 线/成交量数据 + 视野保持/重置（up/down 为 per-bar 量柱色，主题敏感色由调用方传入） */
  setData(data: KlineData | null, period: string, up: string, down: string): void
  /** 主题切换：applyOptions 增量更新 + 主图/成交量 series 换色 + CrosshairState.isDark（primitive 换色） */
  applyTheme(isDark: boolean): void
  /** 周期切换：时间刻度格式 + 分钟级 timeVisible */
  applyPeriod(period: string): void
  /** 主图动态叠加（均线/BOLL/SAR）：结构键变化全量重建，否则仅刷数据 */
  setOverlays(data: KlineData | null, overlays: string[], period: string, isDark: boolean): void
  /** 副图 pane 系统：activeSubs 结构变化全量重建（removePane 防索引漂移），否则仅刷数据 */
  setSubPanes(data: KlineData | null, activeSubs: string[], period: string, isDark: boolean): void
  /** 恢复全部 pane 默认高度（工具栏「重置布局」） */
  resetLayout(): void
  /** T-14 画线：切换工具 + 股票代码（代码变化时加载/恢复该股画线，键 sr-kline-tools:{code}） */
  setDrawingTool(tool: DrawingTool, code: string): void
  /** T-14 画线：清除当前股票全部画线（工具栏「清除全部」） */
  clearDrawings(): void
  /** T-68 画线：删除指定线（右键菜单「删除此线」：无需先选中） */
  deleteDrawingLine(id: string): void
  /** T-68 画线：设置线样式（样式面板驱动；写入 style 字段持久化） */
  setDrawingLineStyle(id: string, style: DrawingStyle): void
  /** 截图（HTMLCanvasElement，React 壳层转 toDataURL；T-14/T-15 使用） */
  takeScreenshot(): HTMLCanvasElement | null
  /** T-117 信号标记（设计落地：机制可用，工作台暂不接线 → 不显示）；空数组清空 */
  setSignalMarkers(markers: SignalMarkerInput[]): void
  /** T-117 程序化十字线定位：naive 时点串 → 滚动至可见范围 + 十字线钉在该 bar（时间线「定位」入口）；成功 true */
  locateToTime(t: string): boolean
  /** 释放 chart/订阅/事件监听（组件卸载） */
  dispose(): void
}

/**
 * 创建引擎：container 必须是已挂载 DOM 元素；isDark 为创建时主题、initialPeriod 为创建时周期
 * （初始配置一次性写入，后续变化走 applyTheme/applyPeriod）。创建一次、dispose 一次，与 React 生命周期绑定。
 */
export function createKlineEngine(
  container: HTMLElement,
  isDark: boolean,
  callbacks: KlineEngineCallbacks = {},
  initialPeriod = 'daily',
): KlineEngine {
  const tokens = chartColors(isDark)
  // 创建期周期（timeScale 初始配置用；后续 applyPeriod 接管）
  const periodRef = { current: initialPeriod }
  // P2-70 crosshair 布局缓存：pane 顶部偏移只在 容器尺寸/pane 增删/行高（拖分隔线/resetLayout）变化时变，
  // 交互帧不重测。失效点：① MutationObserver 监听图表 table 子树结构/行样式（autoSize 缩放、拖分隔线、
  // pane 增删都会改 style.height / childList，微任务即失效，先于下一次 mouse move）
  //         ② 引擎自身 pane 增删/重置路径显式置 dirty（双保险）
  //         ③ onCrosshair 内 panes 数量兜底比对（防失效时序窗口）
  const paneTopsCache: { tops: number[]; dirty: boolean } = { tops: [], dirty: true }
  const chart = createChart(container, {
    autoSize: true, // ResizeObserver 跟随容器尺寸（height prop 或 flex 布局都自动适配）
    layout: {
      background: { type: ColorType.Solid, color: tokens.bg },
      textColor: tokens.axis,
      fontSize: 11,
      panes: { enableResize: true, separatorColor: tokens.border, separatorHoverColor: 'rgba(148, 163, 184, 0.2)' },
      attributionLogo: true,
    },
    grid: {
      vertLines: { color: tokens.grid },
      horzLines: { color: tokens.grid },
    },
    rightPriceScale: {
      borderColor: tokens.border,
      // 价格轴动态缩放：autoscale 随可见区间自动重算尺度（显式声明，防未来回归）
      autoScale: true,
      // 主图价格轴留白（K 线上下各 10%，比默认 20% 更饱满）
      scaleMargins: { top: 0.1, bottom: 0.1 },
    },
    timeScale: {
      borderColor: tokens.border,
      timeVisible: isMinutePeriod(periodRef.current),
      secondsVisible: false,
      tickMarkFormatter: klineTickFormatter(periodRef.current),
      // 右侧留 4 根 bar 空位（TradingView 风格，滚动到实时时 K 线不贴边）
      rightOffset: 4,
    },
    // 交互（T-89 wheel 自定义）：库原生 mouseWheel 全部关闭（缩放/横滚都不吃），由下方容器级
    // wheel 监听自实现——普通上下滚轮/Ctrl+滚轮=图表缩放、左右滚轮（deltaX 主导）=图表横滚
    // （悬停图表即吞页面滚动，专业行情软件语义）；轴拖动/捏合/拖拽平移/触摸横滚保留库默认
    handleScale: { axisPressedMouseMove: true, mouseWheel: false, pinch: true },
    handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    crosshair: {
      mode: CrosshairMode.Normal, // 自由十字线（与 echarts 时代行为一致；竖线贯穿所有 pane）
      // 十字线颜色令牌化（亮/暗两套，见 chartTokens.crosshair；浅色细线虚线，0.55 alpha 双主题清晰）
      // vertLine 轴带标签底色也用 label 令牌（亮色浅底深字，取代固定 #334155 深底白字）
      vertLine: { color: tokens.crosshair, width: 1, style: LineStyle.Dashed, labelBackgroundColor: tokens.label.bg },
      // labelVisible: false——关闭库原生轴带标签：全部 pane 统一用 PaneLabel primitive
      // （右缘 y→值浮标 + 顶部竖线列标签，主图/成交量/副图一致）；横线本身保留
      horzLine: { color: tokens.crosshair, width: 1, style: LineStyle.Dashed, labelBackgroundColor: tokens.label.bg, labelVisible: false },
    },
    // 副图 overlay scale 默认 margins（vol 已改用 left scale，其 margins 在 leftPriceScale 配置）
    overlayPriceScales: { scaleMargins: { top: 0.1, bottom: 0 } },
    // 价格轴/十字线价格标签统一 A 股 2 位小数（成交量 series 有 series 级 priceFormat: volume，优先于全局 formatter）
    localization: {
      priceFormatter: (p: number) => p.toFixed(2),
      // T-137:十字线时间标签按上海时区直解(时间轴数据为 naive 上海串编码的 UTC 秒)。
      // 缺省实现按浏览器本地时区渲染 UTC 秒——非 UTC+8 浏览器分钟悬停标签偏移;
      // 复用 formatShanghaiFullTime(+8h 取 UTC 字段,与全站 naive 直解口径一致)。
      timeFormatter: (time: Time, isDateLabel: boolean): string => {
        if (typeof time === 'string') return time // 日/周/月字符串时间直接显示
        const ms = Number(time) * 1000
        if (isDateLabel) return formatShanghaiFullTime(ms, { withYear: true, withSeconds: false }).slice(0, 10)
        return formatShanghaiFullTime(ms, { withYear: false, withSeconds: false }) // MM-DD HH:mm
      },
    },
  })
  // 主图拉伸因子（默认 2 → 2.4：多副图场景主图 K 线更可读）
  chart.panes()[PANE_MAIN]?.setStretchFactor(MAIN_STRETCH)

  // P2-70：图表根 div(.tv-lightweight-charts)内 table 子树——结构/行样式变化 → crosshair 布局缓存失效
  const layoutObserver = new MutationObserver(() => { paneTopsCache.dirty = true })
  layoutObserver.observe(container.querySelector('.tv-lightweight-charts') ?? container, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ['style'],
  })

  // 十字准星共享状态：副图/成交量 pane 数值标签读取（-1 = 无 hover，隐藏全部标签）；
  // isDark 由 applyTheme 同步（primitive 常驻不重建，颜色按主题从 state 读取）
  const crosshairState: CrosshairState = { logical: -1, yByPane: {}, mousePane: -1, vertX: null, isDark }

  // 主图 K 线（pane 0，常驻）
  const candle = chart.addSeries(CandlestickSeries, {
    upColor: tokens.up, downColor: tokens.down,
    borderVisible: false, wickUpColor: tokens.up, wickDownColor: tokens.down,
    priceLineVisible: false, lastValueVisible: true,
  }, PANE_MAIN)
  // 主图也挂统一双值标签（右缘 y→价格浮标 + 顶部竖线列收盘价；库原生轴标签已关闭）
  const mainPrim = new PaneLabelPrimitive(
    crosshairState, PANE_MAIN, candle, (p) => p.toFixed(2),
    [{ series: candle, fmt: (p) => p.toFixed(2), valueOf: valueOfClose }],
  )
  chart.panes()[PANE_MAIN]?.attachPrimitive(mainPrim)

  // 成交量 histogram（pane 1：addSeries 指定 paneIndex 自动创建该 pane；红绿逐 bar 由 klineVolume 数据传入）
  // priceScaleId 'vol'（自定义 overlay scale）：成交量刻度轴显示在 pane 右侧
  const vol = chart.addSeries(HistogramSeries, {
    // 成交量轴刻度 A 股中文单位（万/亿，与信息条 fmtVol 同语义）
    priceFormat: { type: 'custom', formatter: (p: number) => fmtVol(p), minMove: 1 },
    priceScaleId: 'vol',
    color: tokens.up,
    priceLineVisible: false, lastValueVisible: true,
  }, PANE_VOL)
  // 右侧轴刻度显式可见 + 柱贴底 margins（overlay scale 默认隐藏轴，SKILL foot-gun；
  // 必须用 series.priceScale()——其 PriceScaleApi 带 series 所在 pane 索引，chart.priceScale(id) 查不到）
  vol.priceScale().applyOptions({ visible: true, scaleMargins: { top: 0.1, bottom: 0 } })
  // 统一双值标签（右缘 y→成交量浮标 + 顶部竖线列成交量；所有 pane 一致）
  const volPrim = new PaneLabelPrimitive(
    crosshairState, PANE_VOL, vol, fmtVol,
    [{ series: vol, fmt: fmtVol, valueOf: valueOfValue }],
  )
  chart.panes()[PANE_VOL]?.attachPrimitive(volPrim)

  // T-14 画线引擎：pane primitive 自绘（趋势线/水平线/斐波那契/测量），挂主 pane 跟随缩放/平移；
  // 持久化键 sr-kline-tools:{code}——code 初始空串（不持久化），KlineChart setDrawingTool 传真实代码后加载；
  // T-68 右键菜单/hover 光标/选中样式回调经 callbacks 通知 React 层
  const drawing: KlineDrawingController = createKlineDrawing({
    chart, series: candle, container, isDark,
    onDrawingChange: (count) => callbacks.onDrawingChange?.(count),
    onCursorChange: (cursor) => callbacks.onCursorChange?.(cursor),
    onContextMenu: (info) => callbacks.onContextMenu?.(info),
    onSelectionChange: (sel) => callbacks.onSelectionChange?.(sel),
  })

  // —— 实例引用（全部闭包；dispose 时清空） ——
  // 已挂载的 PaneLabel primitive（crosshair 移动后主动 notify 触发内容层重绘）：
  // 拆两组——fixedPrims 主图+成交量常驻（创建时注册一次）；subPrims 副图动态
  // （副图重建只清 subPrims，主图/成交量 primitive 不被波及 → 修复重建后十字准星标签冻结）
  const fixedPrims: PaneLabelPrimitive[] = [mainPrim, volPrim]
  const subPrims: PaneLabelPrimitive[] = []
  // 动态叠加 series（均线/BOLL/SAR）：结构（overlays/period/主题）变化时整体重建；轮询仅刷数据
  const overlaySeries: { field: string; s: ISeriesApi<'Line'> }[] = []
  let overlayStructKey = ''
  // 副图 pane 系统：{ key, entries } 持久引用——结构变化全量重建；轮询仅 setData（防 60s 轮询闪动）
  const subPanes: { key: string; entries: { field: string; s: ISeriesApi<'Line' | 'Histogram'> }[] }[] = []
  let subStructKey = ''
  // 用户缩放/平移视野记录（P1-11：轮询更新沿用，根数/period 变化才重置）
  let visibleRange: LogicalRange | null = null
  // 数据指纹（根数 + period）：根数/period 变化 → 重置默认视野
  let dataKey: { count: number; period: string } | null = null
  // T-117 信号标记（设计落地）：原始输入 + 已挂 v5 markers primitive；空输入清空显示
  let signalMarkersRaw: SignalMarkerInput[] = []
  let markersPrim: ISeriesMarkersPluginApi<Time> | null = null
  // 标记渲染：按当前周期转 Time、按主题取涨跌色（direction 缺省色）；primitive 懒建复用
  const applyMarkers = (): void => {
    const tokens = chartColors(crosshairState.isDark)
    const ms: SeriesMarker<Time>[] = signalMarkersRaw.map((m) => ({
      time: klineTime(m.time, periodRef.current),
      position: m.direction === 'up' ? 'belowBar' : 'aboveBar',
      shape: m.direction === 'up' ? 'arrowUp' : 'arrowDown',
      color: m.color ?? (m.direction === 'up' ? tokens.up : tokens.down),
      text: m.text,
    }))
    if (ms.length === 0) {
      markersPrim?.setMarkers([])
      return
    }
    if (!markersPrim) markersPrim = createSeriesMarkers(candle, ms)
    else markersPrim.setMarkers(ms)
  }

  // —— P2-72 轮询增量：各组「上次消费的数据」单一事实源 ——
  // setData 先于 setOverlays/setSubPanes 运行（React effect 声明序），但三组各自独立判定——
  // 换股时 setData 已全量重建，而叠加/副图若结构键未变（indFp 相同）会误判增量，故每组记自己的 last。
  let lastKline: KlineData | null = null // setData 组（主图/成交量）
  let overlayLast: KlineData | null = null // 主图叠加组
  let subLast: KlineData | null = null // 副图 pane 组
  let lastPeriod = initialPeriod
  // 增量判定：prev 为该组上次消费的 data；前缀未变 → 尾部切片（旧末 bar 起，可能已更新）+ 新 bar
  const incOf = (prev: KlineData | null, data: KlineData): { ok: boolean; tail: KlineData } => {
    if (!prev || data.dates.length < prev.dates.length) return { ok: false, tail: data }
    return isPrefix(prev.dates, data.dates)
      ? { ok: true, tail: sliceKline(data, prev.dates.length - 1) }
      : { ok: false, tail: data }
  }

  // 悬停 → 顶部 OHLC 信息条（TradingView 风格：悬停即更新；param.point 为空=离开图表 → 保留最后值，
  // 与顶部常驻信息条语义一致——轮询/重绘不打断用户最后查看的 bar）
  const onCrosshair = (param: MouseEventParams<Time>) => {
    // 准星状态同步：logical（竖线列）+ 每 pane 局部 y（横线/轴带标签；同一绝对 y 贯穿所有 pane）
    const st = crosshairState
    st.logical = param.point && param.logical != null ? Math.round(param.logical) : -1
    st.mousePane = param.point ? (param.paneIndex ?? 0) : -1 // 横线去重：primitive 跳过鼠标 pane
    if (param.point) {
      // param.point.y 是「相对鼠标所在 pane 顶部」的坐标（非图表容器）。P2-70：
      // pane 顶部偏移用缓存（失效才重测一次 getBoundingClientRect），避免每帧对每个 pane
      // 同步强制布局 + closest；panes 数量兜底比对覆盖任何失效时序窗口。
      const panes = chart.panes()
      if (paneTopsCache.dirty || paneTopsCache.tops.length !== panes.length) {
        const tops: number[] = []
        for (let pi = 0; pi < panes.length; pi++) {
          tops[pi] = panes[pi].getHTMLElement()?.getBoundingClientRect().top ?? 0
        }
        paneTopsCache.tops = tops
        paneTopsCache.dirty = false
      }
      const tops = paneTopsCache.tops
      const mousePane = param.paneIndex ?? 0
      // 以 pane0 顶部（=图表内容区顶部）为基准：同视口坐标系内相减 → 绝对 y 贯穿所有 pane，
      // 每 pane 局部 y = 绝对 y - 该 pane 顶部偏移（与旧 contTop 归一语义等价，且不依赖 closest）
      const absY = param.point.y + ((tops[mousePane] ?? 0) - (tops[0] ?? 0))
      st.yByPane = {}
      for (let pi = 0; pi < panes.length; pi++) {
        st.yByPane[pi] = absY - ((tops[pi] ?? 0) - (tops[0] ?? 0))
      }
      // 竖线列 x（内容区坐标，与库原生竖线同源）：顶部 x 标签定位用（param.logical 为 Logical 品牌类型，直接用）
      st.vertX = param.logical != null ? chart.timeScale().logicalToCoordinate(param.logical) : null
    } else {
      st.yByPane = {}
      st.vertX = null
    }
    // 内容层 canvas 不随 crosshair 自动重绘：主动 notify 所有 PaneLabel primitive
    // （fixedPrims 常驻 + subPrims 动态，两组都要；副图重建只影响 subPrims）
    for (const p of [...fixedPrims, ...subPrims]) p.notify()
    // 离开图表：保留最后值（既有语义，不 setState 也不排 rAF）
    if (!param.point || param.logical == null) return
    callbacks.onCrosshairMove?.(Math.round(param.logical))
  }
  chart.subscribeCrosshairMove(onCrosshair)

  // ===== T-89 wheel 自定义：容器级原生监听（passive:false 可 preventDefault） =====
  // 设计（为何不用库原生）：库 mouseWheel 无灵敏度选项；自实现二分支（专业行情软件语义，
  // 悬停图表即吞页面滚动——2026-08-15 用户确认接受的权衡）：
  //   ① 普通上下滚轮（deltaY 主导）或 Ctrl/⌘/Alt+滚轮 → 图表缩放（以鼠标所在 bar 为不动点，
  //      exp 灵敏度与 delta 连续相关；preventDefault 同时拦浏览器 ctrl+wheel 页面缩放）
  //   ② 左右滚轮/两指 trackpad 横滚（deltaX 主导）→ 图表横滚（替换 handleScroll.mouseWheel 的横滚职责）
  // handleScale.mouseWheel/handleScroll.mouseWheel 均已置 false；捏合 pinch 保留库默认。
  const normWheelDelta = (d: number, mode: number): number => {
    if (mode === WheelEvent.DOM_DELTA_LINE) return d * 32 // LINE → 像素（与库内部同换算）
    if (mode === WheelEvent.DOM_DELTA_PAGE) return d * 120 // PAGE → 像素
    return d
  }
  const onWheel = (e: WheelEvent) => {
    const ax = Math.abs(e.deltaX)
    const ay = Math.abs(e.deltaY)
    const isHorzPan = ax > ay * 1.5 && ax > 4 // 横滚主导（鼠标侧滚轮/两指 trackpad 横滚）→ 左右平移
    if (isHorzPan && !(e.ctrlKey || e.metaKey || e.altKey)) {
      // ② 左右平移：按可见范围/内容宽度换算每像素 logical，随 deltaX 平移
      e.preventDefault()
      e.stopPropagation()
      const ts = chart.timeScale()
      const range = ts.getVisibleLogicalRange()
      if (!range) return
      const width = Math.max(1, container.clientWidth - 60) // 内容区宽（减右侧价格轴近似）
      const perPx = (range.to - range.from) / width
      const delta = normWheelDelta(e.deltaX, e.deltaMode) * perPx
      ts.setVisibleLogicalRange({ from: range.from + delta, to: range.to + delta })
    } else {
      // ① 缩放（普通上下滚轮 / Ctrl/⌘/Alt+滚轮；阻止浏览器 ctrl+wheel 页面缩放）：
      //    以鼠标 logical 为不动点
      e.preventDefault()
      e.stopPropagation()
      const ts = chart.timeScale()
      const range = ts.getVisibleLogicalRange()
      if (!range || range.to <= range.from) return
      const rect = container.getBoundingClientRect()
      const mouseLogical = ts.coordinateToLogical(e.clientX - rect.left)
      const span = range.to - range.from
      const ratio = mouseLogical != null ? Math.max(0, Math.min(1, (mouseLogical - range.from) / span)) : 0.5
      const dy = normWheelDelta(e.deltaY, e.deltaMode)
      // 缩放灵敏度：exp(-dy*ZOOM_SPEED) 与 delta 连续相关（120=大幅、2=微调），下限 5 根防过缩
      const factor = Math.exp(-dy * ZOOM_SPEED)
      const newSpan = Math.max(5, span * factor)
      const newFrom = range.from + (span - newSpan) * ratio
      ts.setVisibleLogicalRange({ from: newFrom, to: newFrom + newSpan })
    }
  }
  container.addEventListener('wheel', onWheel, { passive: false })

  // 用户缩放/平移视野记录（P1-11 替代 echarts zoomRef）：程序 setVisibleLogicalRange 也会回调，一并记录
  const onVisibleRange = (range: LogicalRange | null) => {
    if (range) visibleRange = range
  }
  chart.timeScale().subscribeVisibleLogicalRangeChange(onVisibleRange)

  const engine: KlineEngine = {
    getChart: () => chart,

    setData(data, period, up, down) {
      // 空态：lightweight 无 chart.clear，setData([]) 即清空
      if (!data || data.dates.length === 0) {
        candle.setData([])
        vol.setData([])
        dataKey = null
        lastKline = null
        return
      }
      const n = data.dates.length
      const inc = incOf(lastKline, data)
      if (inc.ok && lastPeriod === period) {
        // P2-72 轮询增量：dates 前缀未变（追加新 bar / 末 bar 在途更新）→ 只 update 尾部，
        // 不重建全量数组；update 不重置可见范围 → 视野天然保留（不再每次写回 visibleRange）
        const c = klineCandles(inc.tail, period)
        const v = klineVolume(inc.tail, period, up, down)
        for (const p of c) candle.update(p)
        for (const p of v) vol.update(p)
        dataKey = { count: n, period }
        lastKline = data
        lastPeriod = period
        return
      }
      // 全量路径（首载/换股/换周期/数据修订）：重建 + 视野重置/沿用（P1-11 既有语义）
      candle.setData(klineCandles(data, period))
      vol.setData(klineVolume(data, period, up, down))
      if (!dataKey || dataKey.count !== n || dataKey.period !== period) {
        dataKey = { count: n, period }
        const wantBars = INIT_BARS[period] ?? 120
        chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - wantBars), to: n - 1 })
      } else if (visibleRange) {
        chart.timeScale().setVisibleLogicalRange(visibleRange)
      }
      lastKline = data
      lastPeriod = period
    },

    applyTheme(dark) {
      const t = chartColors(dark)
      crosshairState.isDark = dark
      drawing.setTheme(dark) // 画线渲染色随主题（primitive 常驻不重建）
      chart.applyOptions({
        layout: {
          background: { type: ColorType.Solid, color: t.bg },
          textColor: t.axis,
          panes: { separatorColor: t.border },
        },
        grid: {
          vertLines: { color: t.grid },
          horzLines: { color: t.grid },
        },
        rightPriceScale: { borderColor: t.border },
        timeScale: { borderColor: t.border },
        // 十字线/轴带标签颜色随主题（与 PaneLabel primitive 的 label 令牌同源）
        crosshair: {
          vertLine: {
            color: t.crosshair,
            labelBackgroundColor: t.label.bg,
          },
          horzLine: {
            color: t.crosshair,
            labelBackgroundColor: t.label.bg,
          },
        },
      })
      candle.applyOptions({ upColor: t.up, downColor: t.down, wickUpColor: t.up, wickDownColor: t.down })
      vol.applyOptions({ color: t.up })
    },

    applyPeriod(period) {
      // T-133:periodRef 必须随 applyPeriod 同步——locateToTime/INIT_BARS 换算
      // 依赖当前周期,此前只在创建时记录初始值,切周期后定位仍按旧周期换算(错位)
      periodRef.current = period
      const ts = chart.timeScale()
      ts.applyOptions({ timeVisible: isMinutePeriod(period), secondsVisible: false })
      // 库类型缺陷：ITimeScaleApi.applyOptions 参数为 DeepPartial<HorzScaleOptions>（基类），
      // tickMarkFormatter 实际定义在 TimeScaleOptions，运行时合并合法。
      // 注意：必须 ts.applyOptions(...) 直接调用保留 this 绑定——解绑裸调会触发库内部
      // "Cannot read properties of undefined (reading '_private__timeScale')" 崩溃。
      ts.applyOptions({ tickMarkFormatter: klineTickFormatter(period) } as Parameters<typeof ts.applyOptions>[0])
      // T-117 周期变化 → 标记 time 换算重跑（分钟/日线时间格式不同）
      applyMarkers()
    },

    setOverlays(data, overlays, period, dark) {
      const t = chartColors(dark)
      const hasData = !!data && data.dates.length > 0
      // overlays 分流：副图 key（SUB_KEYS，副图 pane 渲染）不参与主图叠加；主图叠加 key 均不在 SUB_KEYS
      const mainKeys = overlays.filter((k) => !SUB_KEYS.includes(k))
      const indFp = Object.keys(data?.indicators ?? {}).sort().join(',')
      const key = `${mainKeys.join(',')}|${period}|${dark}|${hasData}|${indFp}`
      if (overlayStructKey === key) {
        // 结构未变：仅刷新数据（轮询；key 含 hasData，此处 data 必非空）。
        // P2-72 增量：该组上次消费数据（overlayLast）前缀未变 → update 尾部；否则（换股/数据修订）
        // 全量 setData——换股时 setData 已全量重建但本组结构键未变，必须用自己的 last 判定
        if (!data) return
        const inc = incOf(overlayLast, data)
        const src = inc.ok ? inc.tail : data
        for (const e of overlaySeries) {
          const pts = klineLine(e.field, src, period)
          if (inc.ok) {
            if (pts.length === 0) continue // 尾部无点（末 bar 指标 null）：无可更新
            for (const p of pts) e.s.update(p)
          } else {
            e.s.setData(pts)
          }
        }
        overlayLast = data
        return
      }
      overlayStructKey = key
      for (const e of overlaySeries) chart.removeSeries(e.s)
      overlaySeries.length = 0
      overlayLast = data
      if (!hasData) return

      const addLine = (k: string, color: string, lineStyle?: LineStyle) => {
        const v = data?.indicators?.[k]
        if (!v) return // indicators 缺失该字段：跳过，不建 series（与 echarts 时代一致）
        const s = chart.addSeries(LineSeries, {
          color, lineWidth: 1, lineStyle,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        }, PANE_MAIN)
        s.setData(klineLine(k, data, period))
        overlaySeries.push({ field: k, s })
      }

      // 均线（标准色 + 扩展色板）+ EMA/EXPMA
      let lineColorIdx = 4 // ma5/10/20/60 有 MA_COLORS，其余从扩展色板第 4 色起
      for (const k of MAIN_LINE_KEYS) {
        if (!mainKeys.includes(k)) continue
        addLine(k, t.maColors[k] ?? t.lineColors[lineColorIdx++ % t.lineColors.length])
      }
      // BOLL：上/下轨虚线、中轨实线（颜色走 tokens.boll 双套令牌，图例与线条同源）
      if (mainKeys.includes('boll')) {
        for (const [k] of BOLL_KEYS) {
          addLine(k, t.boll, k === 'boll_mid' ? undefined : LineStyle.Dashed)
        }
      }
      // SAR：简化连线式（颜色沿用现有涨色；与 echarts scatter 散点的视觉差异见重构报告）
      if (mainKeys.includes('sar')) {
        addLine('sar', t.up)
      }
    },

    setSubPanes(data, activeSubs, period, dark) {
      const hasData = !!data && data.dates.length > 0
      // 结构键（activeSubs/period/主题/有无数据/indicators 字段指纹）变化 → 全量重建（规避 pane 索引漂移，
      // SKILL：index can move）；轮询 data 引用变化但结构键不变 → 仅 setData（防 60s 轮询 removePane+addPane 闪动）。
      // indFp 语义同主图叠加：fetchIndicators 异步就绪后含新字段 → 指纹变化 → 强制重建副图 pane
      // （修复"点击指标后副图不建"时序 bug 根因）。
      const indFp = Object.keys(data?.indicators ?? {}).sort().join(',')
      const key = `${activeSubs.join(',')}|${period}|${dark}|${hasData}|${indFp}`
      if (subStructKey === key) {
        // 结构未变：仅刷新数据（轮询；key 含 hasData，此处 data 必非空；
        // MACD 柱双色随 UP/DOWN 不变——主题变化已进结构键）。
        // P2-72 增量：该组上次消费数据（subLast）前缀未变 → update 尾部；否则全量 setData
        if (!data) return
        const t = chartColors(dark)
        const inc = incOf(subLast, data)
        const src = inc.ok ? inc.tail : data
        for (const sub of subPanes) {
          for (const e of sub.entries) {
            const pts = sub.key === 'macd' && e.field === 'macd_hist'
              ? klineHistogram(e.field, src, period, (v) => (v >= 0 ? t.up + 'cc' : t.down + 'cc'))
              : klineLine(e.field, src, period)
            if (inc.ok) {
              if (pts.length === 0) continue // 尾部无点：无可更新
              for (const p of pts) e.s.update(p)
            } else {
              e.s.setData(pts)
            }
          }
        }
        subLast = data
        return
      }
      subStructKey = key
      subPanes.length = 0
      // 只清副图 primitive 注册（旧副图 primitive 随 pane 销毁）；主图/成交量 primitive 在 fixedPrims
      // 不受波及——修复副图重建后主图/成交量十字准星标签冻结（旧实现清空全量列表导致它们不再被 notify）
      subPrims.length = 0
      subLast = data
      // 移除 PANE_SUB_BASE 及之后全部副图 pane（从末尾向前删，索引不越界）
      while (chart.panes().length > PANE_SUB_BASE) {
        chart.removePane(chart.panes().length - 1)
      }
      paneTopsCache.dirty = true // P2-70：pane 增删 → crosshair 布局缓存失效
      if (!hasData) return
      const t = chartColors(dark)

      for (const subKey of activeSubs) {
        if (!subHasData(subKey, data.indicators)) continue // 该指标无数据：不建 pane（与主图叠加跳过一致）
        const cfg = SUB_ANNOT[subKey]
        if (!cfg) continue
        const pane = chart.addPane() // 空 pane：索引 = PANE_SUB_BASE + 已建副图数
        // 副图高度统一 factor（所有副图相同 → 严格均匀；用户可拖分隔线调整）
        pane.setStretchFactor(SUB_STRETCH)
        const idx = pane.paneIndex()
        const entries: { field: string; s: ISeriesApi<'Line' | 'Histogram'> }[] = []
        for (const it of cfg.items) {
          const field = it.field
          // 仅 macd_hist 用柱状（SUB_PANE_KIND 语义：macd 含柱状系列），其余为折线
          if (subKey === 'macd' && field === 'macd_hist') {
            const s = chart.addSeries(HistogramSeries, {
              color: resolveChartColor(it.color, dark), // series 级兜底色（数据点 color 优先覆盖）
              priceLineVisible: false, lastValueVisible: false,
            }, idx)
            // MACD 柱双色（A 股红涨绿跌）：正值涨色、负值跌色，80% 不透明度（hex alpha 后缀）
            s.setData(klineHistogram(field, data, period, (v) => (v >= 0 ? t.up + 'cc' : t.down + 'cc')))
            entries.push({ field, s })
          } else {
            const s = chart.addSeries(LineSeries, {
              color: resolveChartColor(it.color, dark),
              lineWidth: 1,
              priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
            }, idx)
            s.setData(klineLine(field, data, period))
            entries.push({ field, s })
          }
        }
        // 阈值线（SUB_THRESHOLDS：rsi 30/70、kdj 20/80…）：挂在 pane 首条 series 的 price line 上
        const ths = SUB_THRESHOLDS[subKey]
        if (ths && entries[0]) {
          // 标尺自适应（防数值较小时图形不可见）：数据值域不覆盖阈值时，自动标尺范围扩展到阈值，
          // 否则阈值线落在可视范围外（如 RSI 数据 40-60 时 30/70 线不可见）。
          // 注意参数是「默认 provider 函数」（官方契约 original => ...），须调用 original() 取范围；
          // 直接当对象取 priceRange 会导致恒返回 null → series 失去 autoscale 数据 → 不渲染。
          entries[0].s.applyOptions({
            autoscaleInfoProvider: (original: () => AutoscaleInfo | null): AutoscaleInfo | null => {
              const res = original()
              if (res === null || res.priceRange === null) return res // 无数据：走默认
              let min = res.priceRange.minValue
              let max = res.priceRange.maxValue
              for (const p of ths) {
                if (p < min) min = p
                if (p > max) max = p
              }
              res.priceRange.minValue = min // 原地修改（官方示例风格，零分配）
              res.priceRange.maxValue = max
              return res
            },
          })
          for (const p of ths) {
            entries[0].s.createPriceLine({
              price: p, color: t.threshold, lineWidth: 1, lineStyle: LineStyle.Dashed,
              axisLabelVisible: false, title: '',
            })
          }
        }
        // 统一双值标签（右缘 y→指标值浮标 + 顶部竖线列标签；竖线列显示该 pane 全部线值，
        // 各段用系列色，如 KDJ 的「K: 12.3 D: 12.1 J: 12.5」；y 浮标用首条 series 的坐标轴转换）
        if (entries[0]) {
          const xSpecs: XSpec[] = entries.map((e) => {
            const it = cfg.items.find((c) => c.field === e.field)
            return {
              series: e.s,
              label: it?.label,
              color: it ? resolveChartColor(it.color, dark) : undefined,
              fmt: (v) => v.toFixed(2),
              valueOf: valueOfValue,
            }
          })
          const prim = new PaneLabelPrimitive(crosshairState, idx, entries[0].s, (v) => v.toFixed(2), xSpecs)
          pane.attachPrimitive(prim)
          subPrims.push(prim)
        }
        subPanes.push({ key: subKey, entries })
      }
    },

    resetLayout() {
      const panes = chart.panes()
      // 主图 pane（0）：恢复创建时的拉伸因子（MAIN_STRETCH=2.4）——用户拖过分隔线后一键还原
      panes[PANE_MAIN]?.setStretchFactor(MAIN_STRETCH)
      panes[PANE_VOL]?.setStretchFactor(VOL_STRETCH)
      for (let i = PANE_SUB_BASE; i < panes.length; i++) {
        panes[i].setStretchFactor(SUB_STRETCH)
      }
      paneTopsCache.dirty = true // P2-70：高度调整 → crosshair 布局缓存失效
    },

    setDrawingTool(tool, code) {
      drawing.setTool(tool, code)
    },

    clearDrawings() {
      drawing.clearAll()
    },

    deleteDrawingLine(id) {
      drawing.deleteLine(id)
    },

    setDrawingLineStyle(id, style) {
      drawing.setLineStyle(id, style)
    },

    takeScreenshot() {
      return chart.takeScreenshot()
    },

    setSignalMarkers(markers) {
      signalMarkersRaw = markers
      applyMarkers()
    },

    locateToTime(t) {
      const time = klineTime(t, periodRef.current)
      const ts = chart.timeScale()
      const x = ts.timeToCoordinate(time)
      if (x == null) return false // 数据未加载/时点在数据范围外
      const logical = ts.coordinateToLogical(x)
      if (logical == null) return false
      const idx = Math.round(logical)
      // 滚动：目标 bar 已在可见范围内则不滚动，否则以其为中心（范围按当前跨度）
      const range = ts.getVisibleLogicalRange()
      if (!range || idx < range.from || idx > range.to) {
        const span = range ? range.to - range.from : INIT_BARS[periodRef.current] ?? 120
        ts.setVisibleLogicalRange({ from: Math.max(0, idx - span / 2), to: idx + span / 2 })
      }
      // 程序化十字线（v5 setCrosshairPosition：price + time + series；触发 crosshair 订阅 → 标签联动）
      const bar = candle.dataByIndex(Math.round(logical), -1)
      if (bar && 'close' in bar && typeof bar.close === 'number') {
        chart.setCrosshairPosition(bar.close, time, candle)
      }
      return true
    },

    dispose() {
      layoutObserver.disconnect() // P2-70：解除 crosshair 布局缓存失效监听
      chart.unsubscribeCrosshairMove(onCrosshair)
      container.removeEventListener('wheel', onWheel)
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onVisibleRange)
      drawing.dispose() // 解除画线订阅/primitive（chart.remove 前）
      chart.remove() // 一次性释放全部 DOM/canvas/订阅
      overlaySeries.length = 0
      subPanes.length = 0
      fixedPrims.length = 0
      subPrims.length = 0
      overlayStructKey = ''
      subStructKey = ''
      lastKline = null
      overlayLast = null
      subLast = null
    },
  }
  return engine
}
