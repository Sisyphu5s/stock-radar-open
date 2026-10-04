/* ============================================================
   浮窗系统统一抽象:两套浮窗(关注提醒 / AI 助手)的参数化几何 hook。
   - 共同内核 useFloatGeometry(hooks/useFloatGeometry.ts)保留不动:
     视口跟踪 / 拖动会话 / 钳制 / 吸附纯函数单一事实源;
   - 本模块持有两套系统的差异参数(持久化 key / 尺寸 / 停靠 / 移动端 sheet),
     输出统一的 FloatWindowApi;useFloatPlacement(关注)与
     components/copilot/useCopilotPlacement(AI 助手)各自以参数化实例消费,
     对外 API 签名保留,消费方零改动(见 06-components.md §5)。
   - 位置模型:统一 WindowPlacement(edge 停靠 / free 自由),归一化坐标 [0,1];
     watchlist(无停靠菜单)表现为恒 free + 拖动只提交 x/y;
     copilot(可停靠)额外提供 edge 吸附与 dockTo。
   - 移动端(<768):enabled=false,几何恒 null,由渲染层降级为底部 sheet。
   ============================================================ */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { MutableRefObject, PointerEvent as RPointerEvent, RefObject } from 'react'
import {
  EDGE_GAP, LAUNCHER_SIZE,
  clamp01, clampLauncherRect, clampRect, freeRange, launcherBottomInset, launcherFromDrop,
  launcherRect, normToPx, pxToNorm, readCssVar, useFloatGeometry,
} from './useFloatGeometry'
import type { LauncherPlacement } from './useFloatGeometry'

/* ==================== 通用类型(提升自 copilotGeometry,06 §5.2) ==================== */

export type DockEdge = 'left' | 'right' | 'bottom'
export type DockAction = DockEdge | 'free'

export type WindowPlacement =
  | { mode: 'edge'; edge: DockEdge; x: number; y: number }
  | { mode: 'free'; x: number; y: number }

/** launcher 持久化结构:吸附侧 + 归一化 y [0,1](两套系统同构) */
export type { LauncherPlacement, LauncherSide } from './useFloatGeometry'

/* ==================== 参数化配置(两套系统差异全部集中于此) ==================== */

export interface FloatWindowConfig {
  /** 窗口位置持久化 key(两套系统各自独立,不合并历史存储) */
  storageKey: string
  /** launcher 独立持久化 key;缺省时 launcher 随窗口同一对象持久化(copilot 形态) */
  launcherStorageKey?: string
  /** 桌面窗口固定宽度(px);CSS width 需与之一致(min(400px, 100vw-32px)) */
  width: number
  /** 窗口高度上限(px):视口高 - 上下留白(CSS max-height 需同步) */
  maxHeight: number
  /** 桌面窗口上下留白(px) */
  vMargin: number
  /** 移动端形态:'sheet'(底部面板,不参与几何) | 'none'(由调用方自理) */
  mobile: 'sheet' | 'none'
  /** 是否可停靠(有停靠菜单):false = 无停靠菜单的纯自由位置(关注浮窗) */
  dockable?: boolean
  /** 默认位置(未拖过且无持久化时);缺省 null = CSS 默认定位 */
  defaultPlacement?: WindowPlacement
  /** 底部停靠避让量(px):底部停靠时窗口底边高于视口底 EDGE_GAP + 本值,
   *  为右下角关注浮窗/launcher 默认区留出空间(对齐 --sr-float-bottom-clearance) */
  bottomClearance?: () => number
}

/* ==================== 纯函数:归一化 <-> 像素(提升自 copilotGeometry) ==================== */

/** 窗口矩形钳制:永不离开视口 */
function clampWindowRect(r: { x: number; y: number; w: number; h: number }, vw: number, vh: number): { x: number; y: number } {
  const c = clampRect(r, vw, vh)
  return { x: c.x, y: c.y }
}

/** placement → 窗口左上角像素(含钳制,保证任何输入都不离屏) */
export function placementToRect(p: WindowPlacement, w: number, h: number, vw: number, vh: number, bottomClearance?: () => number): { x: number; y: number } {
  const { xRange, yRange } = freeRange(w, h, vw, vh)
  if (p.mode === 'edge') {
    if (p.edge === 'bottom') {
      // 底部停靠:贴底但留出避让区(右下角关注浮窗默认区,见 bottomClearance)
      const clearance = bottomClearance?.() ?? 0
      return clampWindowRect({ x: (vw - w) / 2, y: Math.max(EDGE_GAP, vh - h - EDGE_GAP - clearance), w, h }, vw, vh)
    }
    const y = normToPx(p.y, yRange)
    const x = p.edge === 'left' ? EDGE_GAP : Math.max(EDGE_GAP, vw - w - EDGE_GAP)
    return clampWindowRect({ x, y, w, h }, vw, vh)
  }
  return clampWindowRect({ x: normToPx(p.x, xRange), y: normToPx(p.y, yRange), w, h }, vw, vh)
}

