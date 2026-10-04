const BRAND_BLUE = 'var(--sr-accent)'

/** 品牌标识：扁平单色（无渐变），暗色侧栏/亮色场景通用 */
export default function BrandLogo({ size = 28, collapsed = false }: { size?: number; collapsed?: boolean }) {
  const icon = (
    <svg width={size} height={size} viewBox="0 0 32 32" style={{ flexShrink: 0, display: 'block' }} aria-hidden>
      <rect x="1" y="1" width="30" height="30" rx="6" fill={BRAND_BLUE} />
      <circle cx="16" cy="16" r="10.5" fill="none" stroke="rgba(255,255,255,0.38)" strokeWidth="1.3" strokeDasharray="24 13" strokeLinecap="round" />
      <polyline points="8,21 13,15.5 18,17.5 24,10.5" fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="24" cy="10.5" r="4.2" fill="rgba(255,255,255,0.3)" />
      <circle cx="24" cy="10.5" r="2.1" fill="#fff" />
    </svg>
  )
  if (collapsed) return icon
  return (
    <div className="sr-logo" style={{ display: 'flex', alignItems: 'center', gap: 9, overflow: 'hidden' }}>
      {icon}
      {/* 文字颜色走 .sr-logo-title/-sub 类（index.css）：亮色侧栏 --sr-text-1/2，暗色侧栏白字 */}
      <div className="sr-logo-text" style={{ whiteSpace: 'nowrap' }}>
        <div className="sr-logo-title" style={{ fontWeight: 700, fontSize: 16, letterSpacing: 0.5, lineHeight: 1.2 }}>Stock Radar</div>
        <div className="sr-logo-sub" style={{ fontSize: 9, letterSpacing: 1, marginTop: 1 }}>MARKET · ALPHA</div>
      </div>
    </div>
  )
}
