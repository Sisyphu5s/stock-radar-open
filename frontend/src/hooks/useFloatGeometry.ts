/* ============================================================
   浮窗几何核心(单一事实源):常量 + 纯函数 + 视口跟踪 + 拖动会话
   - 供 useFloatPlacement(关注浮窗)与 useCopilotPlacement(AI 助手)共用,
     替换原先两套独立实现(copilotGeometry 通用部分 + useFloatPlacement 复制实现)
   - 纯函数:边缘钳制 / 归一化 / launcher 吸附 / 底部安全线
   - 拖动会话:窗口拖动 + launcher 拖动状态机(pointer 位移阈值、rAF 限频、
     互斥、pointerup 提交);数据状态(placement/launcher 持久化)由外层 hook 持有,
     本模块只提供交互能力与视口跟踪
   - 移动端(<768)enabled=false,外层决定几何是否返回 null
   ============================================================ */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { MutableRefObject, PointerEvent as RPointerEvent } from 'react'

/** launcher 圆形按钮直径(px) */
export const LAUNCHER_SIZE = 36
/** 视口边缘留白(px) */
export const EDGE_GAP = 12
/** pointer 位移阈值:超过才视为拖动/缩放,否则视为点击,不做任何提交 */
export const DRAG_THRESHOLD = 5
/** 拖拽释放时贴近边缘的吸附距离(px) */
export const SNAP_DISTANCE = 56
/** 移动端断点:视口宽低于此值视为移动端 */
export const MOBILE_BREAKPOINT = 768

export type LauncherSide = 'left' | 'right'
export interface LauncherPlacement {
  side: LauncherSide
  /** 归一化 y [0,1]:launcher 顶部在 [bottomInset, vh - size - gap] 区间 */
  y: number
}
export interface Rect {
  x: number
  y: number
  w: number
  h: number
}

export const clamp01 = (n: number): number => Math.min(1, Math.max(0, n))
export const clampRange = (v: number, min: number, max: number): number => Math.min(max, Math.max(min, v))

/** 读取 CSS 变量(--sr-bottombar-h / --sr-safe-bottom 随断点变化,实时读取最稳) */
export function readCssVar(name: string, fallback: number): number {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
    const n = parseFloat(v)
    return Number.isFinite(n) ? n : fallback
  } catch {
    return fallback
  }
}

/** launcher 距视口底部安全距离:16 + 底部导航高 + 安全区(避让常量见 styles/float-tokens.css) */
export function launcherBottomInset(): number {
  return readCssVar('--sr-float-edge', 16) + readCssVar('--sr-bottombar-h', 0) + readCssVar('--sr-safe-bottom', 0)
}

/** 矩形视口钳制:永不离开视口(边缘留 EDGE_GAP);窄视口下 max 兜底保证不产生负值/NaN */
export function clampRect(r: Rect, vw: number, vh: number): Rect {
  const minX = EDGE_GAP
  const maxX = Math.max(minX, vw - r.w - EDGE_GAP)
  const minY = EDGE_GAP
  const maxY = Math.max(minY, vh - r.h - EDGE_GAP)
  return { x: clampRange(r.x, minX, maxX), y: clampRange(r.y, minY, maxY), w: r.w, h: r.h }
}

/** launcher 矩形钳制:底部不低于安全线(bottombar + 安全区) */
export function clampLauncherRect(r: { x: number; y: number }, vw: number, vh: number): { x: number; y: number } {
  const minX = EDGE_GAP
  const maxX = Math.max(minX, vw - LAUNCHER_SIZE - EDGE_GAP)
  const minY = launcherBottomInset()
  const maxY = Math.max(minY, vh - LAUNCHER_SIZE - EDGE_GAP)
  return { x: clampRange(r.x, minX, maxX), y: clampRange(r.y, minY, maxY) }
}