/** 拖拽释放 → 自由位置(无停靠菜单形态:仅归一化,不吸附) */
export function toFreePlacement(px: number, py: number, w: number, h: number, vw: number, vh: number): WindowPlacement {
  const rect = clampWindowRect({ x: px, y: py, w, h }, vw, vh)
  const { xRange, yRange } = freeRange(w, h, vw, vh)
  return { mode: 'free', x: pxToNorm(rect.x, xRange), y: pxToNorm(rect.y, yRange) }
}

/** 拖拽释放(可停靠形态):贴近左/右/底边缘则吸附,否则自由位置 */
export function placementFromDrop(px: number, py: number, w: number, h: number, vw: number, vh: number): WindowPlacement {
  const rect = clampWindowRect({ x: px, y: py, w, h }, vw, vh)
  const { xRange, yRange } = freeRange(w, h, vw, vh)
  const dxLeft = rect.x - EDGE_GAP
  const dxRight = vw - (rect.x + w) - EDGE_GAP
  const dyBottom = vh - (rect.y + h) - EDGE_GAP
  const best = Math.min(dxLeft, dxRight, dyBottom)
  const SNAP = 56
  if (best === dxLeft && dxLeft <= SNAP) return { mode: 'edge', edge: 'left', x: 0, y: pxToNorm(rect.y, yRange) }
  if (best === dxRight && dxRight <= SNAP) return { mode: 'edge', edge: 'right', x: 0, y: pxToNorm(rect.y, yRange) }
  if (best === dyBottom && dyBottom <= SNAP) return { mode: 'edge', edge: 'bottom', x: 0, y: 0 }
  return { mode: 'free', x: pxToNorm(rect.x, xRange), y: pxToNorm(rect.y, yRange) }
}

/** resize 后钳制 placement 的归一化值(保证持久化坐标始终有效);未变化时返回原引用 */
export function clampPlacementNorm(p: WindowPlacement, w: number, h: number, vw: number, vh: number): WindowPlacement {
  if (p.mode === 'free') {
    const { xRange, yRange } = freeRange(w, h, vw, vh)
    const rect = placementToRect(p, w, h, vw, vh)
    const x = pxToNorm(rect.x, xRange)
    const y = pxToNorm(rect.y, yRange)
    if (Math.abs(x - p.x) <= 0.001 && Math.abs(y - p.y) <= 0.001) return p
    return { mode: 'free', x, y }
  }
  if (p.mode === 'edge' && p.edge !== 'bottom') {
    const { yRange } = freeRange(w, h, vw, vh)
    const rect = placementToRect(p, w, h, vw, vh)
    const y = pxToNorm(rect.y, yRange)
    if (Math.abs(y - p.y) <= 0.001) return p
    return { ...p, y }
  }
  return p
}

/* ==================== 持久化读写(两套存储格式经 config 参数化) ==================== */

/** 校验 + 归一化 launcher 持久化({ side, y }),非法返回 null */
function sanitizeLauncher(p: Partial<LauncherPlacement> | null | undefined): LauncherPlacement | null {
  if (!p) return null
  if (p.side !== 'left' && p.side !== 'right') return null
  if (typeof p.y !== 'number' || !Number.isFinite(p.y)) return null
  return { side: p.side, y: clamp01(p.y) }
}

