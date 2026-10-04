import { Card } from '@mantine/core'
import type { ReactNode } from 'react'

interface CardShellProps {
  title: ReactNode
  /** 标题图标:自动渲染为 accent 色 + 间距(与既有图标化标题一致) */
  icon?: ReactNode
  extra?: ReactNode
  className?: string
  children: ReactNode
}

/**
 * 图标 + 标题行(accent 色图标 + 间距刻度 --sr-pad-sm)。
 * CardShell 与 templates/page-table.tsx(PageTableBlock)共用,抽为内部小组件。
 */
export function CardTitleIcon({ icon, title }: { icon?: ReactNode; title: ReactNode }) {
  if (!icon) return <>{title}</>
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--sr-pad-sm)' }}>
      <span style={{ color: 'var(--sr-accent)', display: 'inline-flex' }} aria-hidden>{icon}</span>
      {title}
    </span>
  )
}

/**
 * 卡片基板(Mantine Card 薄壳):统一「图标 + 标题 + extra + 内容」结构,
 * 吸收全站 20+ 处手写 Card 容器。标题图标间距走间距刻度 --sr-pad-sm(原 6px)。
 * 原 .sr-card-dense(antd 结构选择器,index.css)由本组件内联样式承担:
 * header 40px / body --sr-card-pad / extra 不收缩不换行。
 */
export default function CardShell({ title, icon, extra, className, children }: CardShellProps) {
  return (
    <Card padding={0} className={className} style={{ minWidth: 0, border: '1px solid var(--sr-border)', boxShadow: 'none' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sr-pad-md)', minHeight: 40, padding: '0 var(--sr-pad-2xl)', borderBottom: '1px solid var(--sr-border)' }}>
        <div style={{ flex: 1, minWidth: 0, fontSize: 'var(--sr-font-head)', fontWeight: 600, lineHeight: 1.4 }}>
          <CardTitleIcon icon={icon} title={title} />
        </div>
        {extra != null && <div style={{ flex: 'none', whiteSpace: 'nowrap' }}>{extra}</div>}
      </div>
      <div style={{ padding: 'var(--sr-card-pad)' }}>{children}</div>
    </Card>
  )
}
