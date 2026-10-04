/**
 * 研究域 DataList（T-43：antd Descriptions → Mantine 替代）。
 * ------------------------------------------------------------
 * Mantine 无 Descriptions 等价物；本薄壳承担「label/value 键值网格」职责：
 * items = antd Descriptions items 同形（key/label/children/span），column 网格自适应，
 * bordered 时逐格描边。研究域各页（挖掘详情 / Alpha101 抽屉 / 任务摘要 / 只读快照）共用。
 */
import type { CSSProperties, ReactNode } from 'react'

export interface DataListItem {
  key: string
  label: ReactNode
  children: ReactNode
  /** 跨列数（≤ column；antd Descriptions span 语义） */
  span?: number
}

interface DataListProps {
  items: DataListItem[]
  /** 网格列数（antd Descriptions column；移动端自动收敛为 1 列） */
  column?: number
  /** 逐格描边（antd bordered） */
  bordered?: boolean
  /** label 字号档（antd size="small" = 12px 标签） */
  size?: 'small' | 'default'
}

const cellStyle = (bordered: boolean): CSSProperties => ({
  display: 'flex',
  alignItems: 'baseline',
  gap: 'var(--sr-pad-sm)',
  minWidth: 0,
  padding: bordered ? '6px 10px' : '3px 0',
  border: bordered ? '1px solid var(--sr-border)' : 'none',
  borderRadius: bordered ? 'var(--sr-radius-tag)' : 0,
  background: bordered ? 'var(--sr-card-bg)' : 'transparent',
})

export default function DataList({ items, column = 1, bordered = false, size = 'small' }: DataListProps) {
  const labelStyle: CSSProperties = {
    flex: 'none',
    color: 'var(--sr-text-3)',
    fontSize: size === 'small' ? 'var(--sr-font-xs)' : 'var(--sr-font-sm)',
    whiteSpace: 'nowrap',
  }
  const valueStyle: CSSProperties = {
    flex: 1,
    minWidth: 0,
    color: 'var(--sr-text-1)',
    fontSize: size === 'small' ? 'var(--sr-font-sm)' : 'var(--sr-font-page)',
    overflowWrap: 'anywhere',
  }
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: `repeat(${column}, minmax(0, 1fr))`,
      gap: bordered ? 4 : 2,
      alignItems: 'start',
    }}>
      {items.map((it) => (
        <div
          key={it.key}
          style={{
            ...cellStyle(bordered),
            gridColumn: it.span != null && it.span > 1 ? `span ${Math.min(it.span, column)}` : undefined,
          }}
        >
          <span style={labelStyle}>{it.label}</span>
          <span style={valueStyle}>{it.children}</span>
        </div>
      ))}
    </div>
  )
}
