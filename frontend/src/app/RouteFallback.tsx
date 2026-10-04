/** 路由级 lazy 加载时的占位（避免整页跳动，纯 CSS 实现） */
export default function RouteFallback() {
  return (
    <div className="sr-route-fallback" role="status" aria-label="页面加载中">
      <span className="sr-route-spinner" />
    </div>
  )
}
