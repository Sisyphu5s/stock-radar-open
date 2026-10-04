import { lazy, Suspense } from 'react'
import Launcher from './copilot/Launcher'
import { useCopilotPlacement } from './copilot/useCopilotPlacement'

// T-126:浮窗按需懒加载——CopilotWindow 静态导入链含 antd(ConfigProvider,经 @ant-design/x peer
// 解析),改为 lazy 后干净安装/异常安装下不阻塞任意路由的首屏渲染(打开助手时才加载)
const CopilotWindow = lazy(() => import('./copilot/CopilotWindow'))

/**
 * AI 助手入口：可拖动停靠 launcher + 可移动停靠浮窗（非模态，无遮罩 / 无 Drawer DOM）。
 * 面板打开状态由 copilotStore.open 单一管理；会话持久化到 localStorage，关闭/重开不丢。
 * 位置按归一化坐标持久化（sr-copilot-placement-v1），resize/visualViewport 后自动钳制。
 */
export default function HermesWidget() {
  const p = useCopilotPlacement()
  return (
    <>
      <Launcher
        launcherRef={p.launcherRef}
        style={p.launcherStyle}
        onPointerDown={p.launcherPointerDown}
        suppressClickRef={p.suppressClickRef}
        dragging={p.launcherDragging}
        hidden={p.open}
        windowId={p.windowId}
      />
      <Suspense fallback={null}>
        <CopilotWindow
          windowId={p.windowId}
          launcherRef={p.launcherRef}
          winRef={p.winRef}
          winRect={p.winRect}
          winDragging={p.winDragging}
          isMobile={p.isMobile}
          onPointerDown={p.windowPointerDown}
          dockTo={p.dockTo}
        />
      </Suspense>
    </>
  )
}