/** 读窗口位置:按 config 区分两种存储格式(watchlist 归一化 free / copilot WindowPlacement) */
function loadWindowPlacement(cfg: FloatWindowConfig): WindowPlacement | null {
  try {
    const raw = localStorage.getItem(cfg.storageKey)
    if (!raw) return cfg.defaultPlacement ?? null
    const p = JSON.parse(raw) as Record<string, unknown> & { v?: unknown } & { x?: unknown; y?: unknown; window?: Partial<WindowPlacement> }
    if (p?.v !== 1) return cfg.defaultPlacement ?? null
    if (cfg.dockable) {
      // copilot 形态:{ v, launcher, window }
      const w = p.window
      if (w?.mode === 'free' && typeof w.x === 'number' && typeof w.y === 'number') {
        return { mode: 'free', x: clamp01(w.x), y: clamp01(w.y) }
      }
      if (w?.mode === 'edge' && (w.edge === 'left' || w.edge === 'right' || w.edge === 'bottom')) {
        return { mode: 'edge', edge: w.edge, x: 0, y: typeof w.y === 'number' ? clamp01(w.y) : 0.5 }
      }
      return cfg.defaultPlacement ?? null
    }
    // watchlist 形态:{ v, x, y, w, h }(w/h 为固定尺寸,忽略)
    if (typeof p.x !== 'number' || typeof p.y !== 'number') return cfg.defaultPlacement ?? null
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) return cfg.defaultPlacement ?? null
    return { mode: 'free', x: clamp01(p.x), y: clamp01(p.y) }
  } catch {
    return cfg.defaultPlacement ?? null
  }
}

/** 写窗口位置:按 config 区分两种存储格式 */
function saveWindowPlacement(cfg: FloatWindowConfig, p: WindowPlacement, vh: number) {
  try {
    if (cfg.dockable) {
      // copilot 形态:与 launcher 同对象合并写(launcher 由外部读,避免覆盖)
      let existing: { launcher?: LauncherPlacement | null } = {}
      try {
        const raw = localStorage.getItem(cfg.storageKey)
        if (raw) existing = JSON.parse(raw) as { launcher?: LauncherPlacement | null }
      } catch { /* ignore */ }
      localStorage.setItem(cfg.storageKey, JSON.stringify({ v: 1, launcher: existing.launcher ?? null, window: p }))
    } else {
      // watchlist 形态:补固定尺寸 w/h 兼容旧存储
      const h = Math.min(cfg.maxHeight, vh - cfg.vMargin)
      localStorage.setItem(cfg.storageKey, JSON.stringify({ v: 1, x: p.x, y: p.y, w: cfg.width, h }))
    }
  } catch { /* ignore quota / unavailable storage */ }
}

/** 读 launcher 位置:独立 key(watchlist)或随窗口对象(copilot) */
function loadLauncherPlacement(cfg: FloatWindowConfig): LauncherPlacement | null {
  try {
    if (cfg.launcherStorageKey) {
      const raw = localStorage.getItem(cfg.launcherStorageKey)
      if (!raw) return null
      const p = JSON.parse(raw) as Partial<LauncherPlacement> & { v?: unknown }
      if (p?.v !== 1) return null
      return sanitizeLauncher(p)
    }
    const raw = localStorage.getItem(cfg.storageKey)
    if (!raw) return null
    const p = JSON.parse(raw) as { launcher?: Partial<LauncherPlacement> }
    return sanitizeLauncher(p.launcher)
  } catch {
    return null
  }
}

function saveLauncherPlacement(cfg: FloatWindowConfig, p: LauncherPlacement | null) {
  try {
    if (cfg.launcherStorageKey) {
      localStorage.setItem(cfg.launcherStorageKey, JSON.stringify({ v: 1, ...p }))
    } else {
      // copilot 形态:与 window 合并写(读出现有 window,防覆盖)
      let existing: { window?: WindowPlacement } = {}
      try {
        const raw = localStorage.getItem(cfg.storageKey)
        if (raw) existing = JSON.parse(raw) as { window?: WindowPlacement }
      } catch { /* ignore */ }
      localStorage.setItem(cfg.storageKey, JSON.stringify({ v: 1, launcher: p, window: existing.window ?? null }))
    }
  } catch { /* ignore */ }
}

/* ==================== 统一 hook ==================== */

export interface FloatWindowApi {
  enabled: boolean
  isMobile: boolean
  winRef: RefObject<HTMLDivElement | null>
  /** 窗口左上角像素(拖动中实时 / 空闲由持久化换算);移动端 null */
  winRect: { x: number; y: number } | null
  winDragging: boolean
  /** 窗口头部拖拽起点 */
  windowPointerDown: (e: RPointerEvent<HTMLDivElement>) => void
  launcherRef: RefObject<HTMLButtonElement | null>
  /** null = 未拖过,用 CSS 默认定位 */
  launcherStyle: { left: number; top: number } | null
  /** launcher 中心点像素;launcherStyle 非 null 时返回(消费方回退 CSS 默认锚点) */
  launcherCenter: { x: number; y: number } | null
  launcherPointerDown: (e: RPointerEvent<HTMLButtonElement>) => void
  launcherDragging: boolean
  suppressClickRef: MutableRefObject<boolean>
  /** 停靠菜单动作(仅 dockable 时提供) */
  dockTo?: (action: DockAction) => void
}

