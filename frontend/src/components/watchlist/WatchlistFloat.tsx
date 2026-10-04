import { Badge, Button } from '@mantine/core'
import {
  IconCheck, IconEye,
  IconGripVertical, IconStarFilled, IconX,
} from '@tabler/icons-react'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent as RPointerEvent, RefObject, TransitionEvent as RTransitionEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import type { WatchAlertItem } from '../../hooks/useWatchlistAlerts'
import type { FloatRect } from '../../hooks/useFloatPlacement'
import type { WatchlistItem } from '../../api/client'
import IconTextButton from '../ui/IconTextButton'
import SignalTag from '../ui/SignalTag'
import SignalMomentCell from '../ui/SignalMomentCell'
import { fmtPct, toNum } from '../../utils/format'
import { readCssVar } from '../../hooks/useFloatGeometry'
import './watchlist-float.css'

/** morphing 锚点尺寸 = launcher 直径（px） */
const MORPH_SIZE = 36
/** 离开动画兜底时长（transitionend 优先，超时兜底） */
const LEAVE_FALLBACK_MS = 400

/** 数字化容错：number 原样；非空字符串转 number；其余 null（复用 utils/format toNum） */
const numOrNull = (v: number | null): number | null => (v != null && Number.isFinite(v) ? v : null)

/** 未读事件时点：统一 SignalMomentCell 双时组件（主=信号理论原始时刻，副=扫描发现时刻；
 *  旧数据 scan_discovered_at 为 null 时仅主行）。period 缺失（旧事件）兜底 daily。 */
function AlertMoment({ a }: { a: WatchAlertItem }) {
  return (
    <span className="sr-wl-float-item-time">
      <SignalMomentCell period={a.period} triggeredAt={a.triggeredAt} asOf={a.asOf} discoveredAt={a.discoveredAt} compact />
    </span>
  )
}

/** 收缩态几何：以 morphOrigin（launcher 中心）为锚点缩至 MORPH_SIZE（36px）。
 *  rect 非空用 rect 的 left/top/宽高；null 用 CSS 默认位近似（右下 vw-w-16, vh-bottombar-safe-60-h）。
 *  容器 transform-origin 为 0 0（CSS 已设），translate 把元素左上角平移到锚点，缩放后整体收敛于 launcher。 */
function shrinkTransform(origin: { x: number; y: number }, r: FloatRect | null) {
  const vw = window.innerWidth
  const vh = window.innerHeight
  let left: number
  let top: number
  let targetW: number
  let targetH: number
  if (r) {
    left = r.x
    top = r.y
    targetW = r.w
    targetH = r.h
  } else {
    targetW = 400
    targetH = Math.min(600, vh - 40)
    left = vw - targetW - readCssVar('--sr-float-edge', 16)
    top = vh - readCssVar('--sr-bottombar-h', 0) - readCssVar('--sr-safe-bottom', 0) - readCssVar('--sr-float-bottom-clearance', 60) - targetH
  }
  return {
    transform: `translate(${origin.x - left}px, ${origin.y - top}px) scale(${MORPH_SIZE / targetW}, ${MORPH_SIZE / targetH})`,
    opacity: 0,
  }
}

/** 行情段：价格（2 位）+ 涨跌幅（带符号 %，红涨绿跌、零=灰）；数字化容错，价格缺省整段不渲染 */
function QuoteSegment({ w }: { w: WatchlistItem | undefined }) {
  if (!w) return null
  const price = numOrNull(toNum(w.last_price))
  if (price == null) return null
  const pct = numOrNull(toNum(w.pct_change))
  const pctCls = pct == null ? null : pct > 0 ? 'sr-wl-float-pct-up' : pct < 0 ? 'sr-wl-float-pct-down' : 'sr-wl-float-pct-zero'
  return (
    <span className="sr-wl-float-quote">
      <span className="sr-wl-float-quote-price">{price.toFixed(2)}</span>
      {pct != null && (
        <span className={pctCls ?? undefined}>{(pct >= 0 ? '+' : '') + fmtPct(pct / 100, 2)}</span>
      )}
    </span>
  )
}

export interface WatchlistFloatProps {
  // ===== 几何（来自 useFloatPlacement，父级 WatchlistWidget 注入） =====
  enabled: boolean
  floatRef: RefObject<HTMLDivElement | null>
  rect: FloatRect | null
  dragging: boolean
  resizing: boolean
  headPointerDown: (e: RPointerEvent<HTMLDivElement>) => void
  resizePointerDown: (e: RPointerEvent<HTMLDivElement>) => void
  edgeRightPointerDown: (e: RPointerEvent<HTMLDivElement>) => void
  edgeBottomPointerDown: (e: RPointerEvent<HTMLDivElement>) => void
  // ===== 数据（来自 useWatchlistAlerts，父级注入） =====
  alerts: WatchAlertItem[]
  unreadCount: number
  watchlistData: WatchlistItem[]
  markAllSeen: () => void
  toggleWatch: (code: string, name?: string) => Promise<void>
  labelOf: (code: string) => { text: string; color: string }
  // ===== 行为 =====
  /** 关闭 = 浮窗隐藏（launcher 常驻为入口；新未读到达由父级自动弹出） */
  onClose: () => void
  // ===== morphing（launcher ↔ 浮窗 几何动画） =====
  /** morphing 锚点 = launcher 中心像素；null 时不做几何动画（移动端） */
  morphOrigin: { x: number; y: number } | null
  /** 挂载即 true：首帧渲染收缩态（scale≈36px 锚点），rAF 后过渡到正常尺寸 */
  entering: boolean
  /** true：反向动画（收缩回 launcher），动画结束后调 onLeaveEnd */
  leaving: boolean
  /** 离开动画结束回调（transitionend 优先，setTimeout 兜底） */
  onLeaveEnd: () => void
}

