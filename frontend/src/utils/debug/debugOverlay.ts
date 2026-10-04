/* ============================================================
   Debug 覆盖层状态 hook(useDebugOverlay)
   - 单例外部 store + useSyncExternalStore:DebugOverlay 与 DebugPanel
     各自独立调用本 hook 即共享同一状态,无需 props 传参
   - 激活:监听 'sr-debug-mode'(detail.mode==='visual' 进入可视化工作台)
     + 兼容既有 'sr-debug-changed'(MainLayout toggleDebug 广播:on→visual / off→off)
   - 自监听 document:pointermove(rAF 节流)→ hover 检测 + 光标坐标;
     click → 测量取点 / 元素选中;M 键测量模式;Esc 逐级清除
   - 模块级动作(dbgActions)供面板/覆盖层共用,引用稳定
   ============================================================ */

import { useEffect, useSyncExternalStore } from 'react'
import type { DebugMode, Rect } from './debugTypes'

/** 视口坐标点(core debugTypes 无 Point,UI 覆盖层侧本地定义;坐标为 clientX/Y 口径) */
export interface Point {
  x: number
  y: number
}

/** 一段测量(视口坐标两点) */
export interface Measure {
  p1: Point
  p2: Point
}

export type CollisionKind = 'fixed' | 'overflow-v' | 'overflow-c' | 'breakpoint'

/** 碰撞 Tab 的检查条目(面板写入,覆盖层绘制 overlap 区域与高亮) */
export interface CollisionItem {
  id: string
  kind: CollisionKind
  /** 主元素 describe(点击反查高亮) */
  desc: string
  /** 人类可读说明(面板展示) */
  detail: string
  /** fixed 互撞的重叠区域(视口坐标) */
  overlap?: Rect
  areaPct?: number
}

/** 覆盖层全部状态(单例 store,setState 整体替换触发订阅) */
export interface OverlayState {
  mode: DebugMode
  /** 光标坐标(rAF 节流,测量预览/吸附用) */
  pointer: Point | null
  /** 悬停元素(pointer-events:none 下由 elementFromPoint 检测) */
  hoverEl: Element | null
  /** 选中元素(点击选中;检查器数据源) */
  selectedEl: Element | null
  /** 测量模式:M 键切换;模式下点击取点 */
  measureMode: boolean
  /** 测量第 1 点(未完成的一段) */
  pendingPoint: Point | null
  /** 已完成的测量段列表 */
  measures: Measure[]
  /** 碰撞 Tab 的检查条目 */
  collisionItems: CollisionItem[]
  /** 碰撞点击/检查高亮元素 */
  highlightEl: Element | null
}

const initial = (): OverlayState => ({
  mode: 'off',
  pointer: null,
  hoverEl: null,
  selectedEl: null,
  measureMode: false,
  pendingPoint: null,
  measures: [],
  collisionItems: [],
  highlightEl: null,
})

let state: OverlayState = initial()
const listeners = new Set<() => void>()

const emit = (): void => {
  for (const l of listeners) l()
}

const update = (patch: Partial<OverlayState>): void => {
  state = { ...state, ...patch }
  emit()
}

/** 进入 visual 模式时重置全部瞬态(避免上次会话残留) */
const setMode = (mode: DebugMode): void => {
  if (mode === state.mode) return
  state = mode === 'visual' ? { ...initial(), mode } : { ...state, mode }
  emit()
}

/* -------------------- 文档级监听(引用计数单例) -------------------- */

/** 是否命中 Debug 自身 UI(覆盖层/面板),命中则跳过 hover/选中 */
const isDebugUi = (el: Element | null): boolean => {
  if (!el) return false
  return !!el.closest?.('.sr-dbg-root, .sr-dbg-panel')
}

/** 视口点取元素:排除 html/body/iframe 与 Debug UI */
const pickAt = (x: number, y: number): Element | null => {
  const el = document.elementFromPoint(x, y)
  if (!el) return null
  if (el === document.documentElement || el === document.body) return null
  if (el.tagName === 'IFRAME') return null
  if (isDebugUi(el)) return null
  return el
}

let rafId: number | null = null
let lastPointer: Point | null = null

const onPointerMove = (e: PointerEvent): void => {
  if (state.mode !== 'visual') return
  lastPointer = { x: e.clientX, y: e.clientY }
  if (rafId != null) return
  rafId = requestAnimationFrame(() => {
    rafId = null
    if (state.mode !== 'visual') return
    const p = lastPointer
    lastPointer = null
    if (!p) return
    const hover = pickAt(p.x, p.y)
    // 同一元素内移动且坐标未变时不重建状态(避免无谓重渲染)
    if (hover === state.hoverEl && state.pointer && state.pointer.x === p.x && state.pointer.y === p.y) return
    update({ pointer: p, hoverEl: hover })
  })
}

const onPointerLeave = (): void => {
  if (state.mode !== 'visual') return
  if (state.pointer || state.hoverEl) update({ pointer: null, hoverEl: null })
}

const addMeasurePoint = (p: Point): void => {
  if (!state.pendingPoint) {
    update({ pendingPoint: p })
    return
  }
  update({ pendingPoint: null, measures: [...state.measures, { p1: state.pendingPoint, p2: p }] })
}

const onClick = (e: MouseEvent): void => {
  if (state.mode !== 'visual') return
  if (isDebugUi(e.target as Element | null)) return
  const p = { x: e.clientX, y: e.clientY }
  if (state.measureMode) {
    addMeasurePoint(p)
    return
  }
  update({ selectedEl: pickAt(p.x, p.y) })
}

