import { Card } from '@mantine/core'
import type { ReactNode } from 'react'
import { CardTitleIcon } from '../components/ui/CardShell'

export interface PageTableBlockProps {
  /** 卡片标题(可选) */
  title?: ReactNode
  /** 标题前图标(可选,渲染为 accent 色,与 CardShell 一致) */
  icon?: ReactNode
  /** Card 头部右侧动作区(如刷新/分页控制) */
  extra?: ReactNode
  /** 表格内容(通常放 ui/DataTable + 无限滚动哨兵) */
  children: ReactNode
  className?: string
}

/**
 * 页头表格块(UI-TEMPLATES.md T1 模板承载件)。
 *
 * 结构:Mantine Card 薄壳(minWidth 0),body 无额外 padding
 * (DataTable 自带密度,表格直接撑满卡片)。与 CardShell 的区别:表格专用,
 * body 内边距为 0,标题/extra 语义为「表格容器」。图标标题行复用 CardShell 的
 * CardTitleIcon 小组件(两处共用,不重复实现)。
 *
 * T1 组合:PageShell > PageHeader + (MetricStat 筛选行 | Toolbar) + PageTableBlock。
 * 样板页:research/Alpha101.tsx;消费方:后续批 G 页面
 * (MarketRadar / SignalTemplates / Tasks / WatchlistPage 等 T1 页面)。
 */
export function PageTableBlock({ title, icon, extra, children, className }: PageTableBlockProps) {
  const hasHead = title != null || icon != null || extra != null
  return (
    <Card padding={0} className={className} style={{ minWidth: 0, border: '1px solid var(--sr-border)', boxShadow: 'none' }}>
      {hasHead && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sr-pad-md)', minHeight: 40, padding: '0 var(--sr-pad-2xl)', borderBottom: '1px solid var(--sr-border)' }}>
          <div style={{ flex: 1, minWidth: 0, fontSize: 'var(--sr-font-head)', fontWeight: 600, lineHeight: 1.4 }}>
            <CardTitleIcon icon={icon} title={title} />
          </div>
          {extra != null && <div style={{ flex: 'none', whiteSpace: 'nowrap' }}>{extra}</div>}
        </div>
      )}
      <div style={{ padding: 0 }}>{children}</div>
    </Card>
  )
}
