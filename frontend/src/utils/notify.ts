/**
 * Web 通知纯逻辑与持久化水位(供 hooks/useWebNotifications.ts 与设置页复用):
 * - 文本组装:标题「关注提醒」+ 正文「N 只股票新触发信号:code1, code2…」(最多列 5 只,超出 +N);
 * - 重复抑制:已通知水位(事件 id,localStorage)与已读水位(useWatchlistAlerts 的 sr-wl-seen-id)解耦——
 *   同一批未读事件只通知一次,不随 30s 轮询重复打扰;用户未读的仍保持站内浮窗角标。
 * 纯函数无浏览器依赖(localStorage 已做 typeof 保护),可直接被 node 直测。
 */
export const NOTIFICATION_TITLE = '关注提醒'
/** 系统通知持久化开关键(usePersistentState) */
export const NOTIFY_ENABLE_KEY = 'sr-notify-enabled'
/** 已通知水位键(事件 id,localStorage):与已读水位解耦,记录「已通过系统通知提示过」的最大事件 id */
export const NOTIFY_SENT_KEY = 'sr-notify-sent-id'
/** 单条通知最多展示的股票数,超出部分以 +N 汇总 */
export const MAX_NOTIFY_CODES = 5

/** 正文组装:「N 只股票新触发信号:code1, code2…」;超出 maxCodes 只列前 N 只并追加 +N */
export function buildNotificationBody(codes: readonly string[], maxCodes: number = MAX_NOTIFY_CODES): string {
  const shown = codes.slice(0, maxCodes).join(', ')
  const rest = codes.length > maxCodes ? ` +${codes.length - maxCodes}` : ''
  return `${codes.length} 只股票新触发信号:${shown}${rest}`
}

/** 重复抑制判定:只保留事件 id 超过已通知水位的事件(水位内 = 已提示过,不重复通知) */
export function filterUnnotified<T extends { maxId: number }>(items: readonly T[], sentId: number): T[] {
  return items.filter((i) => i.maxId > sentId)
}

/** 读取已通知水位;无记录/损坏 → null(视为从未通知) */
export function readNotifySentId(): number | null {
  try {
    const raw = typeof localStorage === 'undefined' ? null : localStorage.getItem(NOTIFY_SENT_KEY)
    if (raw === null) return null
    const n = Number(raw)
    return Number.isFinite(n) && n >= 0 ? Math.floor(n) : null
  } catch {
    return null
  }
}

/** 推进已通知水位 */
export function writeNotifySentId(id: number) {
  try {
    if (typeof localStorage === 'undefined') return
    localStorage.setItem(NOTIFY_SENT_KEY, String(id))
  } catch {
    /* ignore quota / unavailable storage */
  }
}
