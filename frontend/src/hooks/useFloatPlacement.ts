import type { PointerEvent as RPointerEvent, RefObject, MutableRefObject } from 'react'
import { useFloatWindow } from './useFloatWindow'
import type { LauncherPlacement } from './useFloatGeometry'
import { readCssVar } from './useFloatGeometry'

/* ============================================================
   关注提醒浮窗位置 hook:基于 useFloatWindow 的参数化实例(06 §5 统一抽象)。
   - 位置归一化 [0,1](视口可用范围)持久化,key sr-wl-float-pos-v1;
     launcher 独立 key sr-wl-launcher-pos-v1
   - 尺寸固定策略(clamp + 固定,与 copilot 窗口对齐):展开面板宽高由 CSS
     min(400px, 100vw-32) / min(600px, 100dvh-40) 决定,几何仅用固定尺寸做位置钳制;
     悬浮小窗(chip)宽高内容自适应,CSS .sr-wl-float-collapsed{width:auto}
   - 拖动只提交 x/y,不提交尺寸(chip 态拖拽不会污染展开面板尺寸)
   - 无停靠菜单(dockable:false):位置模型恒 free
   - 移动端(<768)enabled=false,rect 恒为 null,不参与几何、不持久化
   ============================================================ */

export interface FloatRect { x: number; y: number; w: number; h: number }

export interface FloatPlacementApi {
  enabled: boolean
  floatRef: RefObject<HTMLDivElement | null>
  rect: FloatRect | null
  dragging: boolean
  resizing: boolean
  headPointerDown: (e: RPointerEvent<HTMLDivElement>) => void
  resizePointerDown: (e: RPointerEvent<HTMLDivElement>) => void
  edgeRightPointerDown: (e: RPointerEvent<HTMLDivElement>) => void
  edgeBottomPointerDown: (e: RPointerEvent<HTMLDivElement>) => void
  launcherRef: RefObject<HTMLButtonElement | null>
  launcherStyle: { left: number; top: number } | null
  /** launcher 中心点像素坐标；launcherStyle 非 null（已定位）时返回，否则 null（消费方回退 CSS 默认锚点） */
  launcherCenter: { x: number; y: number } | null
  launcherPointerDown: (e: RPointerEvent<HTMLButtonElement>) => void
  launcherDragging: boolean
  suppressClickRef: MutableRefObject<boolean>
}

/** launcher 持久化结构：吸附侧 + 归一化 y [0,1]（同 copilot LauncherPlacement） */
export type { LauncherPlacement }

/** 读取 CSS 变量(单一事实源见 useFloatGeometry；保持本模块历史导出面，消费方零改动) */
export { readCssVar }

/** 展开面板固定宽度(px)：与 copilot 窗口一致，CSS width: min(400px, 100vw-32px) 需同步 */
const FIXED_W = 400
/** 展开面板高度上限(px)：视口高 - 上下留白(copilot WINDOW_V_MARGIN 同值)，CSS max-height 需同步 */
const FIXED_MAX_H = 600
const FIXED_V_MARGIN = 40

export default function useFloatPlacement(): FloatPlacementApi {
  const fw = useFloatWindow({
    storageKey: 'sr-wl-float-pos-v1',
    launcherStorageKey: 'sr-wl-launcher-pos-v1',
    width: FIXED_W,
    maxHeight: FIXED_MAX_H,
    vMargin: FIXED_V_MARGIN,
    mobile: 'sheet',
    dockable: false,
  })

  /** 尺寸固定（clamp + 固定，与 copilot 对齐）：缩放/边缘拖拽停用，保留 API 为 no-op（调用方零改动） */
  const noop = (_e: RPointerEvent<HTMLDivElement>) => { /* 固定尺寸：缩放已停用 */ }

  // rect 补固定尺寸（chip 态拖拽只提交 x/y，不污染展开面板尺寸）
  const rect: FloatRect | null = fw.winRect ? { ...fw.winRect, w: FIXED_W, h: Math.min(FIXED_MAX_H, window.innerHeight - FIXED_V_MARGIN) } : null

  return {
    enabled: fw.enabled,
    floatRef: fw.winRef,
    rect,
    dragging: fw.winDragging,
    resizing: false,
    headPointerDown: fw.windowPointerDown,
    resizePointerDown: noop,
    edgeRightPointerDown: noop,
    edgeBottomPointerDown: noop,
    launcherRef: fw.launcherRef,
    launcherStyle: fw.launcherStyle,
    launcherCenter: fw.launcherCenter,
    launcherPointerDown: fw.launcherPointerDown,
    launcherDragging: fw.launcherDragging,
    suppressClickRef: fw.suppressClickRef,
  }
}