/**
 * 全局关注提醒浮窗（纯展示层，状态全部由父级 WatchlistWidget 注入）：
 * - 展开态：头部整行可拖（按钮组除外）+ 未读列表/空态 + 「查看关注页」入口；
 * - 移动端（<768）：底部 sheet（≤72dvh 面板），不可拖、恒展开态；
 * - 空态（无未读）：仅提示行 + 「查看关注页」按钮（关注股列表弱化移除，避免与关注页重复）；
 * - 未读时点：resolveSignalMoment 状态机（日线事件显示 MM-DD + 相对词，不再显示 15:00 伪时刻）；
 * - 关闭 = onClose（父级隐藏浮窗，launcher 常驻）；新未读到达自动弹出（父级带 30s 频率限制）；
 * - morphing：挂载进入/离开均从 launcher 锚点生长/收缩（T-70 移除 chip 折叠形态后仅剩面板形态）。
 */
export default function WatchlistFloat(props: WatchlistFloatProps) {
  const {
    enabled, floatRef, rect, dragging, resizing,
    headPointerDown,
    alerts, unreadCount, watchlistData, markAllSeen, toggleWatch, labelOf, onClose,
    morphOrigin, entering, leaving, onLeaveEnd,
  } = props
  const navigate = useNavigate()

  // ===== 2a 进入动画：morphOrigin + enabled 才做；首帧收缩态，双 rAF 后过渡到正常尺寸 =====
  const [enteringLocal, setEnteringLocal] = useState<boolean>(() =>
    entering && morphOrigin != null && enabled,
  )
  useLayoutEffect(() => {
    if (!enteringLocal) return
    let raf1 = 0
    let raf2 = 0
    raf1 = requestAnimationFrame(() => {
      raf2 = requestAnimationFrame(() => setEnteringLocal(false))
    })
    return () => {
      cancelAnimationFrame(raf1)
      cancelAnimationFrame(raf2)
    }
  }, [enteringLocal])

  // ===== 2b 离开动画：transitionend(transform) 优先，setTimeout 兜底，ref 防重复回调 =====
  const leaveFiredRef = useRef(false)
  const leaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const fireLeaveEnd = useCallback(() => {
    if (leaveFiredRef.current) return
    leaveFiredRef.current = true
    if (leaveTimerRef.current != null) {
      clearTimeout(leaveTimerRef.current)
      leaveTimerRef.current = null
    }
    onLeaveEnd()
  }, [onLeaveEnd])
  useEffect(() => {
    if (!leaving) return
    leaveFiredRef.current = false
    // 无几何动画能力（移动端/无锚点）：直接结束，不空等兜底
    if (morphOrigin == null || !enabled) {
      fireLeaveEnd()
      return
    }
    leaveTimerRef.current = setTimeout(fireLeaveEnd, LEAVE_FALLBACK_MS)
    return () => {
      if (leaveTimerRef.current != null) {
        clearTimeout(leaveTimerRef.current)
        leaveTimerRef.current = null
      }
    }
  }, [leaving, morphOrigin, enabled, fireLeaveEnd])
  const onContainerTransitionEnd = (e: RTransitionEvent<HTMLDivElement>) => {
    if (leaving && e.propertyName === 'transform' && e.target === e.currentTarget) fireLeaveEnd()
  }

  const goWatchlist = () => navigate('/watchlist')

  const hasUnread = unreadCount > 0
  const containerCls = 'sr-wl-float'
    + (dragging || resizing ? ' sr-wl-float-no-transition' : '')
    + (dragging ? ' sr-wl-float-dragging' : '')
    + (resizing ? ' sr-wl-float-resizing' : '')
    + (leaving ? ' sr-wl-float-leaving' : '')

  // 尺寸固定（clamp + 固定，CSS 单一事实源）：rect 只取 left/top 定位，
  // 宽高由 CSS 决定——拖拽只提交 x/y，不会污染展开面板尺寸
  const rectStyle = rect ? { left: rect.x, top: rect.y } : undefined

  // 进入/离开收缩态：仅在 morphOrigin 非空且 enabled 时叠加几何动画样式
  const shrinkStyle = (enteringLocal || leaving) && morphOrigin
    ? shrinkTransform(morphOrigin, rect)
    : null
  const containerStyle = shrinkStyle
    ? { ...(rectStyle ?? {}), ...shrinkStyle }
    : rectStyle

  // 行情快照按 code 索引（未读列表项按 code 匹配关注股行情）
  const quoteByCode = useMemo(() => {
    const m = new Map<string, WatchlistItem>()
    for (const w of watchlistData) m.set(w.code, w)
    return m
  }, [watchlistData])

  // 离开动画期间不响应拖动（handler 摘除 + CSS pointer-events 双保险）
  const headPointerDownIf = !leaving && enabled ? headPointerDown : undefined

  return (
    <div
      id="sr-wl-float-window"
      ref={floatRef}
      className={containerCls}
      style={containerStyle}
      onTransitionEnd={onContainerTransitionEnd}
      role="region"
      aria-label="关注提醒"
    >
      {/* 头部：整行可拖（按钮组 stopPropagation 排除；移动端 sheet 不可拖） */}
      <div
        className="sr-wl-float-head"
        onPointerDown={headPointerDownIf}
      >
        {enabled && (
          <span className="sr-wl-float-handle" aria-hidden="true">
            <IconGripVertical size={14} />
          </span>
        )}
        <span className="sr-wl-float-title">
          {hasUnread
            ? `关注提醒 · ${unreadCount} 只新触发`
            : `已关注 ${watchlistData.length} 只 · 无新触发`}
        </span>
        <div className="sr-wl-float-spacer" />
        <div className="sr-wl-float-actions" onPointerDown={(e) => e.stopPropagation()}>
          <IconTextButton
            icon={<IconEye size={14} aria-hidden />} text="查看关注页" type="link" size="small"
            className="sr-wl-float-link" buttonClassName="sr-wl-float-act-btn" tooltip="查看关注页" ariaLabel="查看关注页"
            onClick={goWatchlist}
          />
          {hasUnread && (
            <IconTextButton
              icon={<IconCheck size={14} aria-hidden />} text="全部已读" size="small" buttonClassName="sr-wl-float-act-btn"
              tooltip="全部标记已读" ariaLabel="全部已读"
              onClick={markAllSeen}
            />
          )}
          <IconTextButton
            icon={<IconX size={14} aria-hidden />} text={null} type="text" size="small"
            className="sr-wl-float-close" tooltip="关闭" ariaLabel="关闭"
            onClick={onClose}
          />
        </div>
      </div>

      {/* 列表常驻 DOM（T-70 移除 chip 折叠后恒为面板态，无 max-height 折叠过渡） */}
      {hasUnread ? (
        /* ===== 未读列表 ===== */
        <div className="sr-wl-float-list">
          {alerts.map((a) => {
            const extra = a.signals.length - 3
            return (
              <div key={a.code} className="sr-wl-float-item">
                <div className="sr-wl-float-item-head">
                  <IconTextButton
                    icon={<IconStarFilled style={{ color: 'var(--sr-star)', width: 'var(--sr-font-xs)', height: 'var(--sr-font-xs)' }} aria-hidden />}
                    text={null} type="text" size="small" tooltip="取消关注" ariaLabel="取消关注"
                    onClick={() => toggleWatch(a.code, a.name)}
                  />
                  <Button variant="transparent" size="compact-xs" className="sr-wl-float-item-name"
                    style={{ padding: 0, height: 'auto', lineHeight: 1.6, justifyContent: 'flex-start' }}
                    onClick={() => navigate(`/stocks/${a.code}`)}>
                    {a.name}({a.code})
                  </Button>
                  {/* 价格 + 涨跌幅（数字化容错，缺价格整段不渲染） */}
                  <QuoteSegment w={quoteByCode.get(a.code)} />
                </div>
                <div className="sr-wl-float-item-sigs">
                  {a.signals.slice(0, 3).map((s) => (
                    <SignalTag key={s} code={s} labelOf={labelOf} size="small" />
                  ))}
                  {extra > 0 && (
                    <Badge variant="light" color="gray" className="sr-wl-float-more"
                      style={{ padding: '0 var(--sr-pad-xs)', borderRadius: 'var(--sr-radius-tag)', lineHeight: '16px', height: 'auto' }}>
                      +{extra}
                    </Badge>
                  )}
                  {a.signals.length === 0 && (
                    <Badge variant="light" color="gray" className="sr-wl-float-more"
                      style={{ padding: '0 var(--sr-pad-xs)', borderRadius: 'var(--sr-radius-tag)', lineHeight: '16px', height: 'auto' }}>
                      无信号
                    </Badge>
                  )}
                </div>
                <div className="sr-wl-float-item-meta">
                  <AlertMoment a={a} />
                </div>
              </div>
            )
          })}
        </div>
      ) : (
        /* ===== 空态：仅轻提示（关注股列表弱化移除，避免与关注页重复；「查看关注页」在头部） ===== */
        <div className="sr-wl-float-empty">
          <span className="sr-wl-float-empty-text">暂无新触发 · 行情变动实时提醒</span>
        </div>
      )}
    </div>
  )
}
