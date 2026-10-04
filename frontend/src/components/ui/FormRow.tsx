import clsx from 'clsx'
import type { CSSProperties, ReactNode } from 'react'

interface FormRowProps {
  label: ReactNode
  children: ReactNode
  /** 必填标记：label 前渲染红色星号（--sr-warning） */
  required?: boolean
  /** label 定宽：数字按 px，或任意 CSS 长度；默认 80 */
  width?: number | string
  className?: string
  style?: CSSProperties
}

/**
 * 标签+控件行薄壳：flex 行布局，label 定宽 + required 星号，间距走令牌 --sr-gap-row；
 * 替代 104 处手写"标签+控件"行（UI-TEMPLATES.md 契约 { label, children, required?, width? }），
 * 4 种历史变体在迁移轮按各自模板收敛。
 */
export default function FormRow({ label, children, required, width = 80, className, style }: FormRowProps) {
  return (
    <div className={clsx('sr-form-row', className)} style={{ display: 'flex', alignItems: 'center', gap: 'var(--sr-gap-row)', ...style }}>
      <span
        style={{
          width,
          flex: 'none',
          fontSize: 'var(--sr-font-sm)',
          color: 'var(--sr-text-2)',
          whiteSpace: 'nowrap',
        }}
      >
        {required && <span style={{ color: 'var(--sr-warning)' }}>* </span>}
        {label}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>{children}</div>
    </div>
  )
}
