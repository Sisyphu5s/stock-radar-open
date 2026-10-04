import { Indicator } from '@mantine/core'
import { IconBell } from '@tabler/icons-react'
import type { MutableRefObject, PointerEvent as RPointerEvent, RefObject } from 'react'
import './watchlist-launcher.css'

export interface WatchlistLauncherProps {
  launcherRef: RefObject<HTMLButtonElement | null>
  /** null = 未拖过，用 CSS 默认定位（右下、copilot launcher 上方 48px） */
  launcherStyle: { left: number; top: number } | null
  launcherPointerDown: (e: RPointerEvent<HTMLButtonElement>) => void
  launcherDragging: boolean
  /** 拖动结束置 true，onClick 检查后清掉，防拖动误触发开/关 */
  suppressClickRef: MutableRefObject<boolean>
  /** 浮窗当前是否显示（aria-expanded） */
  open: boolean
  /** 浮窗挂载/离场动画期间隐藏 launcher（visibility:hidden），避免重叠遮挡 */
  hidden?: boolean
  /** 未读数徽标 */
  unreadCount: number
  /** 点击开/关浮窗 */
  onClick: () => void
}

/**
 * 关注提醒常驻入口：可拖动铃铛按钮 + 未读徽标（Mantine Indicator）。
 * 纯展示层，不取数据；所有状态由父组件 WatchlistWidget 注入。
 * 未读数 > 0 时徽标显示（Indicator disabled=0 隐藏）；拖动结束置位的
 * suppressClickRef 抑制紧随的 click，防止拖动误触开/关浮窗。
 */
export default function WatchlistLauncher(props: WatchlistLauncherProps) {
  const {
    launcherRef,
    launcherStyle,
    launcherPointerDown,
    launcherDragging,
    suppressClickRef,
    open,
    hidden,
    unreadCount,
    onClick,
  } = props

  const handleClick = () => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false
      return
    }
    onClick()
  }

  return (
    <button
      ref={launcherRef}
      type="button"
      className={'sr-wl-launcher'
        + (launcherDragging ? ' sr-wl-launcher-dragging' : '')
        + (hidden ? ' sr-wl-launcher-hidden' : '')}
      style={launcherStyle ? { ...launcherStyle, right: 'auto', bottom: 'auto' } : undefined}
      aria-label="关注提醒"
      aria-haspopup="dialog"
      aria-expanded={open}
      aria-controls="sr-wl-float-window"
      onPointerDown={launcherPointerDown}
      onClick={handleClick}
    >
      {/* 未读徽标：offset≈原 antd Badge offset [4,-4]（右上向内），disabled 隐藏零值 */}
      <Indicator offset={4} size={16} color="red" disabled={unreadCount === 0} label={unreadCount > 99 ? '99+' : unreadCount}>
        <IconBell size={16} aria-hidden />
      </Indicator>
    </button>
  )
}
