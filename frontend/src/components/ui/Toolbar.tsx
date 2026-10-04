import { Group } from '@mantine/core'
import clsx from 'clsx'
import type { CSSProperties, ReactNode } from 'react'

interface ToolbarProps {
  children: ReactNode
  style?: CSSProperties
  /** 紧凑模式:更小间距(供满高页工具条使用) */
  compact?: boolean
  /** 单行不换行 + 溢出横向滚动(移动端友好);默认 wrap */
  scrollable?: boolean
  /** 吸顶工具区:包一层 .sr-toolbar-sticky(sticky 顶部,列表页筛选常驻) */
  sticky?: boolean
  /** 右对齐动作区:自动包 .sr-toolbar-tail(margin-left:auto,不换行) */
  tail?: ReactNode
  className?: string
}

/**
 * 工具条(Mantine Group 薄壳,wrap/gap/align);旧 props 全兼容。
 * gap 说明:内联 gap="var(--sr-pad-sm)"(6px)固定生效,覆盖 .sr-toolbar CSS 类的
 * gap(var(--sr-gap-ctl),亮色 8px / 暗色 6px)——统一以 6px 为基准(组内紧凑间距),
 * 勿依赖 CSS 类 gap;scrollable 时由 .sr-toolbar-scroll 接管换行与横滚。
 */
export default function Toolbar({ children, style, compact, scrollable, sticky, tail, className }: ToolbarProps) {
  const cls = clsx('sr-toolbar', compact && 'sr-toolbar-compact', scrollable && 'sr-toolbar-scroll', className)
  const bar = (
    <Group wrap={scrollable ? 'nowrap' : 'wrap'} gap="var(--sr-pad-sm)" align="center" className={cls} style={style}>
      {children}
      {tail != null && <span className="sr-toolbar-tail">{tail}</span>}
    </Group>
  )
  return sticky ? <div className="sr-toolbar-sticky">{bar}</div> : bar
}
