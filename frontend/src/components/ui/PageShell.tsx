import clsx from 'clsx'
import type { CSSProperties, ReactNode } from 'react'

interface PageShellProps {
  /** 满高页（dock 面板 / 满高表格页）：高度 = 100dvh - 顶栏 - 内容 padding，内部滚动自理 */
  fill?: boolean
  /** 页头区块（通常传 PageHeader） */
  header?: ReactNode
  children: ReactNode
  className?: string
  style?: CSSProperties
}

/** 页面根容器：统一 .sr-page（gap 16 纵向弹性），fill 时满高不滚动 */
export default function PageShell({ fill, header, children, className, style }: PageShellProps) {
  return (
    <div className={clsx('sr-page', fill && 'sr-page-fill', className)} style={style}>
      {header}
      {children}
    </div>
  )
}