/** launcher 释放吸附:取最近的左/右边缘(中心 x 二分),y 归一化 */
export function launcherFromDrop(centerX: number, top: number, vw: number, vh: number): LauncherPlacement {
  const side: LauncherSide = centerX < vw / 2 ? 'left' : 'right'
  const minY = launcherBottomInset()
  const maxY = Math.max(minY, vh - LAUNCHER_SIZE - EDGE_GAP)
  return { side, y: clamp01((top - minY) / (maxY - minY)) }
}

/** launcher 像素位置;null(未拖过)返回 null,由 CSS 默认样式定位 */
export function launcherRect(p: LauncherPlacement | null, vw: number, vh: number): { x: number; y: number } | null {
  if (!p) return null
  const minY = launcherBottomInset()
  const maxY = Math.max(minY, vh - LAUNCHER_SIZE - EDGE_GAP)
  const y = minY + clamp01(p.y) * (maxY - minY)
  const x = p.side === 'left' ? EDGE_GAP : Math.max(EDGE_GAP, vw - LAUNCHER_SIZE - EDGE_GAP)
  return { x, y }
}

/** 浮窗在视口内的可用移动范围(像素) */
export function freeRange(w: number, h: number, vw: number, vh: number): { xRange: number; yRange: number } {
  return {
    xRange: Math.max(1, vw - w - EDGE_GAP * 2),
    yRange: Math.max(1, vh - h - EDGE_GAP * 2),
  }
}

export function normToPx(n: number, range: number): number {
  return EDGE_GAP + clamp01(n) * range
}

export function pxToNorm(px: number, range: number): number {
  return clamp01((px - EDGE_GAP) / range)
}

/* ==================== 拖动会话 ==================== */

export interface WindowDragSession {
  pointerId: number
  startX: number
  startY: number
  /** 被拖矩形的左上角 + 初始尺寸(拖动期间保持) */
  baseX: number
  baseY: number
  baseW: number
  baseH: number
  dx: number
  dy: number
  moved: boolean
}

export interface LauncherDragSession {
  pointerId: number
  startX: number
  startY: number
  baseX: number
  baseY: number
  dx: number
  dy: number
  moved: boolean
}

export interface FloatGeometryApi {
  viewport: { w: number; h: number }
  /** 视口宽 >= MOBILE_BREAKPOINT */
  enabled: boolean
  /** 窗口拖动会话(实时;null=空闲) */
  windowDrag: WindowDragSession | null
  /** 开始窗口拖动;base=初始矩形(px);onDrop=释放回调(已钳制矩形 + 当前视口) */
  beginWindowDrag: (
    e: RPointerEvent<HTMLDivElement>,
    base: Rect,
    onDrop: (r: Rect, vw: number, vh: number) => void,
    opts?: { mobileDisabled?: boolean },
  ) => void
  /** launcher 拖动会话(实时;null=空闲) */
  launcherDrag: LauncherDragSession | null
  /** 开始 launcher 拖动;base=初始左上角(px);onDrop=释放回调(已钳制 + 当前视口) */
  beginLauncherDrag: (
    e: RPointerEvent<HTMLButtonElement>,
    base: { x: number; y: number },
    onDrop: (r: { x: number; y: number }, vw: number, vh: number) => void,
    opts?: { mobileDisabled?: boolean },
  ) => void
  /** 拖动结束置 true,消费方 onClick 检查后清掉,防拖动误触发开/关 */
  suppressClickRef: MutableRefObject<boolean>
}

/**
 * 浮窗/launcher 交互核心:视口跟踪 + 窗口/launcher 拖动会话。
 * 数据(placement/launcher 持久化)由外层 hook 持有,通过 begin* 的 onDrop 回调提交。
 */