/**
 * 浮窗统一几何 hook:参数化两套浮窗系统的差异(持久化 key / 尺寸 / 停靠 / 移动端 sheet)。
 * 内核走 useFloatGeometry(单一事实源),本模块只持有 placement/launcher 数据状态
 * 与持久化读写;拖动会话经 beginWindowDrag/beginLauncherDrag 的 onDrop 回调提交。
 * 移动端(<768)不参与几何,由渲染层降级为底部 sheet。
 */
export function useFloatWindow(cfg: FloatWindowConfig): FloatWindowApi {
  const g = useFloatGeometry()
  const [placement, setPlacement] = useState<WindowPlacement | null>(() => loadWindowPlacement(cfg))
  const [launcherPos, setLauncherPos] = useState<LauncherPlacement | null>(() => loadLauncherPlacement(cfg))

  const winRef = useRef<HTMLDivElement | null>(null)
  const launcherRef = useRef<HTMLButtonElement | null>(null)
  const placementRef = useRef(placement)
  const launcherPosRef = useRef(launcherPos)

  useEffect(() => { placementRef.current = placement }, [placement])
  useEffect(() => { launcherPosRef.current = launcherPos }, [launcherPos])

  /** 固定尺寸换算:与 CSS width: min(400px, 100vw-32px) / max-height 语义一致 */
  const defaultSize = useCallback((vh: number): { w: number; h: number } => ({
    w: cfg.width,
    h: Math.min(cfg.maxHeight, vh - cfg.vMargin),
  }), [cfg.width, cfg.maxHeight, cfg.vMargin])

  const commit = useCallback((p: WindowPlacement) => {
    placementRef.current = p
    setPlacement(p)
    saveWindowPlacement(cfg, p, window.innerHeight)
  }, [cfg])

  const commitLauncher = useCallback((p: LauncherPlacement | null) => {
    launcherPosRef.current = p
    setLauncherPos(p)
    saveLauncherPlacement(cfg, p)
  }, [cfg])

  const enabled = g.enabled
  const isMobile = !enabled

  // ---- 视口变化:窗口钳制归一化坐标 + launcher 钳制 y(移动端跳过) ----
  const viewport = g.viewport
  useEffect(() => {
    if (!enabled) return
    const cur = placementRef.current
    if (cur) {
      const d = defaultSize(viewport.h)
      const next = clampPlacementNorm(cur, d.w, d.h, viewport.w, viewport.h)
      if (next !== cur) commit(next)
    }
    const lp = launcherPosRef.current
    if (lp) {
      const r = launcherRect(lp, viewport.w, viewport.h)
      if (r) {
        const minY = launcherBottomInset()
        const maxY = Math.max(minY, viewport.h - LAUNCHER_SIZE - EDGE_GAP)
        const y = clamp01((r.y - minY) / (maxY - minY))
        if (Math.abs(y - lp.y) > 0.001) commitLauncher({ side: lp.side, y })
      }
    }
  }, [viewport, enabled, commit, commitLauncher, defaultSize])

  // ---- 窗口拖拽基准:placement 换算;null(未拖过)用元素实测位置(CSS 默认定位) ----
  const baseWindowRect = useCallback((): { x: number; y: number; w: number; h: number } => {
    const vw = window.innerWidth
    const vh = window.innerHeight
    const d = defaultSize(vh)
    const cur = placementRef.current
    if (cur) {
      const tl = placementToRect(cur, d.w, d.h, vw, vh, cfg.dockable ? cfg.bottomClearance : undefined)
      return { ...tl, w: d.w, h: d.h }
    }
    const b = winRef.current?.getBoundingClientRect()
    return {
      x: b?.left ?? vw - d.w - EDGE_GAP,
      y: b?.top ?? vh - d.h - EDGE_GAP,
      w: b?.width ?? d.w,
      h: b?.height ?? d.h,
    }
  }, [cfg, defaultSize])

  const windowPointerDown = useCallback((e: RPointerEvent<HTMLDivElement>) => {
    const base = baseWindowRect()
    g.beginWindowDrag(e, base, (r, vw, vh) => {
      // 可停靠形态走吸附;否则纯自由归一化(尺寸固定,只提交位置)
      const next = cfg.dockable
        ? placementFromDrop(r.x, r.y, r.w, r.h, vw, vh)
        : toFreePlacement(r.x, r.y, r.w, r.h, vw, vh)
      commit(next)
    }, { mobileDisabled: true })
  }, [g, baseWindowRect, commit, cfg.dockable])

  // ---- launcher 拖拽基准:持久化位置换算;null(未拖过)用元素实测位置 ----
  const launcherBase = useCallback((): { x: number; y: number } => {
    const vw = window.innerWidth
    const vh = window.innerHeight
    const r = launcherRect(launcherPosRef.current, vw, vh)
    const b = launcherRef.current?.getBoundingClientRect()
    return {
      x: r?.x ?? b?.left ?? vw - LAUNCHER_SIZE - readCssVar('--sr-float-edge', 16),
      y: r?.y ?? b?.top ?? vh - LAUNCHER_SIZE - readCssVar('--sr-float-edge', 16),
    }
  }, [])

  const launcherPointerDown = useCallback((e: RPointerEvent<HTMLButtonElement>) => {
    const base = launcherBase()
    g.beginLauncherDrag(e, base, (r, vw, vh) => {
      commitLauncher(launcherFromDrop(r.x + LAUNCHER_SIZE / 2, r.y, vw, vh))
    }, { mobileDisabled: true })
  }, [g, launcherBase, commitLauncher])

  // ---- 实时像素位置:拖动中返回实时钳制值;空闲由持久化换算;移动端 null ----
  const winRect: { x: number; y: number } | null = (() => {
    if (!enabled) return null
    const d = defaultSize(viewport.h)
    const drag = g.windowDrag
    if (drag?.moved) {
      // 拖动钳制用拖动基准实测宽高(chip 折叠态实际 ~260px,而非配置的展开宽 400px),
      // 否则 chip 态贴边时按 400px 提前钳制,表现为「拖不到边」的残留空档(浮窗遗留 A4.1)
      return clampWindowRect({ x: drag.baseX + drag.dx, y: drag.baseY + drag.dy, w: drag.baseW, h: drag.baseH }, viewport.w, viewport.h)
    }
    const cur = placementRef.current
    if (!cur) return null
    return placementToRect(cur, d.w, d.h, viewport.w, viewport.h, cfg.dockable ? cfg.bottomClearance : undefined)
  })()

  const launcherStyle: { left: number; top: number } | null = (() => {
    if (!enabled) return null
    const drag = g.launcherDrag
    if (drag?.moved) {
      const r = clampLauncherRect({ x: drag.baseX + drag.dx, y: drag.baseY + drag.dy }, viewport.w, viewport.h)
      return { left: r.x, top: r.y }
    }
    const r = launcherRect(launcherPosRef.current, viewport.w, viewport.h)
    return r ? { left: r.x, top: r.y } : null
  })()

  const launcherCenter: { x: number; y: number } | null =
    launcherStyle ? { x: launcherStyle.left + LAUNCHER_SIZE / 2, y: launcherStyle.top + LAUNCHER_SIZE / 2 } : null

  // ---- 停靠菜单(可停靠形态):左 / 右 / 底 / 自由 ----
  const dockTo = useCallback((action: DockAction) => {
    const vw = window.innerWidth
    const vh = window.innerHeight
    const d = defaultSize(vh)
    const cur = placementRef.current ?? { mode: 'free' as const, x: 0.5, y: 0.5 }
    const y0 = cur.mode === 'free' ? cur.y
      : cur.mode === 'edge' && cur.edge !== 'bottom' ? cur.y
        : 0.5
    let next: WindowPlacement
    if (action === 'free') {
      const rect = placementToRect(cur, d.w, d.h, vw, vh, cfg.bottomClearance)
      const { xRange, yRange } = freeRange(d.w, d.h, vw, vh)
      next = { mode: 'free', x: pxToNorm(rect.x, xRange), y: pxToNorm(rect.y, yRange) }
    } else if (action === 'bottom') {
      next = { mode: 'edge', edge: 'bottom', x: 0, y: 0 }
    } else {
      next = { mode: 'edge', edge: action, x: 0, y: y0 }
    }
    commit(next)
  }, [commit, cfg, defaultSize])

  return {
    enabled,
    isMobile,
    winRef,
    winRect,
    winDragging: g.windowDrag?.moved ?? false,
    windowPointerDown,
    launcherRef,
    launcherStyle,
    launcherCenter,
    launcherPointerDown,
    launcherDragging: g.launcherDrag?.moved ?? false,
    suppressClickRef: g.suppressClickRef,
    ...(cfg.dockable ? { dockTo } : {}),
  }
}

/** 读取 CSS 变量(单一事实源见 useFloatGeometry;保持历史导出面,消费方零改动) */
export { readCssVar }
