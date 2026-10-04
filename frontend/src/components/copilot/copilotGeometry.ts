/* ============================================================
   AI 助手可移动停靠浮窗:几何 / 吸附 / 持久化(历史导出面,消费方零改动)。
   - 06 §5 统一后:位置模型 WindowPlacement / 纯函数 / 持久化读写已提升至
     hooks/useFloatWindow.ts(通用抽象),本模块只保留 copilot 专属常量与
     re-export 桥,useCopilotPlacement 以 useFloatWindow 参数化实例消费。
   ============================================================ */

import {
  EDGE_GAP, DRAG_THRESHOLD, LAUNCHER_SIZE, SNAP_DISTANCE,
  clamp01, clampRange, clampLauncherRect, clampRect, freeRange,
  launcherBottomInset, launcherFromDrop, launcherRect, normToPx, pxToNorm, readCssVar,
} from '../../hooks/useFloatGeometry'
import type { LauncherPlacement } from '../../hooks/useFloatGeometry'
import { useFloatWindow } from '../../hooks/useFloatWindow'
import type {
  DockAction, DockEdge, FloatWindowConfig, WindowPlacement,
} from '../../hooks/useFloatWindow'

// ===== 通用常量/函数 re-export(调用方零改动;定义见 hooks/useFloatGeometry.ts) =====
export {
  EDGE_GAP, DRAG_THRESHOLD, LAUNCHER_SIZE, SNAP_DISTANCE,
  clamp01, clampRange, clampLauncherRect, clampRect, freeRange, launcherBottomInset,
  launcherFromDrop, launcherRect, normToPx, pxToNorm, readCssVar,
}
export type { LauncherPlacement, LauncherSide, Rect } from '../../hooks/useFloatGeometry'

// ===== 统一抽象 re-export(定义见 hooks/useFloatWindow.ts;类型与 hook 消费方零改动) =====
export { useFloatWindow }
export type { DockAction, DockEdge, FloatWindowConfig, WindowPlacement }

export const PLACEMENT_KEY = 'sr-copilot-placement-v1'
export const WINDOW_ID = 'sr-copilot-window'

export const WINDOW_WIDTH = 400
export const WINDOW_MAX_HEIGHT = 600
/** 桌面窗口上下留白(CSS .sr-cop-window height: min(600px, calc(100dvh - 40px)) 需与此一致) */
export const WINDOW_V_MARGIN = 40

/** 底部停靠避让量(px):底部停靠时窗口底边高于视口底 EDGE_GAP + 本值,
 *  为右下角关注浮窗/launcher 默认区留出空间(对齐 --sr-float-bottom-clearance),防重叠遮挡 */
export function bottomDockClearance(): number {
  return readCssVar('--sr-float-bottom-clearance', 60)
}

/** 历史持久化结构(兼容旧存储;useFloatWindow 内部按 window/launcher 分别读写) */
export interface CopilotPlacement {
  v: 1
  /** null = 从未拖过,用 CSS 默认定位(右侧、底部导航上方) */
  launcher: LauncherPlacement | null
  window: WindowPlacement
}

/** copilot 专属 config(06 §5.2 参数化差异;useCopilotPlacement 内部消费) */
export const copilotFloatConfig: FloatWindowConfig = {
  storageKey: PLACEMENT_KEY,
  width: WINDOW_WIDTH,
  maxHeight: WINDOW_MAX_HEIGHT,
  vMargin: WINDOW_V_MARGIN,
  mobile: 'sheet',
  dockable: true,
  defaultPlacement: { mode: 'edge', edge: 'right', x: 0, y: 0.5 },
  bottomClearance: bottomDockClearance,
}