export function useFloatGeometry(): FloatGeometryApi {
  const [viewport, setViewport] = useState(() => ({ w: window.innerWidth, h: window.innerHeight }))
  const [windowDrag, setWindowDrag] = useState<WindowDragSession | null>(null)
  const [launcherDrag, setLauncherDrag] = useState<LauncherDragSession | null>(null)
  const windowDragRef = useRef<WindowDragSession | null>(null)
  const launcherDragRef = useRef<LauncherDragSession | null>(null)
  const suppressClickRef = useRef(false)
  // pointermove 高频限频:rAF 合并,一帧最多一次 setDrag(避免每 pointermove 全树重渲染)
  const windowRafRef = useRef<number | null>(null)
  const windowPendingRef = useRef<WindowDragSession | null>(null)
  const launcherRafRef = useRef<number | null>(null)
  const launcherPendingRef = useRef<LauncherDragSession | null>(null)

  // ---- 视口跟踪(恒挂):window resize + visualViewport resize/scroll。
  // 移动端也挂此监听是为了探测断点切换(<768 启用/禁用几何),值未变时 setViewport 返回原引用不触发重渲染
  useEffect(() => {
    const handle = () => {
      const vw = window.innerWidth
      const vh = window.innerHeight
      setViewport((prev) => (prev.w === vw && prev.h === vh ? prev : { w: vw, h: vh }))
    }
    handle()
    window.addEventListener('resize', handle)
    const vv = window.visualViewport
    vv?.addEventListener('resize', handle)
    vv?.addEventListener('scroll', handle)
    return () => {
      window.removeEventListener('resize', handle)
      vv?.removeEventListener('resize', handle)
      vv?.removeEventListener('scroll', handle)
    }
  }, [])

  const enabled = viewport.w >= MOBILE_BREAKPOINT

  // ---- 窗口拖动会话(与 launcher 拖动互斥) ----
  const beginWindowDrag = useCallback((
    e: RPointerEvent<HTMLDivElement>,
    base: Rect,
    onDrop: (r: Rect, vw: number, vh: number) => void,
    opts?: { mobileDisabled?: boolean },
  ) => {
    if (e.button !== 0) return
    if (windowDragRef.current || launcherDragRef.current) return // 互斥:一种进行中时忽略另一种的 pointerdown
    if (opts?.mobileDisabled && window.innerWidth < MOBILE_BREAKPOINT) return // 移动端空操作
    e.preventDefault()

    const setSession = (s: WindowDragSession | null) => {
      windowDragRef.current = s
      setWindowDrag(s)
    }
    setSession({
      pointerId: e.pointerId, startX: e.clientX, startY: e.clientY,
      baseX: base.x, baseY: base.y, baseW: base.w, baseH: base.h, dx: 0, dy: 0, moved: false,
    })

    // pointermove 高频限频:rAF 合并,一帧最多一次 setSession
    const schedule = () => {
      if (windowRafRef.current != null) return
      windowRafRef.current = requestAnimationFrame(() => {
        windowRafRef.current = null
        const d = windowPendingRef.current
        if (d) { windowPendingRef.current = null; setSession(d) }
      })
    }
    // 立即落最新 pending 拖拽位置(pointerup 收尾前调用,保证落点 dx/dy 为最后一次移动值)
    const flush = () => {
      if (windowRafRef.current != null) { cancelAnimationFrame(windowRafRef.current); windowRafRef.current = null }
      const d = windowPendingRef.current
      if (d) { windowPendingRef.current = null; setSession(d) }
    }
    const clearPending = () => {
      if (windowRafRef.current != null) { cancelAnimationFrame(windowRafRef.current); windowRafRef.current = null }
      windowPendingRef.current = null
    }

    const onMove = (ev: PointerEvent) => {
      const prev = windowDragRef.current
      if (!prev || prev.pointerId !== ev.pointerId) return
      const dx = ev.clientX - prev.startX
      const dy = ev.clientY - prev.startY
      if (!prev.moved && Math.hypot(dx, dy) <= DRAG_THRESHOLD) return
      windowPendingRef.current = { ...prev, dx, dy, moved: true }
      schedule()
    }

    const cleanup = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onCancel)
      clearPending()
    }

    const onUp = (ev: PointerEvent) => {
      // 先落最新 pending 位置再清理监听,保证落点 dx/dy 为最后一次移动值
      flush()
      cleanup()
      const d = windowDragRef.current
      if (!d || d.pointerId !== ev.pointerId) return
      setSession(null)
      if (!d.moved) return // 未超过阈值 → 视为点击,不做任何提交
      const vw = window.innerWidth
      const vh = window.innerHeight
      onDrop(clampRect({ x: d.baseX + d.dx, y: d.baseY + d.dy, w: d.baseW, h: d.baseH }, vw, vh), vw, vh)
    }

    const onCancel = () => {
      clearPending()
      cleanup()
      setSession(null)
    }

    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onCancel)
  }, [])

  // ---- launcher 拖动会话(与窗口拖动互斥) ----
  const beginLauncherDrag = useCallback((
    e: RPointerEvent<HTMLButtonElement>,
    base: { x: number; y: number },
    onDrop: (r: { x: number; y: number }, vw: number, vh: number) => void,
    opts?: { mobileDisabled?: boolean },
  ) => {
    if (e.button !== 0) return
    if (launcherDragRef.current || windowDragRef.current) return // 互斥
    if (opts?.mobileDisabled && window.innerWidth < MOBILE_BREAKPOINT) return // 移动端空操作
    e.preventDefault()

    const setSession = (s: LauncherDragSession | null) => {
      launcherDragRef.current = s
      setLauncherDrag(s)
    }
    setSession({
      pointerId: e.pointerId, startX: e.clientX, startY: e.clientY,
      baseX: base.x, baseY: base.y, dx: 0, dy: 0, moved: false,
    })

    const schedule = () => {
      if (launcherRafRef.current != null) return
      launcherRafRef.current = requestAnimationFrame(() => {
        launcherRafRef.current = null
        const d = launcherPendingRef.current
        if (d) { launcherPendingRef.current = null; setSession(d) }
      })
    }
    const flush = () => {
      if (launcherRafRef.current != null) { cancelAnimationFrame(launcherRafRef.current); launcherRafRef.current = null }
      const d = launcherPendingRef.current
      if (d) { launcherPendingRef.current = null; setSession(d) }
    }
    const clearPending = () => {
      if (launcherRafRef.current != null) { cancelAnimationFrame(launcherRafRef.current); launcherRafRef.current = null }
      launcherPendingRef.current = null
    }

    const onMove = (ev: PointerEvent) => {
      const prev = launcherDragRef.current
      if (!prev || prev.pointerId !== ev.pointerId) return
      const dx = ev.clientX - prev.startX
      const dy = ev.clientY - prev.startY
      if (!prev.moved && Math.hypot(dx, dy) <= DRAG_THRESHOLD) return
      launcherPendingRef.current = { ...prev, dx, dy, moved: true }
      schedule()
    }

    const cleanup = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onCancel)
      clearPending()
    }

    const onUp = (ev: PointerEvent) => {
      flush()
      cleanup()
      const d = launcherDragRef.current
      if (!d || d.pointerId !== ev.pointerId) return
      setSession(null)
      if (!d.moved) return // 未超过阈值 → 视为点击,不提交位置,由 onClick 处理
      suppressClickRef.current = true
      // click 紧随 pointerup 派发;下一轮清掉,避免误吞后续真实点击
      setTimeout(() => { suppressClickRef.current = false }, 0)
      const vw = window.innerWidth
      const vh = window.innerHeight
      onDrop(clampLauncherRect({ x: d.baseX + d.dx, y: d.baseY + d.dy }, vw, vh), vw, vh)
    }

    const onCancel = () => {
      clearPending()
      cleanup()
      setSession(null)
    }

    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onCancel)
  }, [])

  return {
    viewport,
    enabled,
    windowDrag,
    beginWindowDrag,
    launcherDrag,
    beginLauncherDrag,
    suppressClickRef,
  }
}
