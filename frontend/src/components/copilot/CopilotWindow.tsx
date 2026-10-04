import { Button, Menu, Tooltip } from '@mantine/core'
import { openConfirmModal } from '@mantine/modals'
import { notifications } from '@mantine/notifications'
import {
  IconArrowDownToArc, IconColumns, IconEraser, IconGripVertical, IconMaximize, IconPlus, IconX,
} from '@tabler/icons-react'
import { useEffect, useRef } from 'react'
import type { PointerEvent as RPointerEvent, RefObject } from 'react'
import { useLocation } from 'react-router-dom'
import { useCopilotStore, MAX_SESSIONS } from '../../stores/copilotStore'
import { abortActiveStream, ChatPanel } from './ChatPanel'
import { isMarketPath } from '../../utils/routeTitles'
import type { DockAction } from './copilotGeometry'
import './copilot.css'

export interface CopilotWindowProps {
  windowId: string
  launcherRef: RefObject<HTMLButtonElement | null>
  winRef: RefObject<HTMLDivElement | null>
  winRect: { x: number; y: number }
  winDragging: boolean
  isMobile: boolean
  onPointerDown: (e: RPointerEvent<HTMLDivElement>) => void
  dockTo: (action: DockAction) => void
}

/**
 * 可移动停靠浮窗（替代 Drawer）：桌面固定 400px 宽、可用高度内，标题栏可拖拽；
 * 移动端（<768）为底部 sheet，位于底部导航上方，矮视口全屏。
 * 非模态 role=dialog + aria-modal=false，无遮罩、无 Drawer DOM。
 */
export default function CopilotWindow(props: CopilotWindowProps) {
  const { windowId, launcherRef, winRef, winRect, winDragging, isMobile, onPointerDown, dockTo } = props
  const open = useCopilotStore((s) => s.open)
  const closePanel = useCopilotStore((s) => s.closePanel)
  const sessions = useCopilotStore((s) => s.sessions)
  const activeId = useCopilotStore((s) => s.activeId)
  const newSession = useCopilotStore((s) => s.newSession)
  const clearSession = useCopilotStore((s) => s.clearSession)
  const location = useLocation()
  const dialogRef = useRef<HTMLDivElement | null>(null)

  const routeMode: 'market' | 'research' = isMarketPath(location.pathname) ? 'market' : 'research'
  const active = sessions.find((s) => s.id === activeId) ?? null
  const canClear = !!active && active.messages.length > 0

  const handleClear = () => {
    if (activeId) {
      clearSession(activeId)
      notifications.show({ message: '会话已清空', color: 'teal' })
    }
  }

  // 打开时聚焦面板；关闭后焦点还给 launcher
  const prevOpen = useRef(open)
  useEffect(() => {
    if (open) {
      dialogRef.current?.focus({ preventScroll: true })
    } else if (prevOpen.current) {
      requestAnimationFrame(() => launcherRef.current?.focus({ preventScroll: true }))
    }
    prevOpen.current = open
  }, [open, launcherRef])

  // 开/关窗边界中止活动流：关窗后旧流不得在后台继续写 store / 执行动作；重开前清残留流防双流交替写
  useEffect(() => {
    abortActiveStream()
  }, [open])

  // 非模态：Escape 关闭
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closePanel()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, closePanel])

  if (!open) return null

  const confirmClear = () => {
    openConfirmModal({
      title: '清空当前会话消息？',
      labels: { confirm: '清空', cancel: '取消' },
      onConfirm: handleClear,
    })
  }

  const className = 'sr-cop-window'
    + (isMobile ? ' sr-cop-mobile' : '')
    + (!isMobile && !winDragging ? ' sr-cop-window-idle' : '')

  return (
    <div
      id={windowId}
      ref={(el) => { winRef.current = el; dialogRef.current = el }}
      role="dialog"
      aria-modal={false}
      aria-label="AI 助手"
      tabIndex={-1}
      className={className}
      style={isMobile ? undefined : { left: winRect.x, top: winRect.y }}
    >
      <div className="sr-cop-head sr-cop-window-head" onPointerDown={isMobile ? undefined : onPointerDown}>
        <span className="sr-cop-window-title"><IconGripVertical size={14} aria-hidden /><span>AI 助手</span></span>
        <div className="sr-cop-head-actions" onPointerDown={(e) => e.stopPropagation()}>
          {!isMobile && (
            <Menu shadow="md" width={132} position="bottom-end">
              <Menu.Target>
                <Button size="compact-xs" variant="subtle" aria-label="停靠位置">
                  <IconColumns size={14} aria-hidden />
                </Button>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Item leftSection={<IconColumns size={14} aria-hidden />} onClick={() => dockTo('left')}>停靠左侧</Menu.Item>
                <Menu.Item leftSection={<IconColumns size={14} aria-hidden />} onClick={() => dockTo('right')}>停靠右侧</Menu.Item>
                <Menu.Item leftSection={<IconArrowDownToArc size={14} aria-hidden />} onClick={() => dockTo('bottom')}>停靠底部</Menu.Item>
                <Menu.Divider />
                <Menu.Item leftSection={<IconMaximize size={14} aria-hidden />} onClick={() => dockTo('free')}>自由位置</Menu.Item>
              </Menu.Dropdown>
            </Menu>
          )}
          <Button
            size="compact-xs" variant="subtle" aria-label="清空会话" disabled={!canClear}
            onClick={canClear ? confirmClear : undefined}
          >
            <IconEraser size={14} aria-hidden />
          </Button>
          <Tooltip label="新建会话">
            <Button
              size="compact-xs" variant="subtle" aria-label="新建会话"
              onClick={() => {
                if (sessions.length >= MAX_SESSIONS) {
                  notifications.show({ message: `会话数已达上限 ${MAX_SESSIONS}，最旧会话将被清理`, color: 'yellow' })
                }
                newSession(routeMode)
              }}
            >
              <IconPlus size={14} aria-hidden />
            </Button>
          </Tooltip>
          <Tooltip label="关闭">
            <Button size="compact-xs" variant="subtle" aria-label="关闭" onClick={closePanel}>
              <IconX size={14} aria-hidden />
            </Button>
          </Tooltip>
        </div>
      </div>
      <div className="sr-cop">
        <ChatPanel />
      </div>
    </div>
  )
}
