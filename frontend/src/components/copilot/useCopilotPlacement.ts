import type { MutableRefObject, PointerEvent as RPointerEvent, RefObject } from 'react'
import { useCopilotStore } from '../../stores/copilotStore'
import { copilotFloatConfig, WINDOW_ID } from './copilotGeometry'
import type { CopilotPlacement } from './copilotGeometry'
import { useFloatWindow } from '../../hooks/useFloatWindow'
import type { DockAction } from '../../hooks/useFloatWindow'

export interface CopilotPlacementApi {
  windowId: string
  open: boolean
  isMobile: boolean
  launcherRef: RefObject<HTMLButtonElement | null>
  /** null = 未拖过，用 CSS 默认定位（右侧、底部导航上方） */
  launcherStyle: { left: number; top: number } | null
  launcherPointerDown: (e: RPointerEvent<HTMLButtonElement>) => void
  launcherDragging: boolean
  suppressClickRef: MutableRefObject<boolean>
  winRef: RefObject<HTMLDivElement | null>
  winRect: { x: number; y: number }
  winDragging: boolean
  windowPointerDown: (e: RPointerEvent<HTMLDivElement>) => void
  dockTo: (action: DockAction) => void
}

/**
 * 浮窗/launcher 位置管理：基于 useFloatWindow 的参数化实例(06 §5 统一抽象)。
 * 几何核心(视口跟踪/拖动会话/钳制/吸附)走 useFloatGeometry(经 useFloatWindow)，
 * copilot 专属差异(停靠菜单/底部避让/移动端 sheet)全部参数化进 copilotFloatConfig。
 * 对外 API 签名保留(消费方 HermesWidget/CopilotWindow 零改动)。
 * 桌面浮窗固定 400px 宽、可用高度内；移动端为底部 sheet（不参与 placement 几何）。
 */
export function useCopilotPlacement(): CopilotPlacementApi {
  const open = useCopilotStore((s) => s.open)
  const fw = useFloatWindow(copilotFloatConfig)

  return {
    windowId: WINDOW_ID,
    open,
    isMobile: fw.isMobile,
    launcherRef: fw.launcherRef,
    launcherStyle: fw.launcherStyle,
    launcherPointerDown: fw.launcherPointerDown,
    launcherDragging: fw.launcherDragging,
    suppressClickRef: fw.suppressClickRef,
    winRef: fw.winRef,
    // 移动端 sheet 不消费 winRect(组件 style 分支跳过)，null 回退 0,0 安全
    winRect: fw.winRect ?? { x: 0, y: 0 },
    winDragging: fw.winDragging,
    windowPointerDown: fw.windowPointerDown,
    dockTo: fw.dockTo ?? ((_: DockAction) => {}),
  }
}

/** 兼容旧类型导出(消费方若 import 类型保留) */
export type { CopilotPlacement }
