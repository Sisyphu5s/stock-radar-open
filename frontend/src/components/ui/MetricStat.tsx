import { NumberFormatter, Text } from '@mantine/core'
import type { CSSProperties, KeyboardEvent, ReactNode } from 'react'

/** 涨跌语义 → 主题令牌(仅行情涨跌色;success/error 等状态色经 color prop 显式传入) */
const TONE_COLOR = { up: 'var(--sr-up)', down: 'var(--sr-down)', plain: 'var(--sr-text-1)' } as const

interface MetricStatProps {
  label: ReactNode
  value: ReactNode
  tone?: 'up' | 'down' | 'plain'
  sub?: ReactNode
  /** label 前缀图标(渲染于 label 之前,随数值同色) */
  icon?: ReactNode
  /** 主题语义色(var(--sr-*));用作数值色与激活边框色,优先级高于 tone */
  color?: string
  /** 激活态:1.5px 高亮边框(无 onClick 亦可单独生效,与 role=button 无关) */
  active?: boolean
  /** 传入时渲染为 role=button(Tab 聚焦 + Enter/Space 触发)+ cursor:pointer */
  onClick?: () => void
}

/**
 * 指标卡(自绘,Mantine Text + NumberFormatter 薄壳):
 * 与 StatCard 归并(2026-08):吸收 icon/active/onClick/color 能力,props 语义为
 * 颜色优先级 color ?? TONE_COLOR[tone];DOM 顺序保持 label / value / sub,
 * 命中 .sr-stat-card > div:nth-child(2) 数值样式(tabular-nums + 字号)。
 * 数值:number 走 NumberFormatter 千分位;字符串原样显示(与 antd Statistic 行为一致)。
 */
export default function MetricStat({ label, value, tone = 'plain', sub, icon, color, active = false, onClick }: MetricStatProps) {
  const numColor = color ?? TONE_COLOR[tone]
  const base: CSSProperties = {
    flex: 1,
    minWidth: 100, // 下限收窄(120→100):窄区 flex 容器可容纳两列,防单列大卡(P1-4);grid 场景由轨道 minmax 控制
    padding: 'var(--sr-pad-xl) 14px',
    border: active ? `1.5px solid ${numColor}` : '1px solid var(--sr-border)',
  }
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onClick?.()
    }
  }
  return (
    <div
      className="sr-stat-card"
      style={onClick ? { ...base, cursor: 'pointer' } : base}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      aria-pressed={onClick ? active : undefined}
      onClick={onClick}
      onKeyDown={onClick ? onKeyDown : undefined}
    >
      {icon != null ? (
        <span style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-2)' }}>
          <span style={{ color: numColor, marginRight: 'var(--sr-pad-xs)' }} aria-hidden>{icon}</span>
          {label}
        </span>
      ) : (
        <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>{label}</Text>
      )}
      {/* 数值:保持为 .sr-stat-card 的第 2 个子元素(命中 index.css 数值样式) */}
      <div style={{ color: numColor, fontSize: 'var(--sr-font-stat)', fontWeight: 700, lineHeight: 1.35, fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}>
        {typeof value === 'number' ? <NumberFormatter value={value} thousandSeparator /> : value}
      </div>
      {sub != null && <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>{sub}</Text>}
    </div>
  )
}
