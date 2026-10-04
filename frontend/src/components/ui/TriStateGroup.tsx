import { Tooltip } from '@mantine/core'

export interface TriStateOption {
  v: 1 | 0 | -1
  icon: string
  tip: string
  color: string
}

interface TriStateGroupProps {
  value: number
  onChange: (v: 1 | 0 | -1) => void
  options: TriStateOption[]
  className?: string
}

/**
 * 三态按钮组基板:✓ / – / × 型方向控制(只看 / 关闭 / 排除)。
 * 自绘保留(Mantine Chip 不支持逐项自定义激活色,无等价),样式经 Styles API 自绘;
 * tooltip 由 antd Tooltip 换为 Mantine Tooltip。
 */
export default function TriStateGroup({ value, onChange, options, className }: TriStateGroupProps) {
  return (
    // flexShrink:0:按钮组整体不可被父级 flex 压扁(与排除弹层行1 原内联按钮行为一致)
    <span className={'sr-tristate' + (className ? ` ${className}` : '')} style={{ flexShrink: 0 }}>
      {options.map((b) => {
        const active = value === b.v
        return (
          <Tooltip key={b.v} label={b.tip}>
            <button
              type="button"
              aria-label={b.tip}
              aria-pressed={active}
              onClick={() => onChange(b.v)}
              style={{
                // 触控目标(P1-a11y-5):桌面 28px(≥28px 达标线),窄屏经 clamp(6vw) 放大至最高 32px(lineHeight 同步跟随)
                width: 'clamp(28px, 6vw, 32px)',
                height: 'clamp(28px, 6vw, 32px)',
                padding: 0, margin: 0,
                lineHeight: 'calc(clamp(28px, 6vw, 32px) - 2px)', textAlign: 'center',
                fontSize: 'var(--sr-font-page)', fontWeight: 700, borderRadius: 'var(--sr-radius-ctl)',
                cursor: 'pointer', fontFamily: 'inherit',
                color: active ? b.color : 'var(--sr-text-3)',
                background: active
                  ? `color-mix(in srgb, ${b.color} 16%, transparent)`
                  : 'transparent',
                border: active ? `1.5px solid ${b.color}` : '1px solid var(--sr-border)',
                transition: 'all .15s ease',
              }}
            >
              {b.icon}
            </button>
          </Tooltip>
        )
      })}
    </span>
  )
}
