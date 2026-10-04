import { IconRobot } from '@tabler/icons-react'
import type { MutableRefObject, PointerEvent as RPointerEvent, RefObject } from 'react'
import { useCopilotStore } from '../../stores/copilotStore'
import './copilot.css'

export interface LauncherProps {
  launcherRef: RefObject<HTMLButtonElement | null>
  /** null = 从未拖过，用 CSS 默认定位（右侧、底部导航上方） */
  style: { left: number; top: number } | null
  onPointerDown: (e: RPointerEvent<HTMLButtonElement>) => void
  /** 拖动结束置 true 抑制紧随的 click，防止拖动误触发打开 */
  suppressClickRef: MutableRefObject<boolean>
  dragging: boolean
  /** 面板打开时隐藏，避免遮挡浮窗 */
  hidden: boolean
  windowId: string
}

/**
 * 可拖动 launcher：原生 button，pointer 位移 >5px 视为拖动（不触发打开），
 * 释放吸附左/右边缘；未拖动点击展开浮动浮窗。aria-expanded / aria-controls 关联 dialog。
 */
export default function Launcher(props: LauncherProps) {
  const { launcherRef, style, onPointerDown, suppressClickRef, dragging, hidden, windowId } = props
  const open = useCopilotStore((s) => s.open)
  const openPanel = useCopilotStore((s) => s.openPanel)

  const handleClick = () => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false
      return
    }
    openPanel()
  }

  return (
    <button
      ref={launcherRef}
      type="button"
      className={'sr-cop-launcher'
        + (hidden ? ' sr-cop-launcher-hidden' : '')
        + (dragging ? ' sr-cop-launcher-dragging' : '')}
      style={style ? { ...style, right: 'auto', bottom: 'auto' } : undefined}
      aria-label="AI 助手"
      aria-haspopup="dialog"
      aria-expanded={open}
      aria-controls={windowId}
      onPointerDown={onPointerDown}
      onClick={handleClick}
    >
      <IconRobot size={16} aria-hidden />
    </button>
  )
}
