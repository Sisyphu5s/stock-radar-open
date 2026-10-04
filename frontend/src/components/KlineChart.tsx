import { Button, LoadingOverlay, Modal, Radio } from '@mantine/core'
import { useMediaQuery } from '@mantine/hooks'
import { IconEye, IconEyeOff, IconX } from '@tabler/icons-react'
import { forwardRef, memo, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import type { IChartApi } from 'lightweight-charts'
// lightweight-charts 数据层契约（时间序列类型统一从 klineSeries re-export，不直接依赖包路径）
import {
  klineAxisLabel, klineSeriesName, fmtVol, estAmount,
  type KlineData,
} from '../utils/klineSeries'
import { SUB_KEYS, SUB_ANNOT, subTitle, type EffectiveParams } from '../utils/klineSubConfig'
import { usePersistentState } from '../utils/stateMemory'
import { useThemeStore } from '../stores/useAppStore'
import { isMinutePeriod } from '../utils/periods'
import { cnTodayOf, isCnTradingSession } from '../utils/time'
import { chartColors, resolveChartColor } from '../utils/chartTheme'
import { createKlineEngine, MAX_SUB_PANES, KEY_ZOOM_FACTOR, MAIN_LINE_KEYS, BOLL_KEYS, type KlineEngine, type SignalMarkerInput } from './kline/klineEngine'
import type { DrawingTool, DrawingCursor, DrawingStyle, LineColorKey } from './kline/klineDrawing'
import EmptyState from './ui/EmptyState'

// —— 契约下沉 re-export（调用方零 API 变化：data/kline.ts、useWorkbenchData.ts、context.ts 已改从 utils 导入，
//    此处保留组件路径 re-export 兜底）——
export type { KlineData } from '../utils/klineSeries'
export { PANE_SUB_BASE, MAX_SUB_PANES } from './kline/klineEngine'

/** 引擎句柄（ref 透出，供 T-14/T-15 截图/布局能力使用；本次只透出不消费） */
export interface KlineChartHandle {
  /** 图表实例（低层 API，谨慎使用） */
  getChart(): IChartApi | null
  /** 截图：走 chart.takeScreenshot → toDataURL('image/png')；失败/未就绪返回 null */
  takeScreenshot(): Promise<string | null>
  /** 恢复全部 pane 默认高度（工具栏「重置布局」同语义） */
  resetLayout(): void
  /** T-14 清除当前股票全部画线（工具栏「清除全部」经 ref 直达引擎） */
  clearDrawings(): void
  /** T-117 程序化十字线定位：naive 时点串 → 滚动至可见 + 十字线钉在该 bar（时间线事件「定位」入口）；成功 true */
  locateToTime(time: string): boolean
}

interface Props {
  data: KlineData | null
  loading?: boolean
  error?: string | null
  overlays: string[]
  period?: string // 1/5/15/30/60/daily/weekly/monthly
  /** 实际生效的具名指标参数（manual > backend global > default 合并后的实际生效参数），用于副图标题/图例展示（Step 2 使用） */
  effectiveParams?: EffectiveParams
  onRetry?: () => void
  /** 指标数据加载失败提示（顶部轻量警告条；非空时显示，可重试） */
  indicatorError?: string | null
  /** 重试加载指标（清错误提示 + 重新拉取） */
  onRetryIndicators?: () => void
  /** 图表高度（px，受控）：不传时图表自适应父容器实际高度（由父级 Splitter / 布局决定） */
  height?: number
  /** 副图图例条「删除」按钮回调（从 overlays 移除该指标；不传则不显示删除按钮） */
  onToggleOverlay?: (k: string) => void
  /** 布局重置信号：值变化时恢复全部 pane 默认高度（工具栏「重置布局」按钮驱动） */
  layoutResetSignal?: number
  /** T-14 画线工具激活态（'none'=关闭；其他=对应工具激活，点击落点绘制；不传默认关闭） */
  drawingTool?: DrawingTool
  /** T-14 画线数量变化回调（新增/删除/清空/切换股票加载后触发） */
  onDrawingChange?: (count: number) => void
  /** T-14 股票代码：画线持久化键 sr-kline-tools:{code}（不传则不持久化） */
  code?: string
  /** T-117 周期快捷键回调（图表容器聚焦时按 1/5/15/30/60/D/W/M；不传不响应） */
  onPeriodShortcut?: (p: string) => void
  /**
   * T-117 信号标记（设计落地：机制可用；工作台暂不接线 → 恒不传，不显示）。
   * 传 null/undefined 不触发任何渲染；传入数组即渲染（引擎 setSignalMarkers）。
   */
  signalMarkers?: SignalMarkerInput[]
}

/** 顶部 OHLC 信息条内容（crosshair 悬停驱动；离开图表保留最后值） */
interface HoverInfo {
  date: string
  o: number
  h: number
  l: number
  c: number
  pct: number
  vol: number
  amount: number
}

// —— 顶部信息条（OHLC/量/额段，memo 隔离：仅 info 变化时重渲染，悬停高频路径不拖累图表主体）——
const InfoBar = memo(function InfoBar({ info, period, isDark }: {
  info: HoverInfo | null
  period: string
  isDark: boolean
}) {
  const t = chartColors(isDark)
  if (!info) return null
  const upFlag = info.c >= info.o
  return (
    <>
      <span style={{ color: t.axis }}>{klineAxisLabel(info.date, period)}</span>
      <span>开 <b style={{ color: upFlag ? t.up : t.down }}>{info.o.toFixed(2)}</b></span>
      <span>高 <b style={{ color: t.up }}>{info.h.toFixed(2)}</b></span>
      <span>低 <b style={{ color: t.down }}>{info.l.toFixed(2)}</b></span>
      <span>收 <b style={{ color: upFlag ? t.up : t.down }}>{info.c.toFixed(2)}</b></span>
      <span style={{ color: info.pct >= 0 ? t.up : t.down, fontWeight: 600 }}>
        {info.pct >= 0 ? '▲ ' : '▼ '}{Math.abs(info.pct).toFixed(2)}%
      </span>
      <span style={{ color: t.axis }}>量 {fmtVol(info.vol)}</span>
      {/* 成交额为估算值（均价 × 量 × 100，一手100股）：显式标注，避免与真实额混淆 */}
      <span style={{ color: t.axis }} title="估算成交额 = 均价 × 成交量 × 100（一手100股）">额≈{fmtVol(info.amount)}</span>
    </>
  )
})

interface LegendBarProps {
  mainOverlays: string[]
  /** 全部候选副图图例（含未激活的——点击换入当前 pane 槽位；顺序 = overlays 激活顺序 + 折叠项尾部） */
  legendSubs: string[]
  /** 当前渲染在 pane 中的副图 key（最多 MAX_SUB_PANES 个，pane 与图例高亮共用同一份） */
  activeSubs: string[]
  data: KlineData | null
  /** crosshair 悬停 bar 下标（图例显示悬停 bar 的指标值；null=无悬停） */
  hoverIdx: number | null
  isDark: boolean
  collapsedSubs: string[]
  effectiveParams?: EffectiveParams
  onToggleCollapse: (k: string) => void
  onToggleOverlay?: (k: string) => void
  /** 换入选中的副图：追加到当前 pane 槽位末尾，超出 MAX_SUB_PANES 时移出最早激活的（最近使用优先） */
  onSelectSub: (k: string) => void
  /** 打开「替换已满槽位」交互（图例 +N 灰项点击） */
  onOpenReplace: () => void
}

// —— 图例条（主图叠加值 + 副图图例/折叠删除，memo 隔离：内部消费 hoverIdx/data，
//    悬停移动只重渲染本组件，KlineChart 主体/InfoBar 不再联动重渲染）——
// 副图页签化（T-89）：图例条渲染**全部候选副图**（不再按页窗口只显示当前页）；
// 超过同屏上限 MAX_SUB_PANES 时图例容器横向滚动（overflowX），尾部「+N」提示未激活数量，
// 点击未激活图例项即时换入 pane（替换最早激活的）；「+N」可点击打开「替换已满槽位」交互。
// 换入/折叠/删除三动作分离为独立可点击区（图标间距 ≥24px、移动端 ≥28px，hover tooltip 说明）。
const LegendBar = memo(function LegendBar({
  mainOverlays, legendSubs, activeSubs, data, hoverIdx, isDark,
  collapsedSubs, effectiveParams, onToggleCollapse, onToggleOverlay, onSelectSub, onOpenReplace,
}: LegendBarProps) {
  const t = chartColors(isDark)
  // 动作图标间距：桌面 ≥24px、移动端（xs 断点 576px 以下）≥28px（触屏误触防护）
  const xs = useMediaQuery('(max-width: 576px)')
  const actionGap = xs ? 28 : 24
  // 指标值：crosshair 悬停 bar 优先，否则最新一根（数据轮询/切换后 index 越界回退最新）
  const valueAt = (field: string, n: number): string => {
    const arr = data?.indicators?.[field]
    if (!arr || arr.length === 0) return '—'
    const idx = hoverIdx != null && hoverIdx < n ? hoverIdx : n - 1
    const v = arr[idx]
    if (v == null || Number.isNaN(Number(v))) return '—'
    // OBV 为累计量（值可达千万级），2 位小数会溢出图例——走万/亿缩写（与成交量 fmtVol 同语义）
    if (field === 'obv') return fmtVol(v)
    return Number(v).toFixed(2)
  }
  // 未激活（候选但不在当前 pane 槽位）的副图：图例置灰 + 点击换入；「+N」badge 提示数量
  const inactiveSubs = legendSubs.filter((k) => !activeSubs.includes(k) && !collapsedSubs.includes(k))
  return (
    <>
      {mainOverlays.length > 0 && data && data.dates.length > 0 && (
        <span style={{ display: 'inline-flex', gap: 12, flexWrap: 'wrap', alignItems: 'center' }}>
          {MAIN_LINE_KEYS.filter((k) => mainOverlays.includes(k)).map((k) => (
            <span key={k} style={{ color: t.maColors[k] ?? 'var(--sr-text-2)', whiteSpace: 'nowrap' }}>
              {klineSeriesName(k)} <b>{valueAt(k, data.dates.length)}</b>
            </span>
          ))}
          {mainOverlays.includes('boll') && BOLL_KEYS.map(([k, label]) => (
            <span key={k} style={{ color: t.boll, whiteSpace: 'nowrap' }}>
              {label} <b>{valueAt(k, data.dates.length)}</b>
            </span>
          ))}
          {mainOverlays.includes('sar') && (
            <span style={{ color: t.up, whiteSpace: 'nowrap' }}>
              SAR <b>{valueAt('sar', data.dates.length)}</b>
            </span>
          )}
        </span>
      )}
      {legendSubs.length > 0 && (
        <div
          style={{ display: 'flex', gap: 14, alignItems: 'center',
            // T-80:窄屏(≤576)图例条交父容器整体横向滚动,不撑爆信息条高度(移动端 K 线被挤到 2px 根因之一)
            overflowX: xs ? 'visible' : 'auto', maxWidth: xs ? undefined : '100%',
            whiteSpace: 'nowrap', scrollbarWidth: 'thin', flex: xs ? '0 1 auto' : '1 1 auto', minWidth: 0 }}
        >
          {legendSubs.filter((k) => !xs || activeSubs.includes(k) || collapsedSubs.includes(k)).map((key) => {
            const cfg = SUB_ANNOT[key]
            const collapsed = collapsedSubs.includes(key)
            const active = activeSubs.includes(key) // 当前渲染在 pane 中
            const n = data ? data.dates.length : 0
            return (
              <span key={key} style={{ display: 'inline-flex', alignItems: 'center', whiteSpace: 'nowrap' }}>
                {/* 换入动作区：标题 + 指标值（未激活/折叠时整块可点；P1-a11y-6：未激活不用 opacity 降级
                    （亮色 0.4 仅 2.64:1 不达 AA）——标题文字降级 var(--sr-text-3) + 虚线描边区分「可点击换入」） */}
                <span
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap',
                    cursor: active || collapsed ? 'default' : 'pointer',
                    outline: active || collapsed ? undefined : '1px dashed var(--sr-border)',
                    outlineOffset: 2 }}
                  role={active || collapsed ? undefined : 'button'} tabIndex={active || collapsed ? undefined : 0}
                  title={active ? undefined : (collapsed ? '点击展开恢复' : '点击换入副图（替换最早激活的）')}
                  onClick={() => { if (!active && !collapsed) onSelectSub(key) }}
                  onKeyDown={(e) => { if ((e.key === 'Enter' || e.key === ' ') && !active && !collapsed) { e.preventDefault(); onSelectSub(key) } }}
                >
                  <b style={{ color: active ? 'var(--sr-text-1)' : 'var(--sr-text-3)', fontWeight: 600 }}>{subTitle(key, effectiveParams)}</b>
                  {active && cfg?.items.map((it) => (
                    <span key={it.field} style={{ color: resolveChartColor(it.color, isDark) }}>
                      {it.label} <b>{valueAt(it.field, n)}</b>
                    </span>
                  ))}
                </span>
                {/* 折叠动作区（眼睛）：独立可点区，间距 ≥ actionGap */}
                <span
                  role="button" tabIndex={0}
                  aria-label={`${collapsed ? '展开' : '折叠'}${key}副图`}
                  title={collapsed ? '展开（恢复该副图到图表）' : '折叠（隐藏该副图，图例保留）'}
                  style={{ marginLeft: actionGap, cursor: 'pointer', color: 'var(--sr-text-3)', display: 'inline-flex' }}
                  onClick={() => onToggleCollapse(key)}
                  onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggleCollapse(key) } }}
                  >{collapsed ? <IconEyeOff size={14} /> : <IconEye size={14} />}</span>
                {/* 删除动作区（×）：独立可点区，间距 ≥ actionGap */}
                {onToggleOverlay && (
                  <span
                    role="button" tabIndex={0}
                    aria-label={`删除${key}副图`} title="删除（从图表移除该副图）"
                    style={{ marginLeft: actionGap, cursor: 'pointer', color: 'var(--sr-text-3)', display: 'inline-flex' }}
                    onClick={() => onToggleOverlay(key)}
                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggleOverlay(key) } }}
                  ><IconX size={14} /></span>
                )}
              </span>
            )
          })}
          {/* +N 灰项：可点击打开「替换已满槽位」交互（候选换入 + 槽位换出选择） */}
          {inactiveSubs.length > 0 && (
            <span
              role="button" tabIndex={0}
              aria-label={`另有 ${inactiveSubs.length} 个副图未激活，点击替换已满槽位`}
              title={`另有 ${inactiveSubs.length} 个副图未激活：${inactiveSubs.map((k) => SUB_ANNOT[k]?.title ?? k).join('、')}。点击选择替换已满的槽位`}
              style={{ color: 'var(--sr-text-3)', fontSize: 11, whiteSpace: 'nowrap', flex: 'none', cursor: 'pointer' }}
              onClick={onOpenReplace}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpenReplace() } }}
            >
              +{inactiveSubs.length}
            </span>
          )}
        </div>
      )}
    </>
  )
})

