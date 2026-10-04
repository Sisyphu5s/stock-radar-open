import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { usePersistentState } from '../utils/stateMemory'
import {
  buildNotificationBody,
  filterUnnotified,
  MAX_NOTIFY_CODES,
  NOTIFICATION_TITLE,
  NOTIFY_ENABLE_KEY,
  readNotifySentId,
  writeNotifySentId,
} from '../utils/notify'

/** 通知权限状态:unsupported = Notification API 不可用(非 https/localhost 或浏览器不支持) */
export type NotifyPermission = NotificationPermission | 'unsupported'

/** 触发通知所需的最小事件信息(WatchAlertItem 结构兼容:code/maxId 均存在) */
export interface NotifyItem {
  code: string
  maxId: number
}

export interface WebNotificationsApi {
  /** Notification API 是否可用(https/localhost + 浏览器支持) */
  supported: boolean
  permission: NotifyPermission
  /** 持久化开关(sr-notify-enabled) */
  enabled: boolean
  setEnabled: (v: boolean) => void
  /** 权限引导:弹浏览器授权窗;返回最终权限(denied 由设置页展示浏览器设置开启说明) */
  requestPermission: () => Promise<NotifyPermission>
  /** 无条件发一条批量系统通知并推进已通知水位 */
  notifyNewSignals: (items: NotifyItem[]) => void
  /** 条件触发:enabled && granted && 页面不可见,且事件超过已通知水位(重复抑制) */
  maybeNotifyNewSignals: (items: NotifyItem[]) => void
}

function getSupported(): boolean {
  return typeof window !== 'undefined' && 'Notification' in window
}

function readPermission(): NotifyPermission {
  if (!getSupported()) return 'unsupported'
  return Notification.permission
}

/** Web/桌面通知服务:权限引导 + 持久化开关 + 批量新信号系统通知(与站内浮窗互补)。
 *  通知触发条件收敛在 maybeNotifyNewSignals(供 useWatchlistAlerts 集成):
 *  - 仅页面不可见(document.hidden)时走系统通知;页面可见时站内浮窗已承担提醒,不重复打扰;
 *  - 重复抑制:已通知水位(事件 id)与已读水位解耦——同一批未读事件只通知一次,
 *    已通知未读的仍保持站内浮窗角标,不因轮询多轮新增而反复弹系统通知。
 *  点击通知:聚焦窗口 + 跳信号中心(/signals)。 */
export default function useWebNotifications(): WebNotificationsApi {
  const navigate = useNavigate()
  const [enabled, setEnabled] = usePersistentState<boolean>(NOTIFY_ENABLE_KEY, false)
  const [permission, setPermission] = useState<NotifyPermission>(readPermission)

  // 跨标签页 / 浏览器设置变更后重新同步权限(权限为站点级,浏览器自动广播;回到本页时刷新)
  useEffect(() => {
    const sync = () => setPermission(readPermission())
    document.addEventListener('visibilitychange', sync)
    window.addEventListener('focus', sync)
    return () => {
      document.removeEventListener('visibilitychange', sync)
      window.removeEventListener('focus', sync)
    }
  }, [])

  const requestPermission = useCallback(async (): Promise<NotifyPermission> => {
    if (!getSupported()) return 'unsupported'
    try {
      const next = await Notification.requestPermission()
      setPermission(next)
      return next
    } catch {
      return readPermission()
    }
  }, [])

  const notifyNewSignals = useCallback((items: NotifyItem[]) => {
    if (items.length === 0 || !getSupported() || Notification.permission !== 'granted') return
    const codes = items.map((i) => i.code)
    try {
      const n = new Notification(NOTIFICATION_TITLE, {
        body: buildNotificationBody(codes, MAX_NOTIFY_CODES),
        // 同 tag 通知互相替换,多批新信号不堆积成通知墙
        tag: 'sr-watchlist-alert',
        icon: '/favicon.svg',
      })
      n.onclick = () => {
        // 点击通知:聚焦窗口 + 跳信号中心
        window.focus()
        navigate('/signals')
        n.close()
      }
      // 通知发出后才推进已通知水位(重复抑制)
      const maxId = Math.max(...items.map((i) => i.maxId))
      const cur = readNotifySentId() ?? 0
      if (maxId > cur) writeNotifySentId(maxId)
    } catch {
      /* 浏览器拒绝构造/权限被撤销等:静默,不打扰 */
    }
  }, [navigate])

  const maybeNotifyNewSignals = useCallback((items: NotifyItem[]) => {
    if (!enabled) return
    if (!getSupported() || Notification.permission !== 'granted') return
    // 页面可见时站内浮窗已提醒,系统通知不重复打扰
    if (!document.hidden) return
    const sentId = readNotifySentId() ?? 0
    const fresh = filterUnnotified(items, sentId)
    if (fresh.length === 0) return
    notifyNewSignals(fresh)
  }, [enabled, notifyNewSignals])

  return {
    supported: getSupported(),
    permission,
    enabled,
    setEnabled,
    requestPermission,
    notifyNewSignals,
    maybeNotifyNewSignals,
  }
}
