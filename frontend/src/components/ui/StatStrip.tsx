import { Skeleton } from '@mantine/core'
import type { CSSProperties, KeyboardEvent, ReactNode } from 'react'

/** 涨跌语义 → 主题令牌(仅行情涨跌色;plain = 主文字色) */
const TONE_COLOR = { up: 'var(--sr-up)', down: 'var(--sr-down)', plain: 'var(--sr-text-1)' } as const

export interface StatStripItem {
  key: string
  label: ReactNode
  value: ReactNode
  tone?: 'up' | 'down' | 'plain'
  /** 值下方弱化补充(11px,text-3) */
  sub?: ReactNode
  /** 传入时该项渲染为可点击(role=button + Tab 聚焦 + Enter/Space 触发) */
  onClick?: () => void
}

interface StatStripProps {
  items: StatStripItem[]
  /** 数据未到时数字位骨架(02 §2.2:骨架而非空白);label 保留显示 */
  loading?: boolean
  /** 横排滚动(移动端):true 时单行可横滚,项不换行 */
  scrollable?: boolean
  className?: string
  style?: CSSProperties
}

/**
 * L0 结论条(02 硬规则 2:每页必有;06 §8.1 草案落地):
 * 页面第一屏顶部的结论数字条——label 11px(text-3)+ value stat 22px(600 + tabular-nums)+ sub 11px;
 * 项间 1px 分隔线,可点击项 hover 升阶;loading 时数字位骨架。
 * 消费页:信号中心 / 关注列表 / 个股工作台 / 研究评估·回测 / 设置·状态。
 */
export default function StatStrip({ items, loading = false, scrollable = false, className, style }: StatStripProps) {
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>, fn?: () => void) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      fn?.()
    }
  }
  const base: CSSProperties = {
    display: 'flex',
    alignItems: 'stretch',
    flexWrap: scrollable ? 'nowrap' : 'wrap',
    overflowX: scrollable ? 'auto' : 'visible',
    borderRadius: 'var(--sr-radius-card)',
    border: '1px solid var(--sr-border)',
    background: 'var(--sr-card-bg)',
    minWidth: 0,
  }
  return (
    <div className={className} style={style ? { ...base, ...style } : base} role="group" aria-label="关键结论">
      {items.map((it, i) => {
        const numColor = TONE_COLOR[it.tone ?? 'plain']
        // T-127:scrollable 模式下项按内容宽排布(flex 0 0 auto),由容器 overflowX 承接横滚,
        // 不再 flex 收缩把项压到内容以下造成重叠/裁切
        const itemBase: CSSProperties = {
          flex: scrollable ? '0 0 auto' : '1 1 120px',
          minWidth: scrollable ? undefined : 0,
          padding: 'var(--sr-pad-md) var(--sr-pad-xl)',
          borderLeft: i > 0 ? '1px solid var(--sr-border)' : undefined,
          display: 'flex',
          flexDirection: 'column',
          gap: 'var(--sr-pad-xs)',
          cursor: it.onClick ? 'pointer' : undefined,
        }
        return (
          <div
            key={it.key}
            style={itemBase}
            role={it.onClick ? 'button' : undefined}
            tabIndex={it.onClick ? 0 : undefined}
            onClick={it.onClick}
            onKeyDown={it.onClick ? (e) => onKeyDown(e, it.onClick) : undefined}
          >
            <span style={{ fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-3)', whiteSpace: 'nowrap' }}>{it.label}</span>
            <span style={{
              color: numColor,
              fontSize: loading ? undefined : 'var(--sr-font-stat)',
              fontWeight: 600,
              lineHeight: 1.3,
              fontVariantNumeric: 'tabular-nums',
              whiteSpace: 'nowrap',
              minHeight: '1.3em',
            }}>
              {loading
                ? <Skeleton height={22} width={64} radius="sm" style={{ display: 'inline-block', verticalAlign: 'middle' }} />
                : it.value}
            </span>
            {it.sub != null && <span style={{ fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-3)', whiteSpace: 'nowrap' }}>{it.sub}</span>}
          </div>
        )
      })}
    </div>
  )
}
