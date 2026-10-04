/**
 * visibility 守卫的 setInterval（全站 setInterval 轮询统一出口，一处实现处处复用）：
 * 页面隐藏（后台标签/最小化）时跳过回调，不空转、不发请求；恢复可见由下一 tick 续上。
 * 语义与 TanStack Query 的 refetchIntervalInBackground: false（data/queryBase.ts）一致。
 * 返回原生 timer handle，调用方沿用原 setInterval 的清理方式（clearInterval）与清理时机。
 * 纪律：新建轮询一律走本函数，禁止各页面各写一套 document.visibilityState 判断。
 */
export function guardedInterval(callback: () => void, ms: number): number {
  return window.setInterval(() => {
    if (document.visibilityState === 'hidden') return
    callback()
  }, ms)
}
