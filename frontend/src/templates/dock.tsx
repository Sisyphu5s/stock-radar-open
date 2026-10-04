import clsx from 'clsx'
import type { CSSProperties, ReactNode } from 'react'

export interface DockPanelProps {
  /** 面板标题（可选，无则不渲染头部） */
  title?: ReactNode
  /** 头部右侧操作区（可选） */
  extra?: ReactNode
  children: ReactNode
  className?: string
  style?: CSSProperties
}

/**
 * 工作台面板壳（T5 模板）：flex column 满高 + 可滚动内容区。
 * 消费方：SignalCenter / StockWorkbench（批 I 迁移，收编各自手写的面板头/滚动区）。
 * 本层为"复合模板"：允许组合 antd 原子与 ui/ 薄壳（如 Toolbar），
 * 与 ui/ 的纯薄壳（PageShell 等）区别在于承载完整面板结构语义，而非单一基元。
 */
export function DockPanel({ title, extra, children, className, style }: DockPanelProps) {
  return (
    <div
      className={clsx('sr-dock-panel', className)}
      style={{
        display: 'flex',
        flexDirection: 'column',
        minWidth: 0,
        height: '100%',
        background: 'var(--sr-card-bg)',
        border: '1px solid var(--sr-border)',
        borderRadius: 'var(--sr-radius-card)',
        overflow: 'hidden',
        ...style,
      }}
    >
      {title != null && (
        <div
          style={{
            flex: 'none',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: 'var(--sr-pad-sm) var(--sr-pad-md)',
            borderBottom: '1px solid var(--sr-border)',
          }}
        >
          <span style={{ fontSize: 'var(--sr-font-sm)', fontWeight: 600, color: 'var(--sr-text-1)' }}>{title}</span>
          {extra != null && <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sr-gap-ctl)' }}>{extra}</div>}
        </div>
      )}
      <div
        className="sr-dock-scroll"
        style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: 'var(--sr-pad-sm)' }}
      >
        {children}
      </div>
    </div>
  )
}