const KlineChart = forwardRef<KlineChartHandle, Props>(function KlineChart({
  data, loading, error, overlays, period = 'daily', onRetry, height, onToggleOverlay,
  effectiveParams, layoutResetSignal, indicatorError, onRetryIndicators,
  drawingTool, onDrawingChange, code, onPeriodShortcut, signalMarkers,
}: Props, ref) {
  const theme = useThemeStore((st) => st.theme)
  const isDark = theme === 'dark'
  // T-80:窄屏(≤576)信息条整体一行横向滚动,不再多行折行压垮图表容器(移动端 K 线 2px 根因)
  const xsInfo = useMediaQuery('(max-width: 576px)')
  const t = chartColors(isDark)
  const UP = t.up
  const DOWN = t.down

  // —— 实例引用（创建 effect 一次性建立，后续 effect 复用） ——
  const containerRef = useRef<HTMLDivElement | null>(null)
  const engineRef = useRef<KlineEngine | null>(null)
  // crosshair 回调闭包读取最新 data（ref 同步，避免订阅过期）
  const dataRef = useRef<KlineData | null>(null)
  dataRef.current = data
  // 画线数量回调闭包（ref 同步：引擎创建 effect 只跑一次，父级回调变化不重建订阅）
  const onDrawingChangeRef = useRef(onDrawingChange)
  onDrawingChangeRef.current = onDrawingChange
  // T-117 周期快捷键回调闭包（ref 同步：onChartKeyDown 依赖稳定，父级回调变化不重建）
  const onPeriodShortcutRef = useRef(onPeriodShortcut)
  onPeriodShortcutRef.current = onPeriodShortcut

  // 顶部 OHLC 信息条（crosshair 悬停驱动；离开图表保留最后值）
  const [info, setInfo] = useState<HoverInfo | null>(null)
  // 信息条内容 key：轮询产生新 data 引用但值未变时不重复 setInfo
  const infoKeyRef = useRef<string | null>(null)
  // crosshair 悬停 bar 下标（图例联动：悬停时显示该 bar 的指标值；离开保留最后值，与信息条语义一致）
  const [hoverIdx, setHoverIdx] = useState<number | null>(null)
  // hoverIdx 防抖：同 bar 内像素级移动不重复 setState（图例/信息条避免无谓重渲染）
  const hoverIdxRef = useRef(-1)
  // —— 悬停高频路径 rAF 节流：crosshair 移动只记录 pending bar 下标，同帧内多次移动合并为一次
  //    setInfo/setHoverIdx（轻量级图表每秒数十次 crosshair 回调，setState 每帧一次即可）——
  const hoverPendingRef = useRef<number | null>(null)
  const hoverRafRef = useRef<number | null>(null)
  const flushHover = useCallback(() => {
    hoverRafRef.current = null
    const i = hoverPendingRef.current
    hoverPendingRef.current = null
    if (i == null || i < 0) return // 离开图表（-1）保留最后值（既有语义），防御分支
    const d = dataRef.current
    if (!d || i >= d.dates.length) return // 轮询后下标越界：丢弃（保留旧信息条）
    const o = d.open[i] ?? 0, h = d.high[i] ?? 0, l = d.low[i] ?? 0, c = d.close[i] ?? 0, v = d.volume[i] ?? 0
    const prev = i > 0 ? (d.close[i - 1] ?? 0) : c
    const pct = prev > 0 ? ((c / prev - 1) * 100) : 0
    const amount = estAmount(o, h, l, c, v)
    const infoKey = `${d.dates[i]}|${o}|${h}|${l}|${c}|${pct}|${v}|${amount}`
    if (infoKeyRef.current !== infoKey) {
      infoKeyRef.current = infoKey
      setInfo({ date: d.dates[i], o, h, l, c, pct, vol: v, amount })
    }
    // hoverIdx 防抖：同 bar 内移动不重复 setState
    if (hoverIdxRef.current !== i) {
      hoverIdxRef.current = i
      setHoverIdx(i)
    }
  }, [])
  // 折叠的副图 key（折叠=不建 pane；展开恢复；删除走 onToggleOverlay）——sessionStorage 会话级持久（刷新保留）
  const [collapsedSubs, setCollapsedSubs] = usePersistentState<string[]>('kline:collapsedSubs', [])
  // 副图页签化（T-89）：当前 pane 槽位（有序，≤MAX_SUB_PANES），取代旧翻页窗口——
  // 图例条展示全部候选，点击未激活项「换入」追加到末尾并移出最早激活的（最近使用优先）
  const [activeSubKeys, setActiveSubKeys] = useState<string[] | null>(null)
  // 槽位同步 effect 读取最新槽位（渲染期同步，避免 updater 嵌套 setState）
  const activeSubKeysRef = useRef<string[] | null>(null)
  activeSubKeysRef.current = activeSubKeys
  // 「替换已满槽位」交互（T-25）：MAX_SUB_PANES 已满时勾选第 5 个副图 / 点击图例 +N 灰项打开；
  // replacePending=待换入副图 key（null = 用户需先从候选列表选择），replacePick=选中的换出槽位
  const [replaceOpen, setReplaceOpen] = useState(false)
  const [replacePending, setReplacePending] = useState<string | null>(null)
  const [replacePick, setReplacePick] = useState<string | null>(null)

  // 全部候选副图（渲染 pane 的池子）：折叠排除；折叠项保留图例入口（legends 尾部追加）
  const subCandidates = useMemo(
    () => overlays.filter((k) => SUB_KEYS.includes(k) && !collapsedSubs.includes(k)),
    [overlays, collapsedSubs],
  )
  // 槽位同步：候选变化（overlays/折叠）时保留已在槽位中的、补充新增的、裁剪超限的；
  // 用户「换入」走 onSelectSub 直接改槽位，不经过此 effect。
  // 超限新语义（T-25）：槽位已满且有新增副图时不再静默裁剪——弹「替换已满槽位」交互，
  // 用户选换出哪个槽位；取消则新增副图停留在「未激活」（图例灰项，可稍后经 +N/点击换入）。
  useEffect(() => {
    const prev = activeSubKeysRef.current
    if (!prev) {
      setActiveSubKeys(subCandidates.slice(0, MAX_SUB_PANES))
      return
    }
    const keep = prev.filter((k) => subCandidates.includes(k))
    const add = subCandidates.filter((k) => !keep.includes(k))
    if (keep.length >= MAX_SUB_PANES && add.length > 0) {
      setReplacePending(add[add.length - 1])
      setReplacePick(null)
      setReplaceOpen(true)
      return
    }
    setActiveSubKeys([...keep, ...add].slice(0, MAX_SUB_PANES))
  }, [subCandidates])
  // 当前渲染在 pane 中的副图（初始 null 时为前 MAX_SUB_PANES 个；effect 同步后即有值）
  const activeSubs = activeSubKeys ?? subCandidates.slice(0, MAX_SUB_PANES)
  // 图例条展示列表：全部候选（含未激活的——置灰可点击换入）+ 折叠中的（保留入口，可展开恢复）
  const legendSubs = useMemo(() => {
    const collapsed = collapsedSubs.filter((k) => SUB_KEYS.includes(k) && overlays.includes(k))
    return [...subCandidates, ...collapsed]
  }, [subCandidates, collapsedSubs, overlays])
  // 换入副图：追加槽位末尾；超限移出最早激活的（保持最近使用优先）；已在槽位/折叠项忽略
  const selectSub = useCallback((key: string) => {
    setActiveSubKeys((prev) => {
      const cur = prev ?? subCandidatesRef.current
      if (cur.includes(key)) return cur
      const next = [...cur, key]
      return next.length > MAX_SUB_PANES ? next.slice(next.length - MAX_SUB_PANES) : next
    })
  }, [])
  // selectSub 读取最新 subCandidates（callback 依赖稳定，避免每次候选变化重建）
  const subCandidatesRef = useRef<string[]>([])
  subCandidatesRef.current = subCandidates
  // 未激活副图（替换交互候选列表；与 LegendBar 内部口径一致）
  const inactiveSubs = useMemo(
    () => legendSubs.filter((k) => !activeSubs.includes(k) && !collapsedSubs.includes(k)),
    [legendSubs, activeSubs, collapsedSubs],
  )
  // 替换交互：确认（换出 replacePick、换入 replacePending）
  const confirmReplace = useCallback(() => {
    if (!replacePending || !replacePick) return
    setActiveSubKeys((prev) => {
      const cur = prev ?? subCandidatesRef.current
      const next = cur.filter((k) => k !== replacePick)
      return next.includes(replacePending) ? next : [...next, replacePending]
    })
    setReplaceOpen(false)
    setReplacePending(null)
    setReplacePick(null)
  }, [replacePending, replacePick])
  // 替换交互：取消/关闭
  const closeReplace = useCallback(() => {
    setReplaceOpen(false)
    setReplacePending(null)
    setReplacePick(null)
  }, [])
  // 打开替换交互（图例 +N 灰项入口：换入副图由用户从候选列表选择）
  const openReplace = useCallback(() => {
    setReplacePending(null)
    setReplacePick(null)
    setReplaceOpen(true)
  }, [])

  // —— T-68 画线 UI 状态（引擎回调驱动）：hover 光标 / 选中线样式面板 / 右键菜单 ——
  const [hoverCursor, setHoverCursor] = useState<DrawingCursor>('default')
  const [selDraw, setSelDraw] = useState<{ id: string; style: DrawingStyle } | null>(null)
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; lineId: string } | null>(null)
  // 样式面板色档：key 与引擎持久化一致（chartColors 双主题解析，与引擎渲染同源）
  const drawStyleColors: { key: LineColorKey; label: string; value: string }[] = [
    { key: 'yellow', label: '黄色', value: t.lineColors[0] },
    { key: 'red', label: '红色', value: t.up },
    { key: 'blue', label: '蓝色', value: t.lineColors[1] },
    { key: 'white', label: '中性', value: t.label.text },
  ]
  // 样式应用：本地受控立即反馈 + 引擎持久化（引擎回调会再同步一次，值同不触发重渲染）
  const applyDrawStyle = useCallback((style: DrawingStyle) => {
    setSelDraw((prev) => (prev ? { ...prev, style } : prev))
    if (selDraw) engineRef.current?.setDrawingLineStyle(selDraw.id, style)
  }, [selDraw])
  // 右键菜单关闭：点击菜单外任意处（捕获阶段，排除菜单自身）
  useEffect(() => {
    if (!ctxMenu) return
    const onAnyDown = (e: MouseEvent) => {
      const el = e.target as HTMLElement | null
      if (el && el.closest('.sr-draw-ctx-menu')) return
      setCtxMenu(null)
    }
    window.addEventListener('mousedown', onAnyDown, true)
    return () => window.removeEventListener('mousedown', onAnyDown, true)
  }, [ctxMenu])

  // —— 引擎创建（一次）：图表实例/常驻 series/primitive/订阅/wheel 全部下沉 klineEngine ——
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const engine = createKlineEngine(el, isDark, {
      // crosshair 移动 → rAF 节流 setInfo/setHoverIdx（离开图表不回调，保留最后值语义）
      onCrosshairMove: (logical) => {
        if (logical == null || logical < 0) return
        const d = dataRef.current
        if (!d || logical >= d.dates.length) return // 轮询后下标越界：丢弃（保留旧信息条）
        hoverPendingRef.current = logical
        if (hoverRafRef.current == null) {
          hoverRafRef.current = requestAnimationFrame(flushHover)
        }
      },
      onDrawingChange: (count) => onDrawingChangeRef.current?.(count),
      onCursorChange: (c) => setHoverCursor(c),
      onContextMenu: (info) => setCtxMenu(info),
      onSelectionChange: (sel) => setSelDraw(sel),
    }, period)
    engineRef.current = engine
    return () => {
      engine.dispose()
      engineRef.current = null
      if (hoverRafRef.current != null) cancelAnimationFrame(hoverRafRef.current)
      hoverRafRef.current = null
      hoverPendingRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // —— ref 透出（T-14/T-15 截图/布局能力；本次只透出不消费） ——
  useImperativeHandle(ref, () => ({
    getChart: () => engineRef.current?.getChart() ?? null,
    takeScreenshot: async () => {
      const canvas = engineRef.current?.takeScreenshot() ?? null
      if (!canvas) return null
      try {
        return canvas.toDataURL('image/png')
      } catch {
        return null
      }
    },
    resetLayout: () => engineRef.current?.resetLayout(),
    clearDrawings: () => engineRef.current?.clearDrawings(),
    locateToTime: (t) => engineRef.current?.locateToTime(t) ?? false,
  }), [])

  // —— 数据同步（K 线 + 成交量 + 视野；信息条初始在壳层自算） ——
  useEffect(() => {
    const engine = engineRef.current
    if (!engine) return
    engine.setData(data, period, UP, DOWN)
    // 空态：重置信息条，避免切换股票后残留旧 OHLC
    if (!data || data.dates.length === 0) {
      setInfo(null)
      infoKeyRef.current = null
      return
    }
    // 信息条初始：最新一根（内容 key 未变则不重复 setInfo，轮询不触发多余渲染）
    const n = data.dates.length
    const last = n - 1
    const lo = data.open[last] ?? 0, lh = data.high[last] ?? 0, ll = data.low[last] ?? 0, lc = data.close[last] ?? 0, lv = data.volume[last] ?? 0
    const pc = last > 0 ? (data.close[last - 1] ?? 0) : 0
    const pct = last > 0 && pc > 0 ? (lc / pc - 1) * 100 : 0
    const amount = estAmount(lo, lh, ll, lc, lv)
    const infoKey = `${data.dates[last]}|${lo}|${lh}|${ll}|${lc}|${pct}|${lv}|${amount}`
    if (infoKeyRef.current !== infoKey) {
      infoKeyRef.current = infoKey
      setInfo({ date: data.dates[last], o: lo, h: lh, l: ll, c: lc, pct, vol: lv, amount })
    }
  }, [data, period, isDark, UP, DOWN])

  // —— 主图动态叠加（均线/BOLL/SAR，pane 0）→ 引擎（结构键含 indicators 字段指纹） ——
  useEffect(() => {
    engineRef.current?.setOverlays(data, overlays, period, isDark)
  }, [data, overlays, period, isDark, UP, DOWN])

  // —— 副图 pane 系统 → 引擎（activeSubs 结构变化全量重建，removePane 防索引漂移） ——
  useEffect(() => {
    engineRef.current?.setSubPanes(data, activeSubs, period, isDark)
  }, [data, activeSubs, period, isDark, UP, DOWN])

  // —— 主题切换（applyOptions 增量更新；叠加/副图 series 由对应 effect 随 isDark 重建） ——
  useEffect(() => {
    engineRef.current?.applyTheme(isDark)
  }, [isDark, UP, DOWN])

  // —— 周期变化：时间刻度格式 + 分钟级 timeVisible ——
  useEffect(() => {
    engineRef.current?.applyPeriod(period)
  }, [period])

  // —— T-14 画线：工具激活态 + 股票代码（代码变化时引擎重载该股画线，键 sr-kline-tools:{code}） ——
  useEffect(() => {
    engineRef.current?.setDrawingTool(drawingTool ?? 'none', code ?? '')
  }, [drawingTool, code])

  // —— T-117 信号标记（设计落地：prop 由工作台接线后才显示；恒不传 → 引擎保持空，零渲染） ——
  useEffect(() => {
    if (signalMarkers) engineRef.current?.setSignalMarkers(signalMarkers)
  }, [signalMarkers])

  // —— 布局重置（工具栏「重置布局」按钮驱动）：恢复全部 pane 默认高度（含主图拉伸因子） ——
  useEffect(() => {
    if (layoutResetSignal === undefined) return
    engineRef.current?.resetLayout()
  }, [layoutResetSignal])

  // 激活的主图叠加 key（与叠加 effect 同口径：overlays 中非副图 key）
  // 注意：以下 hook 必须位于一切条件 JSX 之前（error 空态改条件 JSX 而非提前 return），
  // 保证首载失败进错误空态时 hook 调用数不变，遵守 rules-of-hooks 防整页崩溃。
  const mainOverlays = useMemo(() => overlays.filter((k) => !SUB_KEYS.includes(k)), [overlays])
  // LegendBar 回调全部 useCallback 稳定（memo 浅比较依赖）：
  // setCollapsedSubs 为 usePersistentState 包装 setter（内部仅依赖稳定 setValue），首实例可安全复用
  const toggleCollapse = useCallback((key: string) => {
    setCollapsedSubs((prev) => (prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]))
  }, [])

  // —— T-117 周期快捷键（TradingView 风格，图表容器聚焦时生效） ——
  // 字母键直切：D/W/M；数字键缓冲：'1' 既精确命中 1 分也是 '15' 的前缀 → 短延时（600ms）等下一数字，
  // 其余（5/15/30/60）精确且无更长候选立即应用；非法组合清缓冲。输入框聚焦时 keydown 不会到达本容器。
  const PERIOD_KEYS: Record<string, string> = { '1': '1', '5': '5', '15': '15', '30': '30', '60': '60' }
  const periodBufRef = useRef('')
  const periodTimerRef = useRef<number | null>(null)
  useEffect(() => () => {
    if (periodTimerRef.current != null) window.clearTimeout(periodTimerRef.current)
  }, [])

  // 键盘可达（P1-a11y-4）：图表容器 tabIndex=0 可聚焦，←/→ 平移（可见范围 10% 步进）、
  // +/−（或 PageUp/PageDown）缩放（锚定视野中心，灵敏度与 wheel 缩放同源 KEY_ZOOM_FACTOR，下限 5 根防过缩）
  const onChartKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const key = e.key
    // 周期快捷键：字母直切（大小写兼容；Ctrl/⌘/Alt 组合不劫持）
    if ((key === 'd' || key === 'D') && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault()
      onPeriodShortcutRef.current?.('daily')
      return
    }
    if ((key === 'w' || key === 'W') && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault()
      onPeriodShortcutRef.current?.('weekly')
      return
    }
    if ((key === 'm' || key === 'M') && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault()
      onPeriodShortcutRef.current?.('monthly')
      return
    }
    if (/^[0-9]$/.test(key)) {
      e.preventDefault()
      const buf = periodBufRef.current + key
      periodBufRef.current = buf
      if (periodTimerRef.current != null) { window.clearTimeout(periodTimerRef.current); periodTimerRef.current = null }
      const exact = PERIOD_KEYS[buf]
      const isPrefix = Object.keys(PERIOD_KEYS).some((k) => k !== buf && k.startsWith(buf))
      const apply = () => {
        periodBufRef.current = ''
        if (exact) onPeriodShortcutRef.current?.(exact)
      }
      if (exact && !isPrefix) {
        apply() // 精确且无更长候选（5/15/30/60）：立即应用
      } else if (exact || isPrefix) {
        // '1'（精确+前缀）/ '3' / '6'（仅前缀）：短延时等下一数字，超时应用最短匹配
        periodTimerRef.current = window.setTimeout(() => {
          periodTimerRef.current = null
          apply()
        }, 600)
      } else {
        periodBufRef.current = '' // 非法组合（如 '12'）：清缓冲
      }
      return
    }
    const chart = engineRef.current?.getChart()
    if (!chart) return
    const ts = chart.timeScale()
    const range = ts.getVisibleLogicalRange()
    if (!range || range.to <= range.from) return
    const span = range.to - range.from
    const step = Math.max(1, Math.round(span * 0.1))
    const center = (range.from + range.to) / 2
    if (e.key === 'ArrowLeft') {
      e.preventDefault()
      ts.setVisibleLogicalRange({ from: range.from - step, to: range.to - step })
    } else if (e.key === 'ArrowRight') {
      e.preventDefault()
      ts.setVisibleLogicalRange({ from: range.from + step, to: range.to + step })
    } else if (e.key === '+' || e.key === '=' || e.key === 'PageUp') {
      e.preventDefault()
      const newSpan = Math.max(5, span * KEY_ZOOM_FACTOR)
      ts.setVisibleLogicalRange({ from: center - newSpan / 2, to: center + newSpan / 2 })
    } else if (e.key === '-' || e.key === '_' || e.key === 'PageDown') {
      e.preventDefault()
      const newSpan = span / KEY_ZOOM_FACTOR
      ts.setVisibleLogicalRange({ from: center - newSpan / 2, to: center + newSpan / 2 })
    }
  }

  // 数据错误标注（与 echarts 时代一致：error 且无数据时显示空态；条件 JSX，不提前 return）
  const errorEmpty = error && !data ? (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', ...(height ? { height } : {}) }}>
      <EmptyState text={error} onRetry={onRetry} padding={0} />
    </div>
  ) : null

  // 语义标注：图表 role="img" + aria-label；另附可视隐藏文字摘要（屏幕阅读器可读）
  const hasData = !!data && data.dates.length > 0
  const lastIdx = hasData ? data.dates.length - 1 : -1
  const lastClose = data && lastIdx >= 0 ? data.close[lastIdx] : null

  // 盘中实时合成标注（仅日/周/月——分钟线逐根实时，不涉及合成；且不为今日 bar 编造截至时刻，
  // 数据只有日期无分钟级时间戳，诚实起见仅标「实时合成」）：
  // - 最后一根 bar 为上海今日 && 当前处于上海交易时段 → 当日 bar 为后端分钟聚合/重采样合成
  // - 最后一根 bar 早于上海今日 && 盘中 → 数据未跟上（行情延迟）轻提示
  const lastDate = hasData ? data.dates[lastIdx] : null
  const shToday = cnTodayOf()
  const inTradingSession = isCnTradingSession()
  const isIntradayLive = hasData && !isMinutePeriod(period) && lastDate === shToday && inTradingSession
  const isStaleData = hasData && !isMinutePeriod(period) && !!lastDate && lastDate < shToday && inTradingSession
  const ariaLabel = data && data.dates.length > 0
    ? `${period === 'daily' ? '日' : period === 'weekly' ? '周' : period === 'monthly' ? '月' : `${period}分钟`}K线图，共 ${data.dates.length} 根，区间 ${data.dates[0]} 至 ${data.dates[lastIdx]}，最新收盘 ${lastClose != null ? Number(lastClose).toFixed(2) : '—'}${info ? `，涨跌 ${info.pct >= 0 ? '涨' : '跌'} ${Math.abs(info.pct).toFixed(2)}%` : ''}`
    : 'K线图'
  const summaryText = data && info
    ? `${data.dates[lastIdx]} 开 ${info.o.toFixed(2)} 高 ${info.h.toFixed(2)} 低 ${info.l.toFixed(2)} 收 ${info.c.toFixed(2)} 涨跌 ${info.pct >= 0 ? '+' : ''}${info.pct.toFixed(2)}% 成交量 ${fmtVol(info.vol)}`
    : ariaLabel

  return (
    <>
      {errorEmpty ?? (
        <div style={{ position: 'relative', display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, minWidth: 0, flex: 1 }}>
          {/* 加载遮罩（loading 时图表保持不闪空；覆盖层相对本容器定位） */}
          <LoadingOverlay visible={!!loading} zIndex={5} />
          {/* 顶部信息条（OHLC + 主图叠加值 + 副图图例合并一行，省垂直空间；flex wrap 窄屏自动换行）：
              OHLC 由 crosshair 悬停驱动（离开保留最后值）；叠加/图例显示 hover bar / 最新指标值。
              InfoBar/LegendBar 均为 memo 子组件：悬停只触发 LegendBar 重渲染，图表主体/InfoBar 隔离 */}
          {(info || legendSubs.length > 0 || mainOverlays.length > 0 || isIntradayLive || isStaleData) && (
            <div style={{
              display: 'flex', gap: 16, flexWrap: xsInfo ? 'nowrap' : 'wrap', alignItems: 'center', padding: '2px 4px 8px',
              fontSize: 12, color: 'var(--sr-text-2)',
              overflowX: xsInfo ? 'auto' : 'visible', scrollbarWidth: xsInfo ? 'thin' : 'auto',
            }}>
              {/* 盘中实时合成标注（日/周/月）：当日 bar 为分钟聚合/重采样合成（未收盘真实数据），
                  语义对齐同花顺等专业软件对在途 bar 的明确标注；不编造截至时刻 */}
              {isIntradayLive && (
                <span title="当日 bar 为盘中实时合成（未收盘，收盘后由真实数据覆盖）"
                  style={{ display: 'inline-flex', alignItems: 'center', gap: 5, color: 'var(--sr-accent)', fontWeight: 600, whiteSpace: 'nowrap' }}>
                  <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--sr-accent)', flex: 'none' }} />
                  实时合成
                </span>
              )}
              {/* 数据延迟轻提示：盘中但最后一根 bar 早于上海今日（合成数据未跟上） */}
              {isStaleData && (
                <span title="盘中行情数据未跟上（最后数据非今日）"
                  style={{ color: 'var(--sr-text-3)', whiteSpace: 'nowrap' }}>
                  行情延迟
                </span>
              )}
              <InfoBar info={info} period={period} isDark={isDark} />
              <LegendBar
                mainOverlays={mainOverlays}
                legendSubs={legendSubs}
                activeSubs={activeSubs}
                data={data}
                hoverIdx={hoverIdx}
                isDark={isDark}
                collapsedSubs={collapsedSubs}
                effectiveParams={effectiveParams}
                onToggleCollapse={toggleCollapse}
                onToggleOverlay={onToggleOverlay}
                onSelectSub={selectSub}
                onOpenReplace={openReplace}
              />
            </div>
          )}
          {/* 指标加载失败警告条：轻量一行（信息条与图表容器之间），可一键重试 */}
          {indicatorError && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '0 4px 4px', fontSize: 'var(--sr-font-xs)', color: 'var(--sr-warning)' }}>
              <span>{indicatorError}</span>
              {onRetryIndicators && (
                <span role="button" tabIndex={0} aria-label="重试加载指标" title="重试"
                  style={{ cursor: 'pointer', textDecoration: 'underline' }}
                  onClick={onRetryIndicators}
                  onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onRetryIndicators() } }}>
                  重试
                </span>
              )}
            </div>
          )}
          {/* 图表容器：height prop 受控时固定高度，否则 flex 占满父容器；lightweight autoSize 自动跟随 */}
          <div style={{ position: 'relative', flex: 1, minHeight: 0, minWidth: 0, ...(height ? { height, flex: 'none' } : {}) }}>
            <div
              ref={containerRef}
              // 键盘可达（P1-a11y-4）：role=application + tabIndex=0 使图表可聚焦（全局 :focus-visible 规则自动提供焦点环），
              // ←/→ 平移、+/-（或 PageUp / PageDown）缩放；aria-label 附键盘操作提示
              role="application"
              aria-label={`${ariaLabel}。按←→键平移图表，按+ -键（或 PageUp / PageDown）缩放，按 1/5/15/30/60/D/W/M 键切换周期`}
              tabIndex={0}
              onKeyDown={onChartKeyDown}
              style={{ position: 'absolute', inset: 0, minWidth: 0, cursor: hoverCursor }}
            />
            {/* T-68 选中线样式面板：选中画线后显示（颜色 4 档 / 线宽 / 实虚线），引擎 setDrawingLineStyle 持久化 */}
            {selDraw && (
              <div
                role="group" aria-label="线样式"
                style={{ position: 'absolute', top: 4, left: 8, zIndex: 6, display: 'flex', gap: 6, alignItems: 'center',
                  background: 'var(--sr-block-bg)', border: '1px solid var(--sr-border)', borderRadius: 4,
                  padding: '4px 6px', boxShadow: '0 2px 8px rgba(0,0,0,0.18)' }}
                onMouseDown={(e) => e.stopPropagation()}
              >
                {drawStyleColors.map((c) => (
                  <button
                    key={c.key} title={`线色：${c.label}`} aria-label={`线色：${c.label}`}
                    onClick={() => applyDrawStyle({ ...selDraw.style, color: c.key })}
                    style={{ width: 14, height: 14, borderRadius: 3, padding: 0, cursor: 'pointer', flex: 'none',
                      background: c.value,
                      border: selDraw.style.color === c.key ? '2px solid var(--sr-accent)' : '1px solid var(--sr-border)' }}
                  />
                ))}
                <span style={{ width: 1, height: 14, background: 'var(--sr-border)', flex: 'none' }} />
                {([1, 2, 3] as const).map((w) => (
                  <button
                    key={w} title={`线宽 ${w}px`} aria-label={`线宽 ${w}px`}
                    onClick={() => applyDrawStyle({ ...selDraw.style, width: w })}
                    style={{ width: 20, height: 18, display: 'flex', alignItems: 'center', justifyContent: 'center',
                      padding: 0, cursor: 'pointer', background: 'transparent', borderRadius: 3,
                      border: selDraw.style.width === w ? '1px solid var(--sr-accent)' : '1px solid var(--sr-border)' }}
                  >
                    <span style={{ width: 14, height: w, background: 'var(--sr-text-2)', borderRadius: 1, flex: 'none' }} />
                  </button>
                ))}
                <span style={{ width: 1, height: 14, background: 'var(--sr-border)', flex: 'none' }} />
                <button
                  title={selDraw.style.dash ? '当前虚线，点击改实线' : '当前实线，点击改虚线'}
                  aria-label={selDraw.style.dash ? '改为实线' : '改为虚线'}
                  onClick={() => applyDrawStyle({ ...selDraw.style, dash: !selDraw.style.dash })}
                  style={{ width: 34, height: 18, display: 'flex', alignItems: 'center', justifyContent: 'center',
                    padding: 0, cursor: 'pointer', background: 'transparent', borderRadius: 3,
                    border: '1px solid var(--sr-border)' }}
                >
                  <span style={{ display: 'block', width: 22, height: 0, flex: 'none',
                    borderTop: `${selDraw.style.width}px ${selDraw.style.dash ? 'dashed' : 'solid'} var(--sr-text-2)` }} />
                </button>
              </div>
            )}
            {/* 空态：无 K 线数据且非加载中/错误时覆盖提示（error && !data 已提前早退） */}
            {!hasData && !loading && (
              <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
                <EmptyState description="暂无K线数据" padding={0} />
              </div>
            )}
            {/* 文字摘要（可视隐藏，屏幕阅读器朗读；布局无重叠） */}
            <div aria-hidden="false" style={{ position: 'absolute', width: 1, height: 1, padding: 0, margin: -1, overflow: 'hidden', clip: 'rect(0 0 0 0)', whiteSpace: 'nowrap', border: 0 }}>
              {summaryText}
            </div>
          </div>
        </div>
      )}
      {/* T-68 画线右键菜单：命中已画线弹出（删除此线/清除全部；删除单线无需先选中） */}
      {ctxMenu && (
        <div
          className="sr-draw-ctx-menu" role="menu" aria-label="画线操作"
          style={{ position: 'fixed', left: ctxMenu.x, top: ctxMenu.y, zIndex: 60, minWidth: 120,
            background: 'var(--sr-card-bg)', border: '1px solid var(--sr-border)', borderRadius: 6,
            boxShadow: '0 4px 16px rgba(0,0,0,0.25)', padding: 4, fontSize: 12, color: 'var(--sr-text-1)' }}
        >
          <div role="menuitem" tabIndex={0}
            style={{ padding: '5px 10px', borderRadius: 4, cursor: 'pointer' }}
            onClick={() => { engineRef.current?.deleteDrawingLine(ctxMenu.lineId); setCtxMenu(null) }}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); engineRef.current?.deleteDrawingLine(ctxMenu.lineId); setCtxMenu(null) } }}
          >删除此线</div>
          <div role="menuitem" tabIndex={0}
            style={{ padding: '5px 10px', borderRadius: 4, cursor: 'pointer' }}
            onClick={() => { engineRef.current?.clearDrawings(); setCtxMenu(null) }}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); engineRef.current?.clearDrawings(); setCtxMenu(null) } }}
          >清除全部</div>
        </div>
      )}
      {/* 替换已满槽位交互（T-25）：勾选第 5 个副图 / 图例 +N 灰项两处入口共用。
          replacePending=null（+N 入口）时先选「换入副图」候选；槽位列表为当前渲染的 MAX_SUB_PANES 个 */}
      <Modal
        opened={replaceOpen}
        onClose={closeReplace}
        title="替换已满槽位"
      >
        {replacePending === null && inactiveSubs.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ marginBottom: 8, fontWeight: 600, fontSize: 12 }}>选择要换入的副图</div>
            <Radio.Group<string>
              value={replacePending ?? undefined}
              onChange={(v) => { setReplacePending(v); setReplacePick(null) }}
            >
              {inactiveSubs.map((k) => (
                <Radio key={k} value={k} label={subTitle(k, effectiveParams)} />
              ))}
            </Radio.Group>
          </div>
        )}
        <div style={{ marginBottom: 8, fontWeight: 600, fontSize: 12 }}>
          现有 {MAX_SUB_PANES} 个槽位{replacePending ? `（换入 ${subTitle(replacePending, effectiveParams)}）` : ''}
        </div>
        <Radio.Group<string>
          value={replacePick ?? undefined}
          onChange={(v) => setReplacePick(v)}
        >
          {activeSubs.map((k) => (
            <Radio key={k} value={k} label={subTitle(k, effectiveParams)} />
          ))}
        </Radio.Group>
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 16 }}>
          <Button variant="default" size="xs" onClick={closeReplace}>取消</Button>
          <Button variant="filled" size="xs" disabled={!(replacePending && replacePick)} onClick={confirmReplace}>替换</Button>
        </div>
      </Modal>
    </>
  )
})

// React.memo 浅比较：data/overlays/period 等引用稳定时跳过重渲染（hover 移动/子级 setState 不拖累）
export default memo(KlineChart)
