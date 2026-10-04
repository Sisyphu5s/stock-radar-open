import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import useFloatPlacement, { readCssVar } from '../../hooks/useFloatPlacement'
import { LAUNCHER_SIZE } from '../../hooks/useFloatGeometry'
import useWatchlistAlerts from '../../hooks/useWatchlistAlerts'
import WatchlistFloat from './WatchlistFloat'
import WatchlistLauncher from './WatchlistLauncher'

/**
 * 关注提醒全局组件（挂 MainLayout，全站可见）：
 * - 数据 hook（useWatchlistAlerts）与几何 hook（useFloatPlacement）在此单例调用，
 *   避免 Launcher 与 Float 各自实例化导致双轮询/双监听；
 * - launcher 为常驻入口（可拖动，未读徽标实时同步）；点击开/关浮窗；
 * - 显隐状态机：hidden（仅 launcher）→ open（挂载 Float，entering=true 由 Float 内部
 *   useLayoutEffect+rAF 消化首帧）→ closing（leaving=true 播放反向动画）→ onLeaveEnd → hidden；
 * - 新未读到达（0 → >0）自动弹出覆盖关闭态；
 * - launcher 在 Float 挂载期间隐藏（sr-wl-launcher-hidden），hidden 态恢复显示；
 * - morphOrigin = launcher 中心（fp.launcherCenter ?? CSS 默认锚点），驱动形态互变动画起点；
 * - 无关注时不渲染任何内容。
 */
export default function WatchlistWidget() {
  const alerts = useWatchlistAlerts()
  const fp = useFloatPlacement()
  /** 显隐状态机：phase='open' 时 Float 挂载；leaving=true 播放离场动画（Float 仍挂载，等待 onLeaveEnd） */
  const [phase, setPhase] = useState<'hidden' | 'open'>('hidden')
  const [leaving, setLeaving] = useState(false)

  /** 自动弹出频率限制：30s 内不重复弹出（P1-57），防止轮询抖动反复打扰 */
  const AUTO_POPUP_COOLDOWN_MS = 30_000

  // 新未读到达 → 自动弹出（0 → >0 时打开；若正关闭则取消关闭），保留提醒价值；
  // 30s 冷却：连续两轮增量事件(间隔 30s 轮询)只在首轮弹一次
  const prevUnreadRef = useRef(0)
  const lastAutoPopupRef = useRef(0)
  useEffect(() => {
    const n = alerts.unreadCount
    const now = Date.now()
    if (n > 0 && prevUnreadRef.current === 0 && now - lastAutoPopupRef.current >= AUTO_POPUP_COOLDOWN_MS) {
      lastAutoPopupRef.current = now
      setPhase('open')
      setLeaving(false)
    }
    prevUnreadRef.current = n
  }, [alerts.unreadCount])

  // 离场动画结束（Float 回调 onLeaveEnd）→ 回 hidden，launcher 浮现
  const handleLeaveEnd = useCallback(() => {
    setLeaving(false)
    setPhase('hidden')
  }, [])

  // morph 起点 = launcher 中心；launcherCenter 为 null（未拖过/移动端）→ CSS 默认锚点
  // （right 16px、bottom 16+底部导航+安全区+48px，取按钮中心 LAUNCHER_SIZE/2）
  // T-63：CSS 默认锚点依赖 window.innerWidth/Height，补 window resize 监听重算，
  // 否则窗口缩放后 morphOrigin 过期（morph 动画起点错位）；同 MultiSelectGrid/DebugOverlay 既有模式
  const [winSize, setWinSize] = useState(() => ({ w: window.innerWidth, h: window.innerHeight }))
  useEffect(() => {
    const onResize = () => setWinSize({ w: window.innerWidth, h: window.innerHeight })
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  const morphOrigin = useMemo<{ x: number; y: number }>(
    () => fp.launcherCenter ?? {
      x: winSize.w - readCssVar('--sr-float-edge', 16) - LAUNCHER_SIZE / 2,
      y: winSize.h - (readCssVar('--sr-float-edge', 16) + readCssVar('--sr-bottombar-h', 0) + readCssVar('--sr-safe-bottom', 0) + readCssVar('--sr-float-wl-launcher-offset', 48)) - LAUNCHER_SIZE / 2,
    },
    [fp.launcherCenter, winSize],
  )

  // 无关注 → 不渲染（launcher 也无意义）
  if (alerts.watchlistData.length === 0) return null

  const open = phase === 'open'
  // launcher 隐藏：Float 挂载期间（open 或离场动画中）不可见、不可点，避免遮挡
  const launcherHidden = open || leaving

  // launcher 点击开/关：hidden → 打开（挂载 Float，entering 恒 true 由 Float 内部消化）；
  // open → 进入 closing（leaving=true，等待 onLeaveEnd 回 hidden）
  const handleToggle = () => {
    if (open) setLeaving(true)
    else setPhase('open')
  }

  return (
    <>
      <WatchlistLauncher
        launcherRef={fp.launcherRef}
        launcherStyle={fp.launcherStyle}
        launcherPointerDown={fp.launcherPointerDown}
        launcherDragging={fp.launcherDragging}
        suppressClickRef={fp.suppressClickRef}
        open={open}
        hidden={launcherHidden}
        unreadCount={alerts.unreadCount}
        onClick={handleToggle}
      />
      {open && (
        <WatchlistFloat
          enabled={fp.enabled}
          floatRef={fp.floatRef}
          rect={fp.rect}
          dragging={fp.dragging}
          resizing={fp.resizing}
          headPointerDown={fp.headPointerDown}
          resizePointerDown={fp.resizePointerDown}
          edgeRightPointerDown={fp.edgeRightPointerDown}
          edgeBottomPointerDown={fp.edgeBottomPointerDown}
          alerts={alerts.alerts}
          unreadCount={alerts.unreadCount}
          watchlistData={alerts.watchlistData}
          markAllSeen={alerts.markAllSeen}
          toggleWatch={alerts.toggleWatch}
          labelOf={alerts.labelOf}
          morphOrigin={morphOrigin}
          entering
          leaving={leaving}
          onLeaveEnd={handleLeaveEnd}
          onClose={() => setLeaving(true)}
        />
      )}
    </>
  )
}