const onKeyDown = (e: KeyboardEvent): void => {
  if (state.mode !== 'visual') return
  const t = e.target as HTMLElement | null
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
  if (e.isComposing) return
  if (e.key === 'm' || e.key === 'M') {
    if (e.metaKey || e.ctrlKey || e.altKey) return
    update({ measureMode: !state.measureMode })
  } else if (e.key === 'Escape') {
    // 逐级清除:未完成测量点 → 选中 → 测量列表 → 碰撞高亮 → 碰撞列表
    if (state.measureMode && state.pendingPoint) update({ pendingPoint: null })
    else if (state.selectedEl) update({ selectedEl: null })
    else if (state.measures.length) update({ measures: [] })
    else if (state.collisionItems.length) update({ collisionItems: [], highlightEl: null })
    else if (state.highlightEl) update({ highlightEl: null })
  }
}

const onMode = (e: Event): void => {
  const detail = (e as CustomEvent<{ mode?: DebugMode }>).detail
  setMode(detail?.mode ?? 'off')
}

/** 兼容既有事件:MainLayout toggleDebug 广播 on/off */
const onDebugChanged = (e: Event): void => {
  const on = (e as CustomEvent<{ on?: boolean }>).detail?.on
  setMode(on ? 'visual' : 'off')
}

const attach = (): void => {
  document.addEventListener('pointermove', onPointerMove, { passive: true })
  document.addEventListener('pointerleave', onPointerLeave)
  document.addEventListener('click', onClick)
  window.addEventListener('keydown', onKeyDown)
  window.addEventListener('sr-debug-mode', onMode)
  window.addEventListener('sr-debug-changed', onDebugChanged)
}

const detach = (): void => {
  document.removeEventListener('pointermove', onPointerMove)
  document.removeEventListener('pointerleave', onPointerLeave)
  document.removeEventListener('click', onClick)
  window.removeEventListener('keydown', onKeyDown)
  window.removeEventListener('sr-debug-mode', onMode)
  window.removeEventListener('sr-debug-changed', onDebugChanged)
  if (rafId != null) { cancelAnimationFrame(rafId); rafId = null }
  lastPointer = null
}

let refCount = 0

/* -------------------- describe 反查(UI 侧实现,不进 core) -------------------- */
/**
 * 由 debugCore.describe() 输出的选择器描述反查 DOM 元素。
 * describe 只保留 tag + 首个 class + id(60 字符截断),故做尽力匹配:
 * 1) id 优先 document.getElementById(带 tag/class 校验,id 含特殊字符也安全)
 * 2) class 其次 querySelector(tag 前缀可选,class 做 CSS 转义)
 * 3) 仅 tag 兜底
 * 不进 core:core 的 DebugReport 条目只含描述字符串、保持无 DOM 反查职责,
 * UI 侧"点击条目高亮元素"的定位需求在覆盖层实现。
 */
const escapeSel = (s: string): string => s.replace(/[^a-zA-Z0-9_-]/g, '\\$&')

export function findElByDescribe(desc: string): Element | null {
  if (typeof document === 'undefined') return null
  const clean = desc.replace(/…$/, '')
  const m = /^([a-zA-Z][\w-]*)?(?:\.([^.#]+))?(?:#([^.\s]+))?/.exec(clean)
  if (!m) return null
  const [, tag, cls, id] = m
  const matchTag = (el: Element): boolean => !tag || el.tagName.toLowerCase() === tag.toLowerCase()
  if (id) {
    const el = document.getElementById(id)
    if (el && matchTag(el) && (!cls || el.classList.contains(cls))) return el
  }
  if (cls) {
    const el = document.querySelector(`${tag ?? ''}.${escapeSel(cls)}`)
    if (el && matchTag(el)) return el
  }
  if (tag) return document.querySelector(tag)
  return null
}

/* -------------------- 动作(模块级,引用稳定) -------------------- */

export const dbgActions = {
  /** 退出 visual 模式(面板关闭按钮用;mount 同步 'sr-debug-mode') */
  setMode,
  /** 清除全部瞬态(悬停/选中/测量/碰撞/高亮) */
  clearAll: (): void => {
    state = { ...initial(), mode: state.mode }
    emit()
  },
  setSelected: (el: Element | null): void => update({ selectedEl: el }),
  clearSelection: (): void => update({ selectedEl: null }),
  setMeasureMode: (on: boolean): void => update({ measureMode: on }),
  toggleMeasureMode: (): void => update({ measureMode: !state.measureMode }),
  addMeasurePoint,
  cancelPending: (): void => update({ pendingPoint: null }),
  removeMeasure: (index: number): void => update({ measures: state.measures.filter((_, i) => i !== index) }),
  clearMeasures: (): void => update({ measures: [] }),
  setCollisionItems: (items: CollisionItem[]): void => update({ collisionItems: items }),
  clearCollision: (): void => update({ collisionItems: [], highlightEl: null }),
  setHighlight: (el: Element | null): void => update({ highlightEl: el }),
}

/** 订阅单例 store;DebugOverlay / DebugPanel 各调一次即共享同一状态 */
const subscribe = (cb: () => void): (() => void) => {
  listeners.add(cb)
  return () => { listeners.delete(cb) }
}

export function useDebugOverlay(): { state: OverlayState; actions: typeof dbgActions } {
  useEffect(() => {
    if (++refCount === 1) attach()
    return () => {
      if (--refCount === 0) detach()
    }
  }, [])
  const snapshot = useSyncExternalStore(subscribe, () => state)
  return { state: snapshot, actions: dbgActions }
}
